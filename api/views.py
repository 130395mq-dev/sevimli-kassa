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
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from catalog.models import Barcode, Customer, Product, Stock
from sales.models import (
    BonusEntry,
    BonusProgram,
    CashOperation,
    Cashier,
    POINT_TIYIN,
    Register,
    Payment,
    PaymentMethod,
    Sale,
    SaleItem,
    Shift,
)
from sales import healer
from sales.aloqa import moysklad_health
from sales.services import build_receipt, close_shift, ShiftError
from shared.receipt import render

from .auth import (
    error,
    make_session_token,
    manager_required,
    register_required,
    verify_session_token,
)

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
        login=login, active=True, archived=False
    ).select_related("store").first()

    if not register or not register.check_password(password):
        return error("Login yoki parol noto'g'ri", status=401)

    return JsonResponse({
        "token": register.api_token,
        "register": {"code": register.code, "name": register.name},
        "point": register.point_name,
    })


# ------------------------------------------------------------------- kirish


@csrf_exempt
@require_POST
@register_required
def login(request):
    """Kassaga kirish: login + parol.

    Asosiy yo'l — **kassaning o'z logini va paroli**. Har bir kassa
    panelda yaratiladi va o'z login-parolini oladi; kim o'sha kassada
    ishlasa, o'shani teradi. Alohida «kassirlar» ro'yxati yuritilmaydi:
    do'konda kassa bitta, unda kim turgani smena hisobotidan ko'rinadi.

    Ikki qatlam saqlanadi: qurilma tokeni «bu kassa bizniki» deydi,
    login-parol esa «kirish huquqi bor» deydi. Token o'g'irlansa ham
    parol kerak, parol bilinsa ham kassa yonida turish kerak.

    Eski kassirlar (agar bazada qolgan bo'lsa) ham kira oladi — shu
    tufayli o'tish paytida hech kim ishsiz qolmaydi.

    Xato uchun sabab aytilmaydi («login noto'g'ri» yoki «parol
    noto'g'ri» emas, balki umumiy xabar): aks holda mavjud loginlarni
    bittalab topib olish mumkin bo'lardi.
    """
    reg = request.register
    data = body(request)
    name = (data.get("login") or "").strip().lower()
    # Eski kassalar `pin`, yangilari `password` yuboradi
    secret = (data.get("password") or data.get("pin") or "").strip()

    # 1. Kassaning o'z login-paroli
    if name == (reg.login or "").lower() and reg.check_password(secret):
        return JsonResponse({
            "cashier": {
                "id": 0,
                "name": reg.name,
                "login": reg.login,
                "is_manager": True,
            },
            # Manager-only amallar uchun imzolangan token (mijoz yasay olmaydi)
            "session": make_session_token(0, True),
        })

    # 2. Eski kassir hisobi (o'tish davri uchun)
    cashier = Cashier.objects.filter(login=name, active=True).first()
    if not cashier or not cashier.check_password(secret):
        return error("Login yoki parol noto'g'ri", status=401)

    Cashier.objects.filter(pk=cashier.pk).update(last_login_at=timezone.now())

    return JsonResponse({
        "cashier": {
            "id": cashier.pk,
            "name": cashier.name,
            "login": cashier.login,
            "is_manager": cashier.is_manager,
        },
        "session": make_session_token(cashier.pk, cashier.is_manager),
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
    """Kassa uchun dastur ZIP'ini beradi. Faqat kassa tokeni bilan."""
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
        handle, as_attachment=True, filename=f"SevimliKassa-{rel.version}.zip",
        content_type="application/octet-stream",
    )
    if rel.size:
        resp["Content-Length"] = str(rel.size)
    return resp


# ------------------------------------------------------------------ hello


def _price_types_for(reg, st) -> tuple[list[dict], str]:
    """Kassa uchun narx turlari ro'yxati va asosiysi (id).

    Asosiy tur tanlanish tartibi: kassa sozlamasidagi nom → savdo
    nuqtasiga biriktirilgan tur → nomida «чакана/розничная» bo'lgani →
    birinchisi.
    """
    from catalog.models import PriceType
    from catalog.sync import CatalogSync

    rows = [{"id": str(pt.ms_id).lower(), "name": pt.name} for pt in PriceType.objects.all()]
    if not rows:
        return [], ""

    wanted = (st.price_type or "").strip().lower()
    if wanted:
        for r in rows:
            if r["name"].strip().lower() == wanted:
                return rows, r["id"]
    store_pt = str(reg.store.price_type_ms_id or "").lower()
    if store_pt and any(r["id"] == store_pt for r in rows):
        return rows, store_pt
    for r in rows:
        if any(w in r["name"].lower() for w in CatalogSync.RETAIL_WORDS):
            return rows, r["id"]
    return rows, rows[0]["id"]


@require_GET
@register_required
def hello(request):
    reg = request.register
    shift = reg.shifts.filter(status=Shift.OPEN).first()
    st = reg.settings

    price_types, default_pt = _price_types_for(reg, st)

    # O'z-o'zini davolash (zaxira yozuvchi / katalog) — kassa har 15
    # soniyada keladi, demak server hech qachon «uxlamaydi». 60 soniyada
    # bir marta, fon oqimida — bu so'rovni kutdirmaydi.
    healer.tick()

    return JsonResponse(
        {
            "register": {"code": reg.code, "name": reg.name},
            # «Nuqta» — kassa qaysi ombordan sotadi (MoySklad ombori nomi).
            "point": reg.point_name,
            "market": settings.MARKET_NAME,
            "receipt_width": settings.RECEIPT_WIDTH,
            "server_time": timezone.now().isoformat(),
            "shift": _shift_json(shift) if shift else None,
            "payment_methods": [
                {"code": m.code, "name": m.name, "is_cash": m.is_cash}
                for m in PaymentMethod.objects.filter(active=True)
            ],
            # Kassirlar ro'yxati endi yuritilmaydi: kassaga o'z
            # login-paroli bilan kiriladi. Kalit eski ilovalar uchun
            # qoldirilgan — ular bo'sh ro'yxatni ko'rib kirish oynasini
            # terish rejimida ochadi.
            "cashiers": [],
            # Kassaning sozlamalari — ilova shunga qarab ishlaydi
            # (chegirma chegarasi, majburiy maydonlar, qaytarish va h.k.).
            "settings": st.as_kassa_dict(),
            # Narx turlari (chakana / ulgurji …) va kassaning asosiysi.
            # Kassir ruxsat bo'lsa kassada almashtiradi.
            "price_types": price_types,
            "default_price_type": default_pt,
            # Aloqa chiroqlari: server ↔ MoySklad holati. Kassa buni
            # pastki qatorda dumaloq belgi qilib ko'rsatadi (kassa
            # MoySklad'ga o'zi ulanmaydi — serverdan so'raydi).
            "links": {"moysklad": moysklad_health()},
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

    # Tovarning BARCHA shtrix-kodlari (dona, blok, quti, MoySklad o'zi
    # yaratgani…). Kassa 1.15.0+ «barcodes» ro'yxatini oladi — qaysi kodi
    # skanerlansa ham topadi. «barcode» (bittasi) eski kassalar uchun qoladi.
    codes: dict[int, str] = {}
    all_codes: dict[int, list[str]] = {}
    for b in Barcode.objects.filter(product__in=rows).order_by("pk"):
        codes.setdefault(b.product_id, b.value)
        all_codes.setdefault(b.product_id, []).append(b.value)
    stock = {
        s.product_id: s.quantity
        for s in Stock.objects.filter(
            product__in=rows, store_ms_id=request.register.warehouse_ms_id
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
                    "barcodes": all_codes.get(p.pk, []),
                    "stock": float(stock.get(p.pk, 0)),
                    "prices": p.prices or {},
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
    """Mijozni topish.

    Asosiy yo'l — nakopitelniy karta shtrix-kodi bo'yicha ANIQ moslik:
    `?card=<kod>`. Kassa mijozni faqat shu karta kodini skanerlab topadi;
    telefon yoki ism bo'yicha qidiruv YO'Q (noto'g'ri mijozни biriktirib
    qo'ymaslik uchun). Aniq moslik bo'lgani uchun bitta mijoz qaytadi.

    Eski `?q=` (telefon/karta/ism ichidan) hali qoldirilgan — faqat eski
    kassalar (1.6/1.7) bilan mos ishlash uchun. Yangi kassa uni chaqirmaydi.
    """
    card = (request.GET.get("card") or "").strip()
    if card:
        # Aniq moslik: skaner o'qigan kod discount_card bilan teng bo'lsa.
        c = (
            Customer.objects.filter(archived=False, discount_card=card)
            .first()
        )
        return JsonResponse({"customers": [_customer_dict(c)] if c else []})

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

    # Internetsiz ochilgan smena kassada mahalliy yaratiladi va aloqa
    # tiklanganda shu yerga `local_uuid` bilan keladi. Takroriy yuborishlar
    # (yoki tunab qolgan smena) ikki nusxa yaratmasligi kerak:
    local_uuid = (data.get("local_uuid") or "").strip()
    if local_uuid:
        same = reg.shifts.filter(local_uuid=local_uuid).first()
        if same:
            # Allaqachon ochilgan — o'shani qaytaramiz (yangi yaratmaymiz).
            return JsonResponse({"shift": _shift_json(same)}, status=200)

    open_shift_now = reg.shifts.filter(status=Shift.OPEN).first()
    if open_shift_now:
        # Ochiq smena bor. Onlayn ochishda bu xato; ammo internetsiz
        # ochilgan smenani sinxronlashda (local_uuid bilan) — bu shunchaki
        # «allaqachon ochiq», o'shani qabul qilamiz, tarixi buzilmaydi.
        if local_uuid:
            if not open_shift_now.local_uuid:
                open_shift_now.local_uuid = local_uuid
                open_shift_now.save(update_fields=["local_uuid"])
            return JsonResponse({"shift": _shift_json(open_shift_now)}, status=200)
        return error("Bu kassada ochiq smena bor", status=409)

    # Ombor tanlanmagan bo'lsa savdo MoySklad'ga yozilmaydi — smenani
    # umuman ochmaymiz, aks holda kun oxirida «cheklar ketmabdi» bo'ladi.
    if not reg.warehouse_ms_id:
        return error(
            f"«{reg.name}» uchun ombor tanlanmagan. "
            "Panel → Kassalar → Sozlash → Tovarlar bo'limida omborni tanlang.",
            status=409,
        )

    # Smenani kim ochgani hisobotda ko'rinib tursin. Kassaning o'z
    # login-paroli bilan kirilsa — kassaning nomi yoziladi. Eski
    # kassirlar hisobi qolgan bo'lsa, o'shaning ismi.
    cashier_ref = None
    cashier_id = data.get("cashier_id")
    if cashier_id:
        cashier_ref = Cashier.objects.filter(pk=cashier_id, active=True).first()
    name = (
        (cashier_ref.name if cashier_ref else "")
        or (data.get("cashier") or "").strip()
        or reg.name
    )

    # Internetsiz ochilgan bo'lsa — o'sha vaqtni saqlaymiz (hisobot to'g'ri
    # bo'lsin), bo'lmasa hozirgi vaqt.
    opened_at = parse_datetime(data.get("opened_at") or "") or timezone.now()
    opening_cash = max(0, int(data.get("opening_cash") or 0))
    try:
        with transaction.atomic():
            # Registerni bloklaymiz — parallel so'rovlar shu yerda navbatga
            # turadi. Lock olgach QAYTA tekshiramiz: shu tufayli bir kassada
            # bir vaqtda ikkita ochiq smena yoki takroriy raqam yaratilmaydi.
            Register.objects.select_for_update().get(pk=reg.pk)

            if local_uuid:
                same = reg.shifts.filter(local_uuid=local_uuid).first()
                if same:
                    return JsonResponse({"shift": _shift_json(same)}, status=200)

            open_now = reg.shifts.filter(status=Shift.OPEN).first()
            if open_now:
                if local_uuid:
                    if not open_now.local_uuid:
                        open_now.local_uuid = local_uuid
                        open_now.save(update_fields=["local_uuid"])
                    return JsonResponse({"shift": _shift_json(open_now)}, status=200)
                return error("Bu kassada ochiq smena bor", status=409)

            last = (reg.shifts.order_by("-number")
                    .values_list("number", flat=True).first() or 0)
            shift = Shift.objects.create(
                register=reg,
                number=last + 1,
                cashier=name,
                cashier_ref=cashier_ref,
                opened_at=opened_at,
                opening_cash=opening_cash,
                local_uuid=local_uuid,
            )
    except IntegrityError:
        # Poyga: bir xil local_uuid yoki (register, number) to'qnashuvi.
        # Mavjud smenani qaytaramiz — ikki nusxa yaratmaymiz.
        if local_uuid:
            same = reg.shifts.filter(local_uuid=local_uuid).first()
            if same:
                return JsonResponse({"shift": _shift_json(same)}, status=200)
        open_now = reg.shifts.filter(status=Shift.OPEN).first()
        if open_now:
            return JsonResponse({"shift": _shift_json(open_now)}, status=200)
        raise
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
@manager_required
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

    # Menejer huquqi (chegirma chegarasini oshirishga ruxsat) — imzolangan
    # X-Session tokeni bilan. Mijozning «men managerman» so'ziga ISHONMAYMIZ.
    minfo = verify_session_token((request.headers.get("X-Session") or "").strip())
    manager_ok = bool(minfo and minfo.get("is_manager"))

    try:
        return _save_sale(shift, data, items, payments, local_uuid, manager_ok)
    except ValueError as e:
        return error(str(e))
    except IntegrityError:
        # Bir vaqtda kelgan bir xil local_uuid — birinchisi yozib ulgurdi.
        # Ikkinchisiga o'shaning javobini qaytaramiz (idempotent, xato emas).
        existing = Sale.objects.filter(local_uuid=local_uuid).first()
        if existing:
            return JsonResponse(
                {"id": existing.pk, "number": existing.number, "duplicate": True}
            )
        raise


@transaction.atomic
def _save_sale(shift, data, items, payments, local_uuid, manager_ok=False):
    # Smena qatorini bloklaymiz — bir smenaga bir vaqtda kelgan ikki chek
    # (parallel kassa yoki qayta yuborish) bir xil tartib raqamini olmasin.
    # PostgreSQL'da bu row-lock; SQLite testida e'tiborsiz, lekin zararsiz.
    shift = Shift.objects.select_for_update().get(pk=shift.pk)

    kind = data.get("kind") or Sale.SALE
    if kind not in (Sale.SALE, Sale.RETURN):
        raise ValueError("kind noto'g'ri")

    # Chegirma chegarasi — kassa sozlamasidan. Kassa o'zi ham tekshiradi,
    # lekin bu YETARLI EMAS: buzilgan/soxta kassa istalgan chegirmani
    # yuborishi mumkin. Shuning uchun server ham tekshiradi. Menejer tokeni
    # (X-Session) bo'lsa — chegaradan oshishga ruxsat (masalan aksiya).
    rs = shift.register.settings
    allow_discount = bool(rs.allow_discount)
    try:
        max_discount = Decimal(str(rs.max_discount or 0))
    except (InvalidOperation, TypeError):
        max_discount = Decimal(0)

    # Qatorlar summasi — SERVER TOMONIDAN tekshiriladi. Mijoz yuborgan
    # summaga ko'r-ko'rona ishonmaymiz: har qatorda `total` narx×miqdordan
    # (brutto) oshmasligi (chegirma faqat kamaytiradi) va manfiy bo'lmasligi
    # shart. Bu — soxta (shishirilgan yoki manfiy) summani bloklaydi.
    lines = []
    lines_total = 0
    gross_sum = 0
    for pos, raw in enumerate(items, start=1):
        try:
            qty = Decimal(str(raw.get("quantity", "1")))
        except (InvalidOperation, TypeError):
            raise ValueError(f"{pos}-qatorda miqdor noto'g'ri")
        if qty <= 0:
            raise ValueError(f"{pos}-qatorda miqdor musbat bo'lishi kerak")

        price = int(raw.get("price") or 0)
        total = int(raw.get("total") or 0)
        if price < 0 or total < 0:
            raise ValueError(f"{pos}-qatorda manfiy qiymat")

        gross_line = int(
            (Decimal(price) * qty).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        if total > gross_line:
            raise ValueError(
                f"{pos}-qator summasi narx×miqdordan katta: {total} > {gross_line}"
            )

        # Chegirma chegarasi — faqat savdoda (qaytarishda tekshirmaymiz).
        # Menejer ruxsati bo'lsa o'tkazamiz.
        if kind == Sale.SALE and not manager_ok and gross_line > 0:
            disc = gross_line - total
            if disc > 0 and not allow_discount:
                raise ValueError(f"{pos}-qatorda chegirmaga ruxsat yo'q")
            # Chegirma foizi chegaradan oshmasin (1 tiyin yaxlitlash yo'li bilan)
            limit = (Decimal(gross_line) * max_discount / 100)
            if Decimal(disc) - limit > 1:
                raise ValueError(
                    f"{pos}-qatorda chegirma chegaradan oshdi "
                    f"(eng ko'p {max_discount}%)"
                )

        # Soft signal: mijoz narxi katalogdagidan past bo'lsa — log qilamiz
        # (offline narx eskirishi qonuniy bo'lishi mumkin, rad etmaymiz).
        pid = raw.get("product_id")
        if pid:
            cat = (Product.objects.filter(pk=pid)
                   .values_list("sale_price", flat=True).first())
            if cat and price < int(cat):
                logger.warning(
                    "Narx katalogdan past: kassa=%s tovar=%s narx=%s katalog=%s",
                    shift.register_id, pid, price, cat,
                )

        lines_total += total
        gross_sum += gross_line
        lines.append((pos, raw, qty, total))

    # ---- Mijoz va qaytariladigan asl chek (ball uchun QULFLAB olamiz) ----
    program = BonusProgram.get()

    origin = None
    customer_id = data.get("customer_id")
    if kind == Sale.RETURN:
        # Qaytarish — FAQAT shu kassaning asl chekiga (IDOR oldi olinadi).
        oid = data.get("origin_id")
        if oid:
            origin = (
                Sale.objects.select_related("customer")
                .filter(pk=oid, kind=Sale.SALE, shift__register=shift.register)
                .first()
            )
            if not origin:
                raise ValueError("Qaytariladigan asl chek topilmadi")
            if not customer_id and origin.customer_id:
                customer_id = origin.customer_id

    customer = None
    if customer_id:
        # QULF: bir mijozga bir vaqtda kelgan ikki savdo balansni buzmasin
        # (ball ikki marta sarflanmasin). PostgreSQL'da row-lock.
        customer = Customer.objects.select_for_update().filter(pk=customer_id).first()

    # ---- Qaytarishni asl chek bilan solishtirish (cheksiz over-refund oldi)
    if kind == Sale.RETURN:
        _check_return_against_origin(origin, lines, lines_total)

    # ---- Ball SARFLASH (server tekshiradi, klientga ishonmaymiz) ----
    points_spent = int(data.get("points_spent") or 0)
    if points_spent < 0:
        raise ValueError("Ball manfiy bo'lishi mumkin emas")
    if points_spent > 0:
        if kind == Sale.RETURN:
            raise ValueError("Qaytarishda ball sarflab bo'lmaydi")
        if not (program.active and program.redeem_enabled):
            raise ValueError("Bonus dasturi ball to'lovini qabul qilmayapti")
        if not customer:
            raise ValueError("Ball sarflash uchun mijoz tanlanishi kerak")
        if points_spent > customer.bonus_points:
            raise ValueError(
                f"Mijozda yetarli ball yo'q: {customer.bonus_points} ta bor, "
                f"{points_spent} ta so'ralyapti"
            )
        max_pts = (lines_total * program.max_redeem_percent) // 100 // POINT_TIYIN
        if points_spent > max_pts:
            raise ValueError(
                f"Ball bilan eng ko'p {program.max_redeem_percent}% to'lash mumkin "
                f"(bu chekda {max_pts} ball)"
            )

    net_total = lines_total - points_spent * POINT_TIYIN
    if net_total < 0:
        raise ValueError("Chek summasi manfiy bo'lib qoldi (ball summadan katta)")

    # ---- Ball BERISH — SERVER hisoblaydi (klient sonига ishonmaymiz) ----
    if kind == Sale.SALE and program.active and customer:
        points_earned = program.earn_for(net_total)
    else:
        points_earned = 0

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

    # (customer va origin yuqorida — qulflab — allaqachon aniqlandi)

    last = (
        shift.sales.filter(kind=kind).order_by("-number")
        .values_list("number", flat=True).first() or 0
    )

    created_at = parse_datetime(data.get("created_at") or "") or timezone.now()

    # gross_total va discount_total ni ham SERVER hisoblaydi (klientga
    # ishonmaymiz) — aks holda Z-hisobot va balans tekshiruvi buzilardi.
    sale = Sale.objects.create(
        shift=shift,
        kind=kind,
        number=last + 1,
        local_uuid=local_uuid,
        customer=customer,
        origin=origin,
        created_at=created_at,
        price_type=str(data.get("price_type") or "")[:64],
        gross_total=gross_sum,
        discount_total=max(0, gross_sum - lines_total),
        points_spent=points_spent,
        points_earned=points_earned,
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

    # ---- BALL: haqiqatan yechamiz/beramiz va reyestrga yozamiz ----
    # customer QULFLANGAN (select_for_update), shuning uchun ikki savdo
    # bir vaqtda balansni buzolmaydi.
    new_balance = customer.bonus_points if customer else 0
    if customer:
        if kind == Sale.SALE:
            if points_spent > 0:
                new_balance -= points_spent
                _bonus_log(customer, sale, BonusEntry.SPEND, -points_spent,
                           new_balance, "Savdoda sarflandi")
            if points_earned > 0:
                new_balance += points_earned
                _bonus_log(customer, sale, BonusEntry.EARN, points_earned,
                           new_balance, "Savdoda berildi")
        elif kind == Sale.RETURN and origin is not None:
            new_balance = _reverse_return_bonus(origin, sale, customer, net_total)

        if new_balance != customer.bonus_points:
            customer.bonus_points = new_balance
            customer.save(update_fields=["bonus_points"])

    # Chek saqlandi. Tranzaksiya tasdiqlangach — darhol MoySklad'ga
    # yozamiz (fon oqimida). So'rov kutmaydi; cron zaxira bo'lib qoladi.
    sale_id = sale.pk
    transaction.on_commit(
        lambda: threading.Thread(
            target=_push_sale_now, args=(sale_id,), daemon=True
        ).start()
    )

    return JsonResponse(
        {
            "id": sale.pk,
            "number": sale.number,
            "duplicate": False,
            "points_earned": points_earned,
            "points_spent": points_spent,
            "customer_balance": new_balance if customer else None,
        },
        status=201,
    )


# ------------------------------------------------------------------ ball yordamchilari


def _bonus_log(customer, sale, kind, delta, balance_after, comment=""):
    """Ball harakatini reyestrga yozadi."""
    BonusEntry.objects.create(
        customer=customer, sale=sale, kind=kind, delta=delta,
        balance_after=balance_after, comment=comment,
    )


def _check_return_against_origin(origin, lines, refund_total):
    """Qaytarishni asl chek bilan solishtiradi — cheksiz qaytarishni bloklaydi.

    Tekshiradi: (1) qaytariladigan summa asl chek summasidan (avval
    qaytarilganini hisobga olib) oshmasin. Bu — bitta chekni bir necha
    marta qaytarib pul yechib olishning oldini oladi.
    """
    if origin is None:
        # Asl cheksiz qaytarish — MVP'da ruxsat (offline yoki eski chek),
        # lekin summa manfiy emasligi baribir yuqorida tekshirilgan.
        return
    already = (
        Sale.objects.filter(origin=origin, kind=Sale.RETURN)
        .aggregate(s=Sum("net_total"))["s"] or 0
    )
    if already + refund_total > origin.net_total:
        qoldi = max(0, origin.net_total - already)
        raise ValueError(
            f"Qaytarish asl chekdan oshib ketdi. Bu chekdan yana "
            f"{qoldi // 100} so'm qaytarish mumkin"
        )


def _reverse_return_bonus(origin, sale, customer, refund_net):
    """Qaytarishda ballni teskari aylantiradi (asl chekka mutanosib).

    Asl chekda ball berilgan bo'lsa — o'shancha (mutanosib) qaytarib olamiz;
    ball sarflangan bo'lsa — o'shancha mijozga qaytaramiz. Bir necha qismli
    qaytarishda ham jami asl chek balларidan oshmaydi (kümülатив klamp).
    """
    balance = customer.bonus_points
    if origin.net_total <= 0 or (not origin.points_earned and not origin.points_spent):
        return balance

    # Shu asl chek bo'yicha jami qaytarilgan summa (bu qaytarish bilan)
    prior = (
        Sale.objects.filter(origin=origin, kind=Sale.RETURN)
        .exclude(pk=sale.pk)
        .aggregate(s=Sum("net_total"))["s"] or 0
    )
    frac_now = min(Decimal(1), Decimal(prior + refund_net) / Decimal(origin.net_total))
    frac_prior = min(Decimal(1), Decimal(prior) / Decimal(origin.net_total))

    def _slice(total_points):
        cum_now = int((Decimal(total_points) * frac_now).to_integral_value(ROUND_HALF_UP))
        cum_prior = int((Decimal(total_points) * frac_prior).to_integral_value(ROUND_HALF_UP))
        return cum_now - cum_prior

    earn_back = _slice(origin.points_earned)   # berilganni qaytarib olamiz (-)
    spend_back = _slice(origin.points_spent)    # sarflaganini qaytaramiz (+)

    if earn_back > 0:
        balance -= earn_back
        _bonus_log(customer, sale, BonusEntry.RETURN, -earn_back, balance,
                   f"Qaytarish: berilgan ball qaytarib olindi (asl #{origin.number})")
    if spend_back > 0:
        balance += spend_back
        _bonus_log(customer, sale, BonusEntry.RETURN, spend_back, balance,
                   f"Qaytarish: sarflangan ball qaytarildi (asl #{origin.number})")
    return balance


# ------------------------------------------- versiya chiqarish (skript uchun)


@csrf_exempt
@require_POST
def release_upload(request):
    """Yangi kassa versiyasini panelga yuklaydi — brauzersiz, bitta so'rovda.

    Nega alohida yo'l kerak: panelning «Versiyalar» sahifasi BRAUZER uchun
    qilingan — u login sessiyasi va CSRF tokenini talab qiladi. Skript
    (curl) orqali yuklashda CSRF doim muammo tug'diradi: token login paytida
    yangilanadi, yo'naltirishda eskiradi, katta fayl ichidan o'qilmaydi.

    Shuning uchun bu yerda oddiy MAXFIY KALIT ishlatiladi — RELEASE_UPLOAD_TOKEN
    (Railway muhit o'zgaruvchisi). Kalit bo'lmasa bu yo'l butunlay yopiq.

    So'rov:
        POST /api/v1/release/upload
        X-Release-Token: <maxfiy kalit>
        multipart: file=<SevimliKassa.zip>, version=1.11.0,
                   notes=<izoh>, mandatory=1|0

    Javob: {"ok": true, "version": "1.11.0", "size": …, "sha256": …}
    """
    import hashlib
    import hmac as _hmac

    from sales.models import KassaRelease, version_key

    secret = (getattr(settings, "RELEASE_UPLOAD_TOKEN", "") or "").strip()
    if not secret:
        return error("Serverda RELEASE_UPLOAD_TOKEN sozlanmagan", status=503)
    got = (request.headers.get("X-Release-Token") or "").strip()
    if not got or not _hmac.compare_digest(got, secret):
        logger.warning("Versiya yuklash: kalit noto'g'ri")
        return error("Kalit noto'g'ri", status=403)

    version = (request.POST.get("version") or "").strip().lstrip("vV")
    notes = (request.POST.get("notes") or "").strip()
    mandatory = str(request.POST.get("mandatory") or "").lower() in {
        "1", "true", "yes", "ha",
    }
    upload = request.FILES.get("file")

    # Tekshiruvlar — panel sahifasidagi bilan bir xil qoidalar
    if not version or version_key(version) == (0, 0, 0):
        return error("Versiya raqami kerak, masalan 1.2.0")
    if KassaRelease.objects.filter(version=version).exists():
        return error(f"{version} allaqachon yuklangan")
    latest = KassaRelease.latest()
    if latest and version_key(version) <= latest.key:
        return error(
            f"Versiya {latest.version} dan katta bo'lishi kerak "
            f"(kassalar faqat kattasini oladi)"
        )
    if not upload:
        return error("Fayl yuborilmadi")
    if not upload.name.lower().endswith(".zip"):
        return error("Faqat .zip fayl qabul qilinadi")
    if upload.size < 1_000_000:
        return error("Fayl juda kichik — bu dastur ZIP emas")

    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)

    rel = KassaRelease(
        version=version, notes=notes, mandatory=mandatory,
        size=upload.size, sha256=digest.hexdigest(),
    )
    rel.file.save(f"SevimliKassa-{version}.zip", upload, save=True)
    logger.info("Yangi versiya chiqarildi (skript orqali): %s", version)
    return JsonResponse({
        "ok": True,
        "version": version,
        "size": upload.size,
        "sha256": rel.sha256,
        "mandatory": mandatory,
    })
