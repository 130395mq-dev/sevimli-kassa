"""
Nuqtalar paneli.

Bu ekran MoySklad'ning «Точки продаж» ekranining o'rnini bosadi. Savol
o'sha edi: kassalar o'z dasturimizga o'tgach, kunlik savdoni qayerdan
ko'raman? Javob — shu yerdan.

Uchta narsa ko'rinadi, muhimlik tartibida:

1. **Diqqat talab qiladiganlar** — tiqilib qolgan cheklar, aloqasi
   uzilgan kassalar. Bular tepada, chunki ular haqida bugun bir narsa
   qilish kerak.
2. **Bugungi savdo** — nuqta va kassa bo'yicha, naqd va naqdsiz ajratib.
3. **Fon** — katalog sinxronizatsiyasi, bonus majburiyati.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.text import slugify

from catalog.models import Customer, Product, SyncState
from sales import aloqa, healer, selftest, sender
from sales.models import (
    BonusEntry, BonusProgram, MoySkladCheck, Payment, PaymentMethod, POINT_TIYIN,
    Register, Sale, Shift,
)
from sales.services import build_receipt
from shared.receipt import render as render_receipt

# Kassa shuncha vaqt jim tursa — aloqa uzilgan deb hisoblaymiz.
# Kassa har daqiqada bir marta ko'rinadi, shuning uchun 5 daqiqa
# tasodifiy uzilish emas.
OFFLINE_AFTER = timedelta(minutes=5)


def _free_login(base: str) -> str:
    """Band bo'lmagan login: «chilonzor», bo'lmasa «chilonzor-2» …"""
    base = (base or "kassa")[:56]
    if not Register.objects.filter(login=base).exists():
        return base
    n = 2
    while Register.objects.filter(login=f"{base}-{n}").exists():
        n += 1
    return f"{base}-{n}"


def health(request):
    """Railway va monitoring uchun — tez va yengil."""
    return JsonResponse({"status": "ok"})


@login_required
def aloqa_json(request):
    """Panel tepasidagi aloqa chiroqlari — sahifa 15 soniyada bir so'raydi.

    Sahifa qayta yuklanmaydi (formalar to'ldirilayotgan bo'lishi mumkin),
    faqat chiroqlarning rangi va izohi yangilanadi.
    """
    healer.tick()
    return JsonResponse(aloqa.snapshot())


def day_start():
    """Bugungi kun boshi — mahalliy vaqt bo'yicha."""
    now = timezone.localtime()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


@login_required
def points(request):
    # «Qayta yuborish» — tiqilib qolgan (stuck) cheklarni navbatga
    # qaytaradi. Sabab tuzatilgach (masalan MoySklad sozlamasi) shu tugma
    # bosiladi; yozuvchi 20 soniyada bir navbatni oladi.
    if request.method == "POST" and request.POST.get("action") == "retry_stuck":
        n = sender.requeue_stuck()
        if n:
            messages.success(
                request,
                f"{n} ta chek navbatga qaytarildi — bir daqiqa ichida MoySklad'ga "
                "yozilishga qayta uriniladi. Yana tiqilsa, sababi shu jadvalda chiqadi.",
            )
        else:
            messages.info(request, "Tiqilib qolgan chek yo'q.")
        return redirect("dashboard:points")

    # «Hozir tekshirish» — MoySklad sinovini shu zahoti yurgizadi
    # (10–20 soniya). Sinov hujjatlari «проведён» qilinmaydi va o'chiriladi —
    # savdoga ta'sir yo'q (sales/selftest.py).
    if request.method == "POST" and request.POST.get("action") == "selftest":
        try:
            check = selftest.run_selftest(MoySkladCheck.MANUAL)
        except selftest.SelfTestBusy:
            messages.info(request, "Sinov allaqachon ketmoqda — bir daqiqadan keyin qarang.")
            return redirect("dashboard:points")
        if check.ok:
            messages.success(request, f"MoySklad sinovi o'tdi — {len(check.steps)} bosqich, hammasi joyida.")
        else:
            messages.error(request, "MoySklad sinovi O'TMADI — sababi pastdagi «MoySklad tekshiruvi» jadvalida.")
        return redirect("dashboard:points")

    today = day_start()
    now = timezone.now()

    sales_today = Sale.objects.filter(kind=Sale.SALE, created_at__gte=today)

    # --- kassalar bo'yicha
    rows = []
    for reg in Register.objects.filter(active=True).select_related("store"):
        shift = reg.shifts.filter(status=Shift.OPEN).first()
        mine = sales_today.filter(shift__register=reg)
        agg = mine.aggregate(n=Count("id"), total=Sum("net_total"))

        cash = (
            Payment.objects.filter(sale__in=mine, method__is_cash=True)
            .aggregate(t=Sum("amount"))["t"] or 0
        )
        total = agg["total"] or 0

        offline = not reg.last_seen_at or (now - reg.last_seen_at) > OFFLINE_AFTER

        rows.append({
            "register": reg,
            "point": reg.point_name,
            "shift": shift,
            "offline": offline,
            # Aloqa chirog'i (yashil/sariq/qizil) — tepadagi qator bilan
            # bir xil hisob, JS 15 soniyada bir yangilab turadi.
            "link": aloqa.register_state(reg, now, shift_open=shift is not None),
            "last_seen": reg.last_seen_at,
            "receipts": agg["n"] or 0,
            "total": total / 100,
            "cash": cash / 100,
            "cashless": (total - cash) / 100,
            "pending": mine.exclude(sync_status=Sale.SENT).count(),
        })

    # --- kunlik yakun
    day = sales_today.aggregate(n=Count("id"), total=Sum("net_total"))
    day_cash = (
        Payment.objects.filter(sale__in=sales_today, method__is_cash=True)
        .aggregate(t=Sum("amount"))["t"] or 0
    )
    day_total = day["total"] or 0

    # --- to'lov turlari bo'yicha
    by_method = (
        Payment.objects.filter(sale__in=sales_today)
        .values("method__name", "method__is_cash")
        .annotate(total=Sum("amount"), n=Count("id"))
        .order_by("-total")
    )

    # --- diqqat talab qiladiganlar
    stuck = (
        Sale.objects.filter(sync_status=Sale.STUCK)
        .select_related("shift__register__store")
        .order_by("-created_at")[:20]
    )
    stuck_count = Sale.objects.filter(sync_status=Sale.STUCK).count()
    queued = Sale.objects.filter(
        sync_status__in=[Sale.NEW, Sale.FAILED]
    ).count()
    check = MoySkladCheck.latest()

    # Ogohlantirishlar — har biri «nima bo'ldi» + «nima qilish kerak».
    # Tizim o'zi hal qiladiganini o'zi qiladi (healer/selftest); bu yerda
    # odam nimani bilishi va (kerak bo'lsa) qilishi yoziladi.
    snap = aloqa.snapshot(now)
    alerts = []
    if stuck_count:
        alerts.append({
            "level": "err", "title": f"{stuck_count} ta chek MoySklad'ga yozilmadi",
            "text": "Sabab pastdagi «Yozilmagan cheklar» jadvalida.",
            "fix": aloqa.FIX_STUCK, "action": "retry_stuck",
        })
    if check is not None and check.finished_at and check.failed_steps:
        alerts.append({
            "level": "err", "title": "MoySklad sinovi o'tmadi — " + check.summary,
            "text": "Kassa yozadigan hujjatlardan birini MoySklad hisobi qabul qilmayapti. "
                    "Tafsilot pastdagi «MoySklad tekshiruvi» jadvalida. Sinov har 30 daqiqada "
                    "qayta o'tadi; tuzalgach tiqilganlar o'zi qayta yuboriladi.",
            "fix": aloqa._fix_for_step(check.failed_steps[0]),
        })
    ms = snap["moysklad"]
    if ms["state"] != "ok" and not ms["text"].startswith("sinov o'tmadi") and "tiqilib" not in ms["text"]:
        alerts.append({
            "level": "err" if ms["state"] == "bad" else "warn",
            "title": "MoySklad — " + ms["text"], "text": "", "fix": ms.get("fix", ""),
        })
    for r in snap["registers"]:
        if r["state"] in ("bad", "warn"):
            alerts.append({
                "level": "err" if r["state"] == "bad" else "warn",
                "title": f"{r['name']} — {r['text']}", "text": "", "fix": r.get("fix", ""),
            })

    bonus_total = (
        Customer.objects.filter(archived=False)
        .aggregate(t=Sum("bonus_points"))["t"] or 0
    )

    return render(request, "dashboard/points.html", {
        "alerts": alerts,
        "rows": rows,
        "day": {
            "receipts": day["n"] or 0,
            "total": day_total / 100,
            "cash": day_cash / 100,
            "cashless": (day_total - day_cash) / 100,
        },
        "by_method": [
            {
                "name": m["method__name"],
                "is_cash": m["method__is_cash"],
                "total": (m["total"] or 0) / 100,
                "n": m["n"],
            }
            for m in by_method
        ],
        "stuck": stuck,
        "stuck_count": stuck_count,
        # MoySklad o'z-o'zini tekshirish — oxirgi natija
        "check": check,
        "queued": queued,
        "sync_rows": SyncState.objects.order_by("entity"),
        "products": Product.objects.filter(archived=False).count(),
        "customers": Customer.objects.filter(archived=False).count(),
        "bonus_total": bonus_total,
        "today": today,
    })


@login_required
def shifts(request):
    """Smenalar ro'yxati — MoySklad'dagi «Смены» o'rniga.

    Sahifalab beriladi: do'kon bir yil ishlaganda smenalar yuzlab bo'ladi,
    hammasini bitta sahifada ko'rsatish sekin va o'qib bo'lmas edi.
    """
    qs = (
        Shift.objects.select_related("register__store")
        .annotate(
            receipts=Count("sales", filter=Q(sales__kind=Sale.SALE)),
            total=Sum("sales__net_total", filter=Q(sales__kind=Sale.SALE)),
        )
        .order_by("-opened_at")
    )
    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get("page"))
    # Tiyinni so'mga — shabloni bo'lish amali yo'q
    for s in page.object_list:
        s.total_sum = (s.total or 0) / 100
    return render(request, "dashboard/shifts.html", {
        "rows": page.object_list,
        "page": page,
    })


@login_required
def shift_detail(request, pk: int):
    """Bitta smena — kassirning ko'rgan chekining aynan o'zi."""
    try:
        shift = Shift.objects.select_related("register__store").get(pk=pk)
    except Shift.DoesNotExist:
        raise Http404("Smena topilmadi")

    receipt = build_receipt(shift)
    return render(request, "dashboard/shift_detail.html", {
        "shift": shift,
        "receipt_text": render_receipt(receipt),
        "sales": shift.sales.order_by("number").select_related("customer"),
    })


@login_required
def registers(request):
    """Kassalar: nom, login, parol.

    Kassa ilovasi shu login-parol bilan ulanadi va tokenni o'zi oladi.
    Xodim uzun tokenni ko'rmaydi.
    """
    from catalog.models import RetailStore, Warehouse
    from sales.models import RegisterSettings, new_api_token

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "create":
            # Yagona majburiy narsa — ombor. Qolganini o'zimiz to'ldiramiz:
            # boshqaruvchi omborni tanlaydi, panel unga tayyor login va
            # parol beradi, u monoblokka o'shani teradi. Xohlasa o'zi ham
            # yozishi mumkin.
            warehouse = Warehouse.objects.filter(
                ms_id=request.POST.get("warehouse") or None
            ).first()
            name = (request.POST.get("name") or "").strip()
            login = slugify(request.POST.get("login") or "")
            password = (request.POST.get("password") or "").strip()

            if warehouse:
                if not name:
                    n = Register.objects.filter(
                        settings_row__warehouse_ms_id=warehouse.ms_id
                    ).count() + 1
                    name = f"Kassa-{n}"
                if not login:
                    login = _free_login(slugify(warehouse.name) or "kassa")
                if not password:
                    password = f"{secrets.randbelow(900000) + 100000}"

            if not warehouse:
                messages.error(request, "Ombor (sklad) tanlanmadi")
            elif len(password) < 4:
                messages.error(request, "Parol kamida 4 belgi bo'lishi kerak")
            elif Register.objects.filter(login=login).exists():
                messages.error(request, f"«{login}» logini band")
            else:
                # Savdo nuqtasi — MoySklad'da shu omborga bog'langani
                # (bo'lsa). Kassa uchun asosiysi ombor, nuqta esa eski
                # hisobotlar bilan bog'lanish uchun.
                store = (
                    RetailStore.objects.filter(store_ms_id=warehouse.ms_id).first()
                    or RetailStore.objects.filter(active=True).first()
                )
                code = slugify(f"{warehouse.name}-{name}")[:32]
                register = Register(
                    code=code, name=name, store=store, login=login,
                )
                register.set_password(password)
                register.save()
                RegisterSettings.objects.update_or_create(
                    register=register,
                    defaults={"warehouse_ms_id": warehouse.ms_id,
                              "warehouse": warehouse.name},
                )
                messages.success(
                    request,
                    f"{name} tayyor — «{warehouse.name}» omboridan sotadi. "
                    f"Monoblokda: login «{login}», parol «{password}».",
                )

        elif action == "password":
            password = (request.POST.get("password") or "").strip()
            reg = Register.objects.filter(pk=request.POST.get("id")).first()
            if not reg:
                messages.error(request, "Kassa topilmadi")
            elif len(password) < 4:
                messages.error(request, "Parol kamida 4 belgi bo'lishi kerak")
            else:
                reg.set_password(password)
                reg.save(update_fields=["password_hash"])
                # Parol endi jadvalda ko'rinmaydi (xavfsizlik) — shuning
                # uchun bu yerda bir marta ko'rsatamiz. Yozib oling.
                messages.success(
                    request,
                    f"{reg.name}: yangi parol «{password}». "
                    "Bu parol boshqa ko'rsatilmaydi — monoblokka kiriting.",
                )

        elif action == "rotate":
            reg = Register.objects.filter(pk=request.POST.get("id")).first()
            if reg:
                reg.api_token = new_api_token()
                reg.save(update_fields=["api_token"])
                messages.success(
                    request,
                    f"{reg.name}: aloqa uzildi. Kassa ilovasida login va "
                    "parol bilan qayta ulanish kerak.",
                )

        elif action == "delete":
            # Smenasi bor kassani BUTUNLAY o'chirib bo'lmaydi (Shift.register —
            # PROTECT): unga bog'langan smenalar, cheklar va Z-hisobotlar —
            # savdo tarixi, yo'qotib bo'lmaydi. Shuning uchun kassa
            # ARXIVLANADI: ro'yxatdan yo'qoladi va kira olmaydi, lekin bazada
            # butun qoladi. Kerak bo'lsa arxivdan qaytariladi.
            reg = Register.objects.filter(pk=request.POST.get("id")).first()
            if not reg:
                messages.error(request, "Kassa topilmadi")
            else:
                shifts_count = reg.shifts.count()
                if shifts_count:
                    reg.archived = True
                    reg.active = False
                    reg.save(update_fields=["archived", "active"])
                    messages.success(
                        request,
                        f"«{reg.name}» ro'yxatdan olib tashlandi. Savdo tarixi "
                        f"({shifts_count} ta smena) saqlanib qoldi — kerak "
                        "bo'lsa pastdagi «Arxiv» dan qaytarasiz.",
                    )
                else:
                    label = reg.name
                    reg.delete()
                    messages.success(request, f"«{label}» o'chirildi.")

        elif action == "restore":
            reg = Register.objects.filter(pk=request.POST.get("id")).first()
            if not reg:
                messages.error(request, "Kassa topilmadi")
            else:
                reg.archived = False
                reg.save(update_fields=["archived"])
                messages.success(
                    request,
                    f"«{reg.name}» arxivdan qaytarildi. Ishlashi uchun uni "
                    "«Yoqish» tugmasi bilan yoqing.",
                )

        elif action == "toggle":
            reg = Register.objects.filter(pk=request.POST.get("id")).first()
            if reg:
                reg.active = not reg.active
                reg.save(update_fields=["active"])
                messages.success(
                    request,
                    f"{reg.name}: " + ("yoqildi" if reg.active else "bloklandi"),
                )

        return redirect("dashboard:registers")

    from catalog.models import Warehouse
    from sales.models import KassaRelease, version_key

    latest = KassaRelease.latest()
    # Arxivlangan («o'chirilgan») kassalar ro'yxatda ko'rinmaydi.
    # ?arxiv=1 bilan ularni ko'rish va qaytarish mumkin.
    show_archived = request.GET.get("arxiv") == "1"
    qs = Register.objects.select_related("store", "settings_row")
    rows = list(qs if show_archived else qs.filter(archived=False))
    archived_count = Register.objects.filter(archived=True).count()
    warehouses = list(Warehouse.objects.filter(archived=False))
    now = timezone.now()
    for r in rows:
        r.wh_name = r.warehouse_name
        # Aloqa chirog'i — kassa ↔ server (JS 15 soniyada bir yangilaydi)
        r.link = aloqa.register_state(r, now)
        # Panelda: yashil — eng yangi, sariq — eskirgan, kulrang — noma'lum
        if not r.app_version:
            r.version_state = "off"
        elif latest and version_key(r.app_version) < latest.key:
            r.version_state = "warn"
        else:
            r.version_state = "ok"

    return render(request, "dashboard/registers.html", {
        "rows": rows,
        "warehouses": warehouses,
        "latest": latest,
        "show_archived": show_archived,
        "archived_count": archived_count,
    })


@login_required
def prices(request):
    """Qaysi kassa qaysi narxda sotadi — chakana yoki ulgurji.

    Ilgari buni kassir kassaning o'zida almashtirardi. Bu xato manbai
    edi: chakana mijozga ulgurji narx berib yuborish uchun bitta noto'g'ri
    bosish yetardi va buni faqat kun oxirida sezilardi.

    Endi narxni shu yerdan biriktiriladi. Kassada tugma umuman
    ko'rinmaydi — kassir narxni o'zgartira olmaydi.
    """
    from catalog.models import PriceType
    from sales.models import RegisterSettings

    types = list(PriceType.objects.all())

    if request.method == "POST":
        changed = 0
        for reg in Register.objects.all():
            value = request.POST.get(f"pt-{reg.pk}")
            if value is None:
                continue
            st = reg.settings
            if st.price_type != value or st.allow_price_type_switch:
                st.price_type = value
                # Kassada almashtirish tugmasi chiqmasin
                st.allow_price_type_switch = False
                st.save(update_fields=["price_type", "allow_price_type_switch"])
                changed += 1
        messages.success(
            request,
            f"{changed} ta kassaning narxi yangilandi. Kassalar bir daqiqada oladi."
            if changed else "O'zgarish yo'q.",
        )
        return redirect("dashboard:prices")

    rows = []
    for reg in (Register.objects.select_related("settings_row")
                .filter(archived=False).order_by("name")):
        st = reg.settings
        rows.append({
            "reg": reg,
            "current": (st.price_type or "").strip(),
            "warehouse": reg.warehouse_name,
        })

    return render(request, "dashboard/prices.html", {
        "rows": rows,
        "types": types,
    })


@login_required
def releases(request):
    """Kassa ilovasining versiyalari.

    Yangi SevimliKassa.exe shu yerdan yuklanadi. Kassalar o'zi tekshirib
    (ochilganda va har 30 daqiqada) yuklab oladi va o'zini almashtiradi.
    «Majburiy» belgilansa — kassir «Keyinroq» deya olmaydi.
    """
    import hashlib

    from sales.models import KassaRelease, version_key

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "upload":
            version = (request.POST.get("version") or "").strip().lstrip("vV")
            notes = (request.POST.get("notes") or "").strip()
            mandatory = bool(request.POST.get("mandatory"))
            upload = request.FILES.get("file")

            latest = KassaRelease.latest()
            if not version or version_key(version) == (0, 0, 0):
                messages.error(request, "Versiya raqami kerak, masalan 1.2.0")
            elif KassaRelease.objects.filter(version=version).exists():
                messages.error(request, f"{version} allaqachon yuklangan")
            elif latest and version_key(version) <= latest.key:
                messages.error(
                    request,
                    f"Versiya {latest.version} dan katta bo'lishi kerak "
                    f"(kassalar faqat kattasini oladi)",
                )
            elif not upload:
                messages.error(request, "SevimliKassa.zip faylini tanlang")
            elif not upload.name.lower().endswith(".zip"):
                messages.error(
                    request,
                    "Faqat .zip fayl qabul qilinadi "
                    "(dist\\SevimliKassa.zip — EXE-YASASH dan keyin)",
                )
            elif upload.size < 1_000_000:
                messages.error(request, "Fayl juda kichik — bu dastur ZIP emas")
            else:
                digest = hashlib.sha256()
                for chunk in upload.chunks():
                    digest.update(chunk)
                rel = KassaRelease(
                    version=version, notes=notes, mandatory=mandatory,
                    size=upload.size, sha256=digest.hexdigest(),
                )
                rel.file.save(f"SevimliKassa-{version}.zip", upload, save=True)
                messages.success(
                    request,
                    f"Versiya {version} chiqarildi. Kassalar 30 daqiqa ichida "
                    + ("majburiy yangilanadi." if mandatory else "taklif oladi."),
                )

        elif action == "toggle":
            rel = KassaRelease.objects.filter(pk=request.POST.get("id")).first()
            if rel:
                rel.active = not rel.active
                rel.save(update_fields=["active"])
                messages.success(
                    request,
                    f"{rel.version}: " + ("yoqildi" if rel.active else "o'chirildi"),
                )

        elif action == "mandatory":
            rel = KassaRelease.objects.filter(pk=request.POST.get("id")).first()
            if rel:
                rel.mandatory = not rel.mandatory
                rel.save(update_fields=["mandatory"])
                messages.success(
                    request,
                    f"{rel.version}: " + ("majburiy" if rel.mandatory else "ixtiyoriy"),
                )

        elif action == "delete":
            rel = KassaRelease.objects.filter(pk=request.POST.get("id")).first()
            if rel:
                try:
                    rel.file.delete(save=False)
                except Exception:
                    pass
                rel.delete()
                messages.success(request, f"{rel.version} o'chirildi")

        return redirect("dashboard:releases")

    latest = KassaRelease.latest()
    rows = list(KassaRelease.objects.all())
    rows.sort(key=lambda r: r.key, reverse=True)

    # Qaysi kassa qaysi versiyada — bir qarashda
    regs = Register.objects.select_related("store").filter(active=True)
    outdated = []
    for r in regs:
        if latest and (not r.app_version or version_key(r.app_version) < latest.key):
            outdated.append(r)

    return render(request, "dashboard/releases.html", {
        "rows": rows,
        "latest": latest,
        "registers": regs,
        "outdated": outdated,
    })


# Sozlash oynasidagi checkbox maydonlar — hammasi shu yerda ro'yxatda.
# Yangi checkbox qo'shsangiz — shu ro'yxatga qo'shing, boshqa joyni
# o'zgartirish shart emas.
_SETTINGS_BOOLS = [
    "enabled",
    "allow_choose_cashier",
    "allow_price_edit", "allow_price_type_switch",
    "allow_delete_line", "allow_discount",
    "allow_create_product", "track_stock", "track_reserves",
    "add_customers_to_groups", "upload_customers_offline",
    "req_fio", "req_phone", "req_card", "req_email", "req_birthday", "req_gender",
    "require_fiscal_receipt", "test_print_modes",
    "autoprint_nonfiscal", "autoprint_fiscal",
    "shift_create_incoming_cashless", "shift_create_cash_order",
    "allow_returns_closed_shift", "allow_returns_no_reason",
    "orders_enabled", "allow_advances", "allow_certificates",
    "credit_sales_enabled", "expense_receipts_enabled",
]
_SETTINGS_TEXT = [
    "bank_account", "address", "access_group",
    "price_type", "card_acquirer", "qr_acquirer",
    "sales_channel", "sales_prefix_1c",
]
#: MoySklad bog'lanishlari — ro'yxatdan tanlanadi, matn emas.
#: (maydon nomi, model, nom uchun matn maydoni)
_SETTINGS_MS = [
    ("warehouse_ms_id", "Warehouse", "warehouse"),
    ("organization_ms_id", "Organization", "organization"),
]
_SETTINGS_DECIMAL = ["max_discount", "card_commission"]
_SETTINGS_CHOICE = ["show_product_groups", "show_customer_groups"]


@login_required
def register_edit(request, pk: int):
    """Bitta kassani sozlash — MoySklad «Точка продаж» tahrirlash oynasi.

    Chap tomonda bo'limlar ro'yxati, o'ngda «Точка продаж» kartasi,
    o'rtada bo'limlar. Har bir bo'lim — o'sha ekrandagidek.
    """
    from decimal import Decimal, InvalidOperation

    from catalog.models import RetailStore
    from sales.models import RegisterSettings

    try:
        reg = Register.objects.select_related("store").get(pk=pk)
    except Register.DoesNotExist:
        raise Http404("Kassa topilmadi")

    st, _ = RegisterSettings.objects.get_or_create(register=reg)

    if request.method == "POST":
        for name in _SETTINGS_BOOLS:
            setattr(st, name, bool(request.POST.get(name)))
        for name in _SETTINGS_TEXT:
            setattr(st, name, (request.POST.get(name) or "").strip())
        for name in _SETTINGS_DECIMAL:
            raw = (request.POST.get(name) or "0").replace(",", ".").strip()
            try:
                setattr(st, name, Decimal(raw))
            except InvalidOperation:
                setattr(st, name, Decimal("0"))
        for name in _SETTINGS_CHOICE:
            val = request.POST.get(name)
            if val in (RegisterSettings.GROUP_ALL, RegisterSettings.GROUP_SELECTED):
                setattr(st, name, val)

        # Ombor va tashkilot — MoySklad ro'yxatidan
        import catalog.models as cm

        for field, model_name, text_field in _SETTINGS_MS:
            raw = (request.POST.get(field) or "").strip()
            obj = getattr(cm, model_name).objects.filter(ms_id=raw).first() if raw else None
            setattr(st, field, obj.ms_id if obj else None)
            setattr(st, text_field, obj.name if obj else "")

        # Kassa nomini ham shu yerdan o'zgartirish mumkin
        new_name = (request.POST.get("register_name") or "").strip()
        if new_name:
            reg.name = new_name
        reg.active = st.enabled

        # Savdo nuqtasi (haqiqiy MoySklad do'koni). Kassa shu do'kon
        # nomidan MoySklad'ga yozadi — «Sinov filiali» dan haqiqiyga
        # o'tkazish shu yerdan.
        store_id = request.POST.get("store")
        if store_id:
            new_store = RetailStore.objects.filter(pk=store_id, active=True).first()
            if new_store:
                reg.store = new_store

        reg.save(update_fields=["name", "active", "store"])

        st.save()

        messages.success(request, f"{reg.name}: sozlamalar saqlandi")
        return redirect("dashboard:register-edit", pk=reg.pk)

    # Do'konlar ro'yxati: MoySklad'dan kelgan haqiqiy do'konlar (org id si
    # bor). Joriy do'kon ro'yxatda bo'lmasa (masalan «Sinov filiali») —
    # uni ham qo'shamiz, tanlangani ko'rinib tursin.
    real_stores = list(
        RetailStore.objects.filter(active=True, organization_ms_id__isnull=False)
        .order_by("name")
    )
    if reg.store_id and all(s.pk != reg.store_id for s in real_stores):
        real_stores.insert(0, reg.store)

    from catalog.models import Organization, PriceType, Warehouse

    return render(request, "dashboard/register_edit.html", {
        "reg": reg,
        "st": st,
        "stores": real_stores,
        "price_types": PriceType.objects.all(),
        "store_price_type": reg.store.price_type_name if reg.store_id else "",
        "warehouses": Warehouse.objects.filter(archived=False),
        "organizations": Organization.objects.filter(archived=False),
        "warehouse_id": str(st.warehouse_ms_id or ""),
        "organization_id": str(st.organization_ms_id or ""),
        "effective_warehouse": reg.warehouse_name,
    })


# ---------------------------------------------------------------- o'rnatish


def installer(request):
    """Yangi kassaga dasturni o'rnatish sahifasi — kirishsiz ochiladi.

    Yangi monoblok keldi. Unda hech narsa yo'q: na dastur, na sozlama.
    Brauzerni ochib shu manzilni yozadi, ZIP ni yuklab oladi, ochadi
    (Extract) va ichidagi SevimliKassa.exe ni ishga tushiradi — dastur
    o'zini %LOCALAPPDATA%\\SevimliKassa ga ko'chiradi, yorliq yasaydi va
    login/parol so'raydi. Boshqa hech narsa kerak emas.

    Kirish talab qilinmaydi: faylning o'zi hech narsa ochmaydi —
    login va parolsiz kassa ishga tushmaydi.
    """
    from sales.models import KassaRelease

    rel = KassaRelease.latest()
    return render(request, "dashboard/installer.html", {
        "rel": rel,
        "host": request.get_host(),
    })


def installer_download(request):
    """Eng yangi dastur ZIP'ini beradi."""
    from django.http import FileResponse

    from sales.models import KassaRelease

    rel = KassaRelease.latest()
    if not rel or not rel.file:
        raise Http404("Hali versiya chiqarilmagan")
    return FileResponse(
        rel.file.open("rb"),
        as_attachment=True,
        filename="SevimliKassa.zip",
        content_type="application/octet-stream",
    )


# ============================================================ SEVIMLI BONUS


def _moysklad_client():
    """MoySklad klienti — token bo'lsa. Bo'lmasa None."""
    from django.conf import settings as s
    token = getattr(s, "MOYSKLAD_TOKEN", "")
    if not token:
        return None
    from moysklad.client import MoySkladClient
    return MoySkladClient(token=token)


@login_required
def bonus(request):
    """SEVIMLI BONUS — o'zimizning bonus dasturi.

    MoySklad Отгрузка yo'lida ballni hisoblamaydi, shuning uchun ballni
    shu yerda — o'z bazamizda — yuritamiz. Sahifada: dastur holati,
    sozlamalar, ishga tushirish (MoySklad balanslarini olib), mijoz
    balanslari va qo'lda tuzatish.
    """
    program = BonusProgram.get()

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "activate":
            client = _moysklad_client()
            pulled = 0
            if client is not None:
                try:
                    from catalog.sync import CatalogSync
                    pulled = CatalogSync(client).force_import_bonus()
                except Exception as e:  # pull ishlamasa ham yoqamiz — balanslar bazada bor
                    messages.error(request, f"MoySklad'dan olishda xato: {e}")
            program.active = True
            program.activated_at = timezone.now()
            program.save(update_fields=["active", "activated_at"])
            messages.success(
                request,
                "SEVIMLI BONUS yoqildi. " +
                (f"MoySklad'dan {pulled} ta balans yangilandi. "
                 if client is not None else
                 "MoySklad tokeni yo'q — balanslar bazadagidek qoldi. ") +
                "Endi ballni faqat biz yuritamiz.",
            )
            return redirect("dashboard:bonus")

        if action == "reimport":
            client = _moysklad_client()
            if client is None:
                messages.error(request, "MoySklad tokeni sozlanmagan")
            else:
                try:
                    from catalog.sync import CatalogSync
                    n = CatalogSync(client).force_import_bonus()
                    messages.success(
                        request,
                        f"MoySklad'dan {n} ta balans qayta olindi. "
                        "Diqqat: lokal balanslar MoySklad qiymatiga tenglandi.",
                    )
                except Exception as e:
                    messages.error(request, f"Xato: {e}")
            return redirect("dashboard:bonus")

        if action == "deactivate":
            program.active = False
            program.save(update_fields=["active"])
            messages.success(
                request,
                "SEVIMLI BONUS o'chirildi. Ehtiyot bo'ling: o'chiq bo'lsa "
                "ball berilmaydi va MoySklad balansi qayta yozila boshlaydi.",
            )
            return redirect("dashboard:bonus")

        if action == "settings":
            try:
                program.earn_percent = _dec(request.POST.get("earn_percent"), program.earn_percent)
                program.max_redeem_percent = max(0, min(100, int(request.POST.get("max_redeem_percent") or 100)))
                program.redeem_enabled = request.POST.get("redeem_enabled") == "on"
                program.save(update_fields=["earn_percent", "max_redeem_percent", "redeem_enabled"])
                messages.success(request, "Sozlamalar saqlandi")
            except (ValueError, TypeError):
                messages.error(request, "Qiymatlar noto'g'ri")
            return redirect("dashboard:bonus")

        if action == "adjust":
            cust = Customer.objects.filter(pk=request.POST.get("customer_id")).first()
            try:
                delta = int(request.POST.get("delta") or 0)
            except (ValueError, TypeError):
                delta = 0
            reason = (request.POST.get("reason") or "").strip()[:256]
            if not cust:
                messages.error(request, "Mijoz topilmadi")
            elif delta == 0:
                messages.error(request, "Ball miqdori kiritilmadi (+ yoki −)")
            elif cust.bonus_points + delta < 0:
                messages.error(request, "Balans manfiy bo'lib qolardi")
            else:
                from django.db import transaction as tx
                with tx.atomic():
                    c = Customer.objects.select_for_update().get(pk=cust.pk)
                    c.bonus_points += delta
                    c.save(update_fields=["bonus_points"])
                    BonusEntry.objects.create(
                        customer=c, kind=BonusEntry.ADJUST, delta=delta,
                        balance_after=c.bonus_points,
                        comment=reason or "Paneldan qo'lda tuzatish",
                    )
                messages.success(
                    request,
                    f"{cust.name}: {'+' if delta > 0 else ''}{delta} ball. "
                    f"Yangi balans: {c.bonus_points} ball",
                )
            return redirect("dashboard:bonus")

    # --- GET: holat + statistika + mijozlar ro'yxati
    stats = Customer.objects.filter(bonus_points__gt=0).aggregate(
        holders=Count("id"), total=Sum("bonus_points"),
    )
    q = (request.GET.get("q") or "").strip()
    customers = Customer.objects.filter(archived=False)
    if q:
        customers = customers.filter(
            Q(name__icontains=q) | Q(phone__icontains=q) | Q(discount_card__icontains=q)
        )
    else:
        customers = customers.filter(bonus_points__gt=0)
    customers = customers.order_by("-bonus_points", "name")

    page = Paginator(customers, 50).get_page(request.GET.get("page"))
    recent = BonusEntry.objects.select_related("customer")[:25]

    return render(request, "dashboard/bonus.html", {
        "program": program,
        "holders": stats["holders"] or 0,
        "total_points": stats["total"] or 0,
        "page": page,
        "q": q,
        "recent": recent,
        "has_token": _moysklad_client() is not None,
    })


@login_required
def customer_bonus(request, pk: int):
    """Bitta mijozning ball tarixi (reyestr)."""
    cust = Customer.objects.filter(pk=pk).first()
    if not cust:
        raise Http404("Mijoz topilmadi")
    entries = cust.bonus_entries.select_related("sale")[:200]
    return render(request, "dashboard/customer_bonus.html", {
        "cust": cust, "entries": entries,
    })


def _dec(value, default):
    from decimal import Decimal, InvalidOperation
    try:
        d = Decimal(str(value))
        return d if d >= 0 else default
    except (InvalidOperation, TypeError):
        return default


@login_required
def payment_methods(request):
    """To'lov turlari — kassadagi to'lov tugmalari.

    Kassa qaysi tugmalarni ko'rsatishini shu yer belgilaydi: bu yerdagi
    FAOL turlar to'lov oynasida chiqadi. Tur o'chirilsa kassada tugma
    yo'qoladi, lekin ESKI savdolar tegilmaydi — shuning uchun tur
    o'chirilmaydi, faqat yashiriladi (savdo tarixi buzilmasin).

    MoySklad hisob raqami (ixtiyoriy): naqdsiz pul MoySklad'da qaysi
    hisobga tushishini belgilaydi. Bo'sh bo'lsa — tashkilotning ASOSIY
    hisobiga tushadi, ya'ni pul yo'qolmaydi.
    """
    if request.method == "POST":
        action = request.POST.get("action")

        if action == "create":
            name = (request.POST.get("name") or "").strip()
            is_cash = bool(request.POST.get("is_cash"))
            code = slugify(request.POST.get("code") or name)[:32]
            if len(name) < 2:
                messages.error(request, "Nom kamida 2 harf bo'lishi kerak")
            elif not code:
                messages.error(request, "Kod yaratilmadi — nomni lotin harflarida yozing")
            elif PaymentMethod.objects.filter(code=code).exists():
                messages.error(request, f"«{code}» kodi band")
            else:
                last = (PaymentMethod.objects.order_by("-sort")
                        .values_list("sort", flat=True).first() or 0)
                PaymentMethod.objects.create(
                    code=code, name=name, is_cash=is_cash, sort=last + 1,
                )
                messages.success(
                    request,
                    f"«{name}» qo'shildi. Kassalar bir daqiqada oladi.",
                )

        elif action == "rename":
            m = PaymentMethod.objects.filter(pk=request.POST.get("id")).first()
            name = (request.POST.get("name") or "").strip()
            if not m:
                messages.error(request, "To'lov turi topilmadi")
            elif len(name) < 2:
                messages.error(request, "Nom kamida 2 harf bo'lishi kerak")
            else:
                old = m.name
                m.name = name
                m.save(update_fields=["name"])
                messages.success(request, f"«{old}» → «{name}»")

        elif action == "toggle":
            m = PaymentMethod.objects.filter(pk=request.POST.get("id")).first()
            if not m:
                messages.error(request, "To'lov turi topilmadi")
            elif m.active and PaymentMethod.objects.filter(active=True).count() <= 1:
                # Hech bo'lmasa bitta to'lov turi qolishi shart, aks holda
                # kassada to'lov qilib bo'lmaydi.
                messages.error(
                    request, "Oxirgi to'lov turini o'chirib bo'lmaydi",
                )
            else:
                m.active = not m.active
                m.save(update_fields=["active"])
                messages.success(
                    request,
                    f"«{m.name}»: " + (
                        "kassada ko'rinadi" if m.active
                        else "kassadan olib tashlandi"
                    ),
                )

        return redirect("dashboard:payment-methods")

    rows = list(PaymentMethod.objects.all())
    return render(request, "dashboard/payment_methods.html", {
        "rows": rows,
        "active_count": sum(1 for m in rows if m.active),
    })
