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

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.text import slugify

from catalog.models import Customer, Product, SyncState
from sales.models import Cashier, Payment, Register, Sale, Shift
from sales.services import build_receipt
from shared.receipt import render as render_receipt

# Kassa shuncha vaqt jim tursa — aloqa uzilgan deb hisoblaymiz.
# Kassa har daqiqada bir marta ko'rinadi, shuning uchun 5 daqiqa
# tasodifiy uzilish emas.
OFFLINE_AFTER = timedelta(minutes=5)


def health(request):
    """Railway va monitoring uchun — tez va yengil."""
    return JsonResponse({"status": "ok"})


def day_start():
    """Bugungi kun boshi — mahalliy vaqt bo'yicha."""
    now = timezone.localtime()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


@login_required
def points(request):
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
            "point": reg.store.name,
            "shift": shift,
            "offline": offline,
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
    queued = Sale.objects.filter(
        sync_status__in=[Sale.NEW, Sale.FAILED]
    ).count()

    bonus_total = (
        Customer.objects.filter(archived=False)
        .aggregate(t=Sum("bonus_points"))["t"] or 0
    )

    return render(request, "dashboard/points.html", {
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
        "stuck_count": Sale.objects.filter(sync_status=Sale.STUCK).count(),
        "queued": queued,
        "offline_count": sum(1 for r in rows if r["offline"]),
        "sync_rows": SyncState.objects.order_by("entity"),
        "products": Product.objects.filter(archived=False).count(),
        "customers": Customer.objects.filter(archived=False).count(),
        "bonus_total": bonus_total,
        "today": today,
    })


@login_required
def shifts(request):
    """Smenalar ro'yxati — MoySklad'dagi «Смены» o'rniga."""
    rows = (
        Shift.objects.select_related("register__store")
        .annotate(
            receipts=Count("sales", filter=Q(sales__kind=Sale.SALE)),
            total=Sum("sales__net_total", filter=Q(sales__kind=Sale.SALE)),
        )
        .order_by("-opened_at")[:100]
    )
    # Tiyinni so'mga — shabloni bo'lish amali yo'q
    data = []
    for s in rows:
        s.total_sum = (s.total or 0) / 100
        data.append(s)
    return render(request, "dashboard/shifts.html", {"rows": data})


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
def cashiers(request):
    """Kassirlar — yaratish, PIN almashtirish, o'chirish.

    Kassirlar Django foydalanuvchisi emas: ular panelga kirmaydi.
    Shuning uchun bu yerda o'z ekrani bor, Django admin emas —
    do'kon boshqaruvchisi Django admin bilan ishlamasligi kerak.
    """
    if request.method == "POST":
        action = request.POST.get("action")

        if action == "create":
            name = (request.POST.get("name") or "").strip()
            pin = (request.POST.get("pin") or "").strip()
            login = slugify(request.POST.get("login") or name) or ""

            if not name or not login:
                messages.error(request, "Ism kiritilmadi")
            elif not pin.isdigit() or not (4 <= len(pin) <= 6):
                messages.error(request, "PIN 4–6 ta raqamdan iborat bo'lishi kerak")
            elif Cashier.objects.filter(login=login).exists():
                messages.error(request, f"«{login}» logini band")
            else:
                cashier = Cashier(
                    name=name, login=login,
                    is_manager=bool(request.POST.get("is_manager")),
                )
                cashier.set_pin(pin)
                cashier.save()
                messages.success(request, f"{name} qo'shildi. Login: {login}")

        elif action == "pin":
            pin = (request.POST.get("pin") or "").strip()
            cashier = Cashier.objects.filter(pk=request.POST.get("id")).first()
            if not cashier:
                messages.error(request, "Kassir topilmadi")
            elif not pin.isdigit() or not (4 <= len(pin) <= 6):
                messages.error(request, "PIN 4–6 ta raqam bo'lishi kerak")
            else:
                cashier.set_pin(pin)
                cashier.save(update_fields=["pin_hash"])
                messages.success(request, f"{cashier.name}: PIN almashtirildi")

        elif action == "toggle":
            cashier = Cashier.objects.filter(pk=request.POST.get("id")).first()
            if cashier:
                cashier.active = not cashier.active
                cashier.save(update_fields=["active"])
                messages.success(
                    request,
                    f"{cashier.name}: " + ("yoqildi" if cashier.active else "o'chirildi"),
                )

        return redirect("dashboard:cashiers")

    return render(request, "dashboard/cashiers.html", {
        "rows": Cashier.objects.annotate(shift_count=Count("shifts")),
    })


@login_required
def registers(request):
    """Kassalar: nom, login, parol.

    Kassa ilovasi shu login-parol bilan ulanadi va tokenni o'zi oladi.
    Xodim uzun tokenni ko'rmaydi.
    """
    from catalog.models import RetailStore
    from sales.models import new_api_token

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "create":
            name = (request.POST.get("name") or "").strip()
            login = slugify(request.POST.get("login") or "")
            password = (request.POST.get("password") or "").strip()
            store = RetailStore.objects.filter(
                pk=request.POST.get("store")
            ).first()

            if not name or not login:
                messages.error(request, "Nom va login kerak")
            elif len(password) < 4:
                messages.error(request, "Parol kamida 4 belgi bo'lishi kerak")
            elif not store:
                messages.error(request, "Savdo nuqtasi tanlanmadi")
            elif Register.objects.filter(login=login).exists():
                messages.error(request, f"«{login}» logini band")
            else:
                code = slugify(f"{store.name}-{name}")[:32]
                register = Register(
                    code=code, name=name, store=store, login=login,
                )
                register.set_password(password)
                register.save()
                messages.success(
                    request,
                    f"{name} qo'shildi. Kassa ilovasida login «{login}» "
                    "va shu parolni kiriting.",
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
                messages.success(request, f"{reg.name}: parol almashtirildi")

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

        return redirect("dashboard:registers")

    return render(request, "dashboard/registers.html", {
        "rows": Register.objects.select_related("store"),
        "stores": RetailStore.objects.filter(active=True),
    })


# Sozlash oynasidagi checkbox maydonlar — hammasi shu yerda ro'yxatda.
# Yangi checkbox qo'shsangiz — shu ro'yxatga qo'shing, boshqa joyni
# o'zgartirish shart emas.
_SETTINGS_BOOLS = [
    "enabled",
    "allow_choose_cashier",
    "allow_price_edit",
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
    "organization", "bank_account", "address", "access_group",
    "price_type", "warehouse", "card_acquirer", "qr_acquirer",
    "sales_channel", "sales_prefix_1c",
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

        # Kassa nomini ham shu yerdan o'zgartirish mumkin
        new_name = (request.POST.get("register_name") or "").strip()
        if new_name:
            reg.name = new_name
        reg.active = st.enabled
        reg.save(update_fields=["name", "active"])

        st.save()

        # Kassirlar — belgilanganlari
        ids = request.POST.getlist("cashiers")
        st.allowed_cashiers.set(Cashier.objects.filter(pk__in=ids))

        messages.success(request, f"{reg.name}: sozlamalar saqlandi")
        return redirect("dashboard:register-edit", pk=reg.pk)

    return render(request, "dashboard/register_edit.html", {
        "reg": reg,
        "st": st,
        "cashiers": Cashier.objects.filter(active=True),
        "allowed_ids": set(st.allowed_cashiers.values_list("pk", flat=True)),
    })
