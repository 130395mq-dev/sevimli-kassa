"""
Panelga kirish huquqi: egasi va market boshqaruvchisi.

Egasining so'rovi (2026-10-09): boshqa market boshqaruvchisi panelga o'z
login-paroli bilan kirsin — faqat o'z marketining savdosi, smenalari,
cheklari va tovarlarini ko'rsin; boshqa marketlarni ko'rmasin, sozlamalarni
o'zgartira olmasin.

Ikki xil foydalanuvchi:

* **egasi** — `PanelManager` yozuvi yo'q har qanday foydalanuvchi. Hammasini
  ko'radi va o'zgartiradi (avvalgidek, hech narsa o'zgarmagan);
* **boshqaruvchi** — `PanelManager` yozuvi bor. Uning marketi — kassalar
  sotadigan MoySklad ombori; o'sha omborga biriktirilgan kassalar
  (arxivlanganlari ham — eski savdosi ham o'sha marketniki) uning
  «doirasi».

Himoya ikki qavat:

1. `PanelAccessMiddleware` — boshqaruvchiga faqat `MANAGER_PAGES` dagi
   sahifalar ochiq, faqat ko'rish (GET). Qolgan hamma sahifa — jumladan
   kelajakda qo'shiladiganlari ham — avtomatik yopiq (403).
2. Ochiq sahifalardagi so'rovlar `sales()`, `shifts()`, `registers()` …
   orqali olinadi: boshqaruvchi uchun ular faqat uning kassalari bilan
   cheklanadi, egasi uchun esa `Sale.objects` ning aynan o'zi.
"""

from __future__ import annotations

from contextvars import ContextVar

from django.shortcuts import render
from django.urls import Resolver404, resolve

from sales.models import PanelManager, Payment, Register, Sale, SaleItem, Shift

#: Joriy so'rov doirasi: None — hammasi (egasi), aks holda kassa id'lari.
_SCOPE: ContextVar[frozenset[int] | None] = ContextVar("panel_scope", default=None)

#: Boshqaruvchiga ochiq sahifalar (URL nomlari). Ro'yxatda yo'q sahifa —
#: yopiq. Yangi sahifa qo'shilsa, u ham avtomatik yopiq bo'ladi.
MANAGER_PAGES = frozenset({
    "login", "logout",
    "dashboard:points",            # bosh sahifa — o'z marketi savdosi
    "dashboard:batafsil",          # karta bosilganda ochiladigan tafsilot
    "dashboard:shifts",
    "dashboard:shift-detail",
    "dashboard:receipt",
    "dashboard:top-products",
    "dashboard:falling-products",
    "dashboard:running-out",
    "dashboard:aloqa-json",
    "dashboard:health",
})

#: POST faqat kirish/chiqish uchun — boshqaruvchi hech narsani o'zgartirmaydi.
_MANAGER_POST = frozenset({"login", "logout"})


def manager_of(user) -> PanelManager | None:
    """Foydalanuvchi boshqaruvchi bo'lsa — uning yozuvi, egasi bo'lsa None."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    cached = getattr(user, "_panel_manager_cache", False)
    if cached is not False:
        return cached
    try:
        manager = user.panel_manager
    except PanelManager.DoesNotExist:
        manager = None
    user._panel_manager_cache = manager
    return manager


def registers_of(warehouse_ms_id) -> frozenset[int]:
    """Shu omborga biriktirilgan hamma kassa (arxivlanganlari ham).

    `Register.warehouse_ms_id` bilan aynan bir xil qoida (kassaning o'z
    sozlamasi, bo'lmasa savdo nuqtasining ombori), lekin har so'rovda
    kassa sozlamasini yaratib yurmaydi va bitta so'rov bilan o'qiydi.
    """
    from sales.models import RegisterSettings

    want = str(warehouse_ms_id)
    own = dict(RegisterSettings.objects.values_list("register_id", "warehouse_ms_id"))
    out = set()
    for reg in Register.objects.select_related("store"):
        wh = own.get(reg.pk) or (reg.store.warehouse_ms_id if reg.store_id else None)
        if wh and str(wh) == want:
            out.add(reg.pk)
    return frozenset(out)


def register_ids() -> frozenset[int] | None:
    """Joriy so'rovda ko'rinadigan kassalar: None — hammasi (egasi)."""
    return _SCOPE.get()


def is_scoped() -> bool:
    return _SCOPE.get() is not None


def _limit(qs, field: str):
    ids = _SCOPE.get()
    return qs if ids is None else qs.filter(**{f"{field}__in": ids})


def sales():
    return _limit(Sale.objects.all(), "shift__register_id")


def shifts():
    return _limit(Shift.objects.all(), "register_id")


def registers():
    return _limit(Register.objects.all(), "pk")


def items():
    return _limit(SaleItem.objects.all(), "sale__shift__register_id")


def payments():
    return _limit(Payment.objects.all(), "sale__shift__register_id")


def forbidden(request):
    return render(request, "dashboard/taqiq.html", status=403)


class PanelAccessMiddleware:
    """Boshqaruvchi: faqat ochiq sahifalar, faqat ko'rish, faqat o'z marketi."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        manager = manager_of(getattr(request, "user", None))
        if manager is None:
            return self.get_response(request)
        # Doira 403 sahifasi uchun ham o'rnatiladi: uning tepasidagi aloqa
        # chiroqlari ham faqat o'z kassalarini ko'rsatsin.
        token = _SCOPE.set(registers_of(manager.warehouse_ms_id))
        try:
            try:
                name = resolve(request.path_info).view_name
            except Resolver404:
                return self.get_response(request)
            if name not in MANAGER_PAGES:
                return forbidden(request)
            if request.method not in ("GET", "HEAD") and name not in _MANAGER_POST:
                return forbidden(request)
            return self.get_response(request)
        finally:
            _SCOPE.reset(token)


def context(request):
    """Shablonlar uchun: `panel_manager` — boshqaruvchi bo'lsa uning yozuvi."""
    return {"panel_manager": manager_of(getattr(request, "user", None))}
