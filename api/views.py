"""
Kassa ilovasi uchun API.

Yo'nalish bir tomonlama: kassa **so'raydi**, Hub **javob beradi**.
Hub kassaga o'zi ulanmaydi. Sabab oddiy — kassalar do'kon ichida, oq IP
manzilsiz, ba'zan internetsiz. Kim ulana olsa, o'sha ulanadi.

Endpointlar:

    GET  /api/v1/hello              kassa kim, smena ochiqmi
    GET  /api/v1/catalog?since=     tovarlar (o'zgarganlari)
    POST /api/v1/catalog/refresh    MoySklad'dan darhol tortish (tugma)
    GET  /api/v1/customers?q=       mijoz qidirish
    POST /api/v1/shift/open         smena ochish
    POST /api/v1/shift/close        smena yopish, chek matni qaytadi
    POST /api/v1/sales              chek yuborish (takrorlansa ham xavfsiz)
    POST /api/v1/cash               kassaga kirim/chiqim

Hamma summa **tiyinda**, butun son. Kasr yo'q.
"""

from __future__ import annotations

import json
import logging
import threading
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from catalog.models import Barcode, Customer, Product, Stock
from sales.models import (
    CashOperation,
    Cashier,
    Register,
    Payment,
    PaymentMethod,
    Sale,
    SaleItem,
    Shift,
)
from sales.services import build_receipt, close_shift, ShiftError
from shared.receipt import render

from .auth import error, register_required

logger = logging.getLogger("api")


def _push_sale_now(sale_id: int) -> None:
    """Savdoni MoySklad'ga DARHOL yozadi (fon oqimida, so'rovni kutdirmay).

    Shu tufayli chek MoySklad'da 5 daqiqalik cron'ni kutmasdan, 1-2
    soniyada paydo bo'ladi. Xato bo'lsa — jimgina qoldiriladi va
    `sync_sales` cron'i keyin qayta urinadi (backoff bilan). syncId
    tufayli ikki marta yozilmaydi.
    """
    from django.conf import settings as s

    if not getattr(s, "MOYSKLAD_TOKEN", ""):
        return

    from django.db import connection
    from django.utils import timezone as tz

    from moysklad.client import MoySkladClient
    from sales.writer import SaleWriter

    try:
        sale = (
            Sale.objects.select_related("shift__register__store", "customer")
            .filter(pk=sale_id)
            .first()
        )
        if not sale or sale.sync_status == Sale.SENT:
            return
        SaleWriter(MoySkladClient(token=s.MOYSKLAD_TOKEN)).send(sale)
        sale.sync_status = Sale.SENT
        sale.synced_at = tz.now()
        sale.sync_error = ""
        sale.next_attempt_at = None
        sale.save(update_fields=[
            "sync_status", "synced_at", "sync_error", "next_attempt_at"
        ])
    except Exception as e:  # cron baribir qayta urinadi
        logger.info("Darhol yozilmadi (cron qayta urinadi): %s", e)
    finally:
        connection.close()

PAGE_SIZE = 500


def body(request) -> dict:
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return {}


# --------------------------------------------------------------- ulanish


@csrf_exempt
@require_POST
def connect(request):
    """Kassani serverga ulash: login va parol evaziga token beriladi.

    Bu yagona endpoint tokensiz ishlaydi — chunki uning vazifasi
    aynan tokenni berish. Bir marta, kassani sozlashda chaqiriladi.

    Token qaytariladi va ilova uni o'zida saqlaydi. Xodim uzun tokenni
    ko'rmaydi ham, qo'lda terishi ham shart emas.
    """
    data = body(request)
    login = (data.get("login") or "").strip().lower()
    password = (data.get("password") or "").strip()

    register = Register.objects.filter(
        login=login, active=True
    ).select_related("store").first()

    if not register or not register.check_password(password):
        return error("Login yoki parol noto'g'ri", status=401)

    return JsonResponse({
        "token": register.api_token,
        "register": {"code": register.code, "name": register.name},
        "point": register.store.name,
    })


# ------------------------------------------------------------------- kirish


@csrf_exempt
@require_POST
@register_required
def login(request):
    """Kassir kassaga kiradi: login + PIN.

    Ikki qatlam: qurilma tokeni «bu kassa bizniki» deydi, PIN esa
    «bu odam kim» deydi. Bittasi yetarli emas — token o'g'irlansa ham
    PIN kerak, PIN bilinsa ham kassa yonida turish kerak.

    Xato PIN uchun sabab aytilmaydi («login noto'g'ri» yoki «PIN
    noto'g'ri» emas, balki umumiy xabar): aks holda mavjud loginlarni
    bittalab topib olish mumkin bo'lardi.
    """
    data = body(request)
    name = (data.get("login") or "").strip().lower()
    pin = (data.get("pin") or "").strip()

    cashier = Cashier.objects.filter(login=name, active=True).first()
    if not cashier or not cashier.check_pin(pin):
        return error("Login yoki PIN noto'g'ri", status=401)

    Cashier.objects.filter(pk=cashier.pk).update(last_login_at=timezone.now())

    return JsonResponse({
        "cashier": {
            "id": cashier.pk,
            "name": cashier.name,
            "login": cashier.login,
            "is_manager": cashier.is_manager,
        }
    })


# ---------------------------------------------------------------- versiya


@require_GET
@register_required
def version(request):
    """Kassa ilovasining eng yangi versiyasi.

    Kassa ochilganda va har 30 daqiqada shu yerga qaraydi. Versiya
    o'zinikidan katta bo'lsa — `url` dan yuklab olib, o'zini almashtiradi.
    `mandatory` bo'lsa kassir «keyinroq» deya olmaydi.

    Manba: panelning «Versiyalar» sahifasi (KassaRelease). U bo'sh bo'lsa —
    Railway'dagi APP_VERSION / APP_DOWNLOAD_URL (eski, zaxira yo'l).
    """
    from sales.models import KassaRelease

    rel = KassaRelease.latest()
    if rel:
        return JsonResponse({
            "version": rel.version,
            "url": request.build_absolute_uri(
                f"/api/v1/update/download?v={rel.version}"
            ),
            "notes": rel.notes,
            "mandatory": rel.mandatory,
            "size": rel.size,
            "sha256": rel.sha256,
        })
    return JsonResponse({
        "version": settings.APP_VERSION,
        "url": settings.APP_DOWNLOAD_URL,
        "notes": settings.APP_UPDATE_NOTES,
        "mandatory": False,
        "size": 0,
        "sha256": "",
    })


@require_GET
@register_required
def update_download(request):
    """Kassa uchun exe faylni beradi. Faqat kassa tokeni bilan."""
    from django.http import FileResponse, Http404

    from sales.models import KassaRelease

    v = (request.GET.get("v") or "").strip()
    rel = KassaRelease.objects.filter(version=v, active=True).first() if v else None
    if not rel or not rel.file:
        raise Http404("Bunday versiya yo'q")
    try:
        handle = rel.file.open("rb")
    except (FileNotFoundError, ValueError):
        logger.error("Versiya %s fayli diskda yo'q: %s", v, rel.file.name)
        raise Http404("Fayl topilmadi")
    resp = FileResponse(
        handle, as_attachment=True, filename=f"SevimliKassa-{rel.version}.exe",
        content_type="application/octet-stream",
    )
    if rel.size:
        resp["Content-Length"] = str(rel.size)
    return resp


# ------------------------------------------------------------------ hello


@require_GET
@register_required
def hello(request):
    reg = request.register
    shift = reg.shifts.filter(status=Shift.OPEN).first()
    st = reg.settings

    # Kassirlar: agar shu kassaga aniq kassirlar biriktirilgan bo'lsa —
    # faqat o'shalar kira oladi (MoySklad «Кассиры» bo'limi). Bo'sh
    # bo'lsa — hamma faol kassir kira oladi (eski holat).
    allowed = st.allowed_cashiers.filter(active=True)
    cashier_qs = allowed if allowed.exists() else Cashier.objects.filter(active=True)

    return JsonResponse(
        {
            "register": {"code": reg.code, "name": reg.name},
            "point": reg.store.name,
            "market": settings.MARKET_NAME,
            "receipt_width": settings.RECEIPT_WIDTH,
            "server_time": timezone.now().isoformat(),
            "shift": _shift_json(shift) if shift else None,
            "payment_methods": [
                {"code": m.code, "name": m.name, "is_cash": m.is_cash}
                for m in PaymentMethod.objects.filter(active=True)
            ],
            # Kassirlar ro'yxati — kirish oynasida tugma bo'lib chiqadi.
            # PIN baribir so'raladi, shuning uchun ismni ko'rsatish
            # xavfsizlikni kamaytirmaydi, lekin kirishni tezlashtiradi.
            "cashiers": [
                {"login": c.login, "name": c.name}
                for c in cashier_qs
            ],
            # Kassaning sozlamalari — ilova shunga qarab ishlaydi
            # (chegirma chegarasi, majburiy maydonlar, qaytarish va h.k.).
            "settings": st.as_kassa_dict(),
        }
    )


def _shift_json(shift: Shift) -> dict:
    return {
        "id": shift.pk,
        "number": shift.number,
        "cashier": shift.cashier,
        "opened_at": shift.opened_at.isoformat(),
        "opening_cash": shift.opening_cash,
        "next_receipt_number": (
            shift.sales.filter(kind=Sale.SALE).count() + 1
        ),
    }


# ------------------------------------------------------------- qaytarish


@require_GET
@register_required
def returnable_sales(request):
    """Qaytarish uchun oxirgi savdolar — tovarlari va to'lovi bilan.

    MoySklad Kassa'dagidek: shu kassaning oxirgi savdolari, smena bo'yicha
    guruhlangan. Kassir chek raqamini qidiradi yoki ro'yxatdan tanlaydi.

    Faqat SHU kassaning savdolari qaytariladi. Boshqa kassada sotilgan
    chekni qaytarish — hozircha yo'q (keyin kengaytiriladi).

    Har savdodan qancha qaytarilganini ham hisoblaymiz: bir chekni
    ikki marta to'liq qaytarib bo'lmasin.
    """
    reg = request.register
    sales = (
        Sale.objects.filter(shift__register=reg, kind=Sale.SALE)
        .select_related("shift", "customer")
        .prefetch_related("items", "payments__method")
        .order_by("-created_at")[:40]
    )

    # Har chek qatoridan qancha allaqachon qaytarilgan
    returned = {}
    origins = [s.pk for s in sales]
    for ret in Sale.objects.filter(kind=Sale.RETURN, origin_id__in=origins).prefetch_related("items"):
        for item in ret.items.all():
            key = (ret.origin_id, item.ms_product_id or item.name)
            returned[key] = returned.get(key, 0) + float(item.quantity)

    rows = []
    for s in sales:
        pays = list(s.payments.all())
        # Asosiy to'lov turi — belgi uchun (naqd/karta)
        is_cash = any(p.method.is_cash for p in pays)
        rows.append({
            "id": s.pk,
            "number": s.number,
            "created_at": s.created_at.isoformat(),
            "net_total": s.net_total,
            "is_cash": is_cash,
            "customer": s.customer.name if s.customer_id else "",
            "shift": {
                "number": s.shift.number,
                "opened_at": s.shift.opened_at.isoformat(),
                "closed": s.shift.status == Shift.CLOSED,
            },
            "payments": [
                {"method": p.method.code, "name": p.method.name,
                 "is_cash": p.method.is_cash, "amount": p.amount}
                for p in pays
            ],
            "items": [
                {
                    "product_id": it.product_id,
                    "ms_product_id": str(it.ms_product_id) if it.ms_product_id else "",
                    "name": it.name,
                    "barcode": it.barcode,
                    "price": it.price,
                    "sold_qty": str(it.quantity),
                    "returned_qty": returned.get(
                        (s.pk, it.ms_product_id or it.name), 0
                    ),
                    "is_weight": it.product.is_weight if it.product_id else False,
                }
                for it in s.items.all()
            ],
        })

    return JsonResponse({"sales": rows})


# ---------------------------------------------------------------- katalog


@require_GET
@register_required
def catalog(request):
    """Tovarlar. `since` berilsa — faqat o'zgarganlari.

    Kassa birinchi marta hammasini oladi, keyin faqat farqni. Katalog
    katta bo'lgani uchun sahifalab beriladi.
    """
    since = request.GET.get("since")
    dt = parse_datetime(since) if since else None

    if dt:
        # Delta: arxivlangan/o'chirilganlar HAM keladi (`archived: true`) —
        # kassa ularni lokal bazadan o'chiradi. Aks holda buxgalter
        # o'chirgan tovar kassada abadiy qolib ketardi.
        qs = Product.objects.filter(synced_at__gte=dt)
    else:
        # To'liq yuklash: faqat tiriklari
        qs = Product.objects.filter(archived=False)

    after = request.GET.get("after")
    if after:
        qs = qs.filter(pk__gt=int(after))

    qs = qs.order_by("pk")[:PAGE_SIZE]
    rows = list(qs)

    codes = {
        b.product_id: b.value
        for b in Barcode.objects.filter(product__in=rows).order_by("-pk")
    }
    stock = {
        s.product_id: s.quantity
        for s in Stock.objects.filter(
            product__in=rows, store_ms_id=request.register.store.store_ms_id
        )
    }

    return JsonResponse(
        {
            "products": [
                {
                    "id": p.pk,
                    "ms_id": str(p.ms_id),
                    "name": p.name,
                    "code": p.code,
                    "price": p.sale_price,
                    "is_weight": p.is_weight,
                    "plu": p.plu,
                    "tracked": p.tracked,
                    "barcode": codes.get(p.pk, ""),
                    "stock": float(stock.get(p.pk, 0)),
                    "archived": p.archived,
                }
                for p in rows
            ],
            "next_after": rows[-1].pk if len(rows) == PAGE_SIZE else None,
            "server_time": timezone.now().isoformat(),
        }
    )


# Bir vaqtda bitta yangilanish — 10 ta kassa birdan bossa ham MoySklad'ga
# bitta so'rov to'plami ketadi, qolganlari «band» javobini oladi va
# shunchaki delta'ni tortadi (birinchisi tugagach o'zgarishlar tayyor).
_refresh_lock = threading.Lock()
REFRESH_COOLDOWN_SEC = 30


@csrf_exempt
@require_POST
@register_required
def catalog_refresh(request):
    """Kassadagi «Ma'lumotlarni yangilash» — MoySklad'dan DARHOL tortadi.

    Cron 5 daqiqada bir yuradi; buxgalter narxni o'zgartirib «hozir
    yangilansin» desa — kassir shu tugmani bosadi. Delta (faqat
    o'zgarganlar) tortiladi: odatda 1-3 soniya.

    Javob:
        {"ran": true,  "products": 12, "customers": 0}   — tortildi
        {"ran": false, "reason": "busy"|"cooldown"}       — hozirgina tortilgan
        {"ran": false, "reason": "error", "error": "..."} — MoySklad xatosi
    Har qanday holatda kassa keyin GET /catalog?since= bilan farqni oladi.
    """
    from datetime import timedelta

    from catalog.models import SyncState
    from catalog.sync import CatalogSync
    from moysklad.client import MoySkladClient, MoySkladError

    token = getattr(settings, "MOYSKLAD_TOKEN", "")
    if not token:
        return JsonResponse({"ran": False, "reason": "no_token"})

    # Hozirgina tortilgan bo'lsa — MoySklad limitini bekorga sarflamaymiz
    state = SyncState.objects.filter(entity="assortment").first()
    if state and state.last_success_at and (
        timezone.now() - state.last_success_at < timedelta(seconds=REFRESH_COOLDOWN_SEC)
    ):
        return JsonResponse({"ran": False, "reason": "cooldown"})

    if not _refresh_lock.acquire(blocking=False):
        return JsonResponse({"ran": False, "reason": "busy"})
    try:
        sync = CatalogSync(MoySkladClient(token=token))
        try:
            products = sync.sync_products()
            customers = sync.sync_customers()
        except MoySkladError as exc:
            logger.warning("Kassa yangilanishi: MoySklad xatosi: %s", exc)
            return JsonResponse({"ran": False, "reason": "error", "error": str(exc)[:200]})
    finally:
        _refresh_lock.release()

    logger.info(
        "Kassa %s yangilanish so'radi: %s tovar, %s mijoz",
        request.register.code, products, customers,
    )
    return JsonResponse({"ran": True, "products": products, "customers": customers})


@require_GET
@register_required
def customers(request):
    """Mijoz qidirish — telefon, karta yoki ism bo'yicha."""
    q = (request.GET.get("q") or "").strip()
    if len(q) < 3:
        return error("Kamida 3 belgi kiriting")

    qs = Customer.objects.filter(archived=False)
    found = (
        qs.filter(phone__icontains=q)
        | qs.filter(discount_card__icontains=q)
        | qs.filter(name__icontains=q)
    )[:20]

    return JsonResponse(
        {
            "customers": [
                {
                    "id": c.pk,
                    "ms_id": str(c.ms_id),
                    "name": c.name,
                    "phone": c.phone,
                    "card": c.discount_card,
                    "bonus_points": c.bonus_points,
                    "accumulation_discount": float(c.accumulation_discount),
                    "personal_discount": float(c.personal_discount),
                }
                for c in found
            ]
        }
    )


def _customer_dict(c: Customer) -> dict:
    return {
        "id": c.pk,
        "ms_id": str(c.ms_id),
        "name": c.name,
        "phone": c.phone,
        "card": c.discount_card,
        "bonus_points": c.bonus_points,
        "accumulation_discount": float(c.accumulation_discount),
        "personal_discount": float(c.personal_discount),
    }


@csrf_exempt
@require_POST
@register_required
def create_customer(request):
    """Kassadan yangi mijoz qo'shish.

    MoySklad tokeni sozlangan bo'lsa — kontragent MoySklad'da yaratiladi
    va uning id'si ishlatiladi (savdo o'shanga bog'lanadi). Sozlanmagan
    bo'lsa (sinov) — lokal uuid bilan yaratiladi.
    """
    import uuid as _uuid

    data = body(request)
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    card = (data.get("card") or "").strip()

    if len(name) < 2:
        return error("Ism kamida 2 harf bo'lishi kerak")

    ms_id = None
    token = getattr(settings, "MOYSKLAD_TOKEN", "")
    if token:
        # MoySklad'da kontragent yaratamiz — savdo shunga bog'lanadi
        from moysklad.client import MoySkladClient, MoySkladError

        payload = {"name": name}
        if phone:
            payload["phone"] = phone
        try:
            created = MoySkladClient(token=token).post("entity/counterparty", payload)
            ms_id = created.get("id")
        except MoySkladError as e:
            return error(f"MoySklad'ga yozilmadi: {e}", status=502)
        if not ms_id:
            return error("MoySklad javobida id yo'q", status=502)
    else:
        ms_id = str(_uuid.uuid4())

    customer, created_local = Customer.objects.get_or_create(
        ms_id=ms_id,
        defaults={"name": name, "phone": phone, "discount_card": card},
    )
    if not created_local:
        # Kamdan-kam: shu id allaqachon bor — nomni yangilaymiz
        customer.name = name
        customer.phone = phone or customer.phone
        customer.save(update_fields=["name", "phone"])

    return JsonResponse({"customer": _customer_dict(customer)})


# ------------------------------------------------------------------ smena


@csrf_exempt
@require_POST
@register_required
def shift_open(request):
    reg = request.register
    data = body(request)

    if reg.shifts.filter(status=Shift.OPEN).exists():
        return error("Bu kassada ochiq smena bor", status=409)

    # Smenani faqat kirgan kassir ocha oladi
    cashier_ref = None
    cashier_id = data.get("cashier_id")
    if cashier_id:
        cashier_ref = Cashier.objects.filter(pk=cashier_id, active=True).first()
        if not cashier_ref:
            return error("Kassir topilmadi", status=401)
        name = cashier_ref.name
    else:
        name = (data.get("cashier") or "").strip()

    if not name:
        return error("Kassir ko'rsatilmagan")

    last = reg.shifts.order_by("-number").values_list("number", flat=True).first() or 0
    shift = Shift.objects.create(
        register=reg,
        number=last + 1,
        cashier=name,
        cashier_ref=cashier_ref,
        opened_at=timezone.now(),
        opening_cash=int(data.get("opening_cash") or 0),
    )
    return JsonResponse({"shift": _shift_json(shift)}, status=201)


@csrf_exempt
@require_POST
@register_required
def shift_close(request):
    reg = request.register
    data = body(request)

    shift = reg.shifts.filter(status=Shift.OPEN).first()
    if not shift:
        return error("Ochiq smena yo'q", status=409)

    counted = data.get("counted_cash")
    try:
        receipt = close_shift(
            shift, counted_cash=int(counted) if counted is not None else None
        )
    except ShiftError as e:
        return error(str(e), status=409)

    return JsonResponse(
        {
            "shift_id": shift.pk,
            # Kassa shu matnni printerga yuboradi — o'zi hech narsa
            # hisoblamaydi, aks holda ikki xil raqam chiqishi mumkin.
            "receipt_text": render(receipt, settings.RECEIPT_WIDTH),
            "net_total": receipt.net_total,
            "cash_total": receipt.cash_total,
            "cashless_total": receipt.cashless_total,
            "expected_cash": receipt.expected_cash,
            "cash_diff": receipt.cash_diff,
            "pending": shift.sales.exclude(sync_status=Sale.SENT).count(),
        }
    )


@require_GET
@register_required
def shift_report(request):
    """Oraliq hisobot — smenani yopmasdan."""
    shift = request.register.shifts.filter(status=Shift.OPEN).first()
    if not shift:
        return error("Ochiq smena yo'q", status=409)

    receipt = build_receipt(shift, market=settings.MARKET_NAME)
    return JsonResponse(
        {"receipt_text": render(receipt, settings.RECEIPT_WIDTH)}
    )


@csrf_exempt
@require_POST
@register_required
def cash_operation(request):
    data = body(request)
    shift = request.register.shifts.filter(status=Shift.OPEN).first()
    if not shift:
        return error("Ochiq smena yo'q", status=409)

    kind = data.get("kind")
    if kind not in (CashOperation.IN, CashOperation.OUT):
        return error("kind: 'in' yoki 'out' bo'lishi kerak")

    amount = int(data.get("amount") or 0)
    if amount <= 0:
        return error("Summa musbat bo'lishi kerak")

    op = CashOperation.objects.create(
        shift=shift, kind=kind, amount=amount,
        comment=(data.get("comment") or "")[:256],
    )
    return JsonResponse({"id": op.pk}, status=201)


# ------------------------------------------------------------------- chek


@csrf_exempt
@require_POST
@register_required
def create_sale(request):
    """Chekni qabul qiladi.

    **Takroriy so'rov xavfsiz.** Kassa javobni olmasdan uzilib qolsa,
    o'sha chekni yana yuboradi. `local_uuid` bo'yicha allaqachon bor
    bo'lsa — yangisi yaratilmaydi, borining raqami qaytariladi.
    """
    data = body(request)
    reg = request.register

    local_uuid = data.get("local_uuid")
    if not local_uuid:
        return error("local_uuid kerak")

    existing = Sale.objects.filter(local_uuid=local_uuid).first()
    if existing:
        return JsonResponse(
            {"id": existing.pk, "number": existing.number, "duplicate": True}
        )

    shift = reg.shifts.filter(status=Shift.OPEN).first()
    if not shift:
        return error("Ochiq smena yo'q", status=409)

    items = data.get("items") or []
    if not items:
        return error("Chek bo'sh")

    payments = data.get("payments") or []
    if not payments:
        return error("To'lov ko'rsatilmagan")

    try:
        return _save_sale(shift, data, items, payments, local_uuid)
    except ValueError as e:
        return error(str(e))


@transaction.atomic
def _save_sale(shift, data, items, payments, local_uuid):
    kind = data.get("kind") or Sale.SALE
    if kind not in (Sale.SALE, Sale.RETURN):
        raise ValueError("kind noto'g'ri")

    # Qatorlar summasi
    lines = []
    lines_total = 0
    for pos, raw in enumerate(items, start=1):
        try:
            qty = Decimal(str(raw.get("quantity", "1")))
        except (InvalidOperation, TypeError):
            raise ValueError(f"{pos}-qatorda miqdor noto'g'ri")
        if qty <= 0:
            raise ValueError(f"{pos}-qatorda miqdor musbat bo'lishi kerak")

        total = int(raw.get("total") or 0)
        lines_total += total
        lines.append((pos, raw, qty, total))

    points_spent = int(data.get("points_spent") or 0)
    net_total = lines_total - points_spent * 100

    # To'lovlar chek summasiga teng bo'lishi shart. Bu yerda tekshirmasak,
    # xato smena yakunida chiqadi va kim aybdorligi noma'lum bo'ladi.
    methods = {m.code: m for m in PaymentMethod.objects.filter(active=True)}
    pay_total = 0
    parsed_pays = []
    for raw in payments:
        code = raw.get("method")
        if code not in methods:
            raise ValueError(f"To'lov turi topilmadi: {code}")
        amount = int(raw.get("amount") or 0)
        if amount <= 0:
            raise ValueError("To'lov summasi musbat bo'lishi kerak")
        pay_total += amount
        parsed_pays.append((methods[code], amount, raw))

    if pay_total != net_total:
        raise ValueError(
            f"To'lovlar chek summasiga teng emas: {pay_total} ≠ {net_total}"
        )

    customer = None
    if data.get("customer_id"):
        customer = Customer.objects.filter(pk=data["customer_id"]).first()

    # Qaytarish qaysi chekdan — asl chekka bog'lanadi
    origin = None
    if kind == Sale.RETURN and data.get("origin_id"):
        origin = Sale.objects.filter(pk=data["origin_id"], kind=Sale.SALE).first()
        # Asl chek mijozi qaytarishga ko'chadi (ball/chegirma to'g'ri bo'lsin)
        if origin and not customer:
            customer = origin.customer

    last = (
        shift.sales.filter(kind=kind).order_by("-number")
        .values_list("number", flat=True).first() or 0
    )

    created_at = parse_datetime(data.get("created_at") or "") or timezone.now()

    sale = Sale.objects.create(
        shift=shift,
        kind=kind,
        number=last + 1,
        local_uuid=local_uuid,
        customer=customer,
        origin=origin,
        created_at=created_at,
        gross_total=int(data.get("gross_total") or lines_total),
        discount_total=int(data.get("discount_total") or 0),
        points_spent=points_spent,
        points_earned=int(data.get("points_earned") or 0),
        net_total=net_total,
    )

    for pos, raw, qty, total in lines:
        product = None
        if raw.get("product_id"):
            product = Product.objects.filter(pk=raw["product_id"]).first()
        SaleItem.objects.create(
            sale=sale,
            position=pos,
            product=product,
            ms_product_id=raw.get("ms_product_id") or (product.ms_id if product else None),
            name=(raw.get("name") or "")[:512],
            barcode=(raw.get("barcode") or "")[:64],
            quantity=qty,
            price=int(raw.get("price") or 0),
            discount=int(raw.get("discount") or 0),
            total=total,
            mark_code=(raw.get("mark_code") or "")[:256],
        )

    for method, amount, raw in parsed_pays:
        Payment.objects.create(
            sale=sale,
            method=method,
            amount=amount,
            tendered=raw.get("tendered"),
            change=raw.get("change"),
        )

    # Chek saqlandi. Tranzaksiya tasdiqlangach — darhol MoySklad'ga
    # yozamiz (fon oqimida). So'rov kutmaydi; cron zaxira bo'lib qoladi.
    sale_id = sale.pk
    transaction.on_commit(
        lambda: threading.Thread(
            target=_push_sale_now, args=(sale_id,), daemon=True
        ).start()
    )

    return JsonResponse({"id": sale.pk, "number": sale.number, "duplicate": False},
                        status=201)
