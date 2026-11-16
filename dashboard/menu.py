"""Panelning tepa menyusi: bo'limlar va ularning sahifalari (faqat ko'rinish).

Egasining so'rovi (2026-11-15): «panel ichi professional ERP dasturlardek tartibga
olinsin». Menyu endi bo'limlarga ajratilgan — Savdo, Tovarlar, Mijozlar, Nazorat,
Sozlash — va tepada turadi; tanlangan bo'limning sahifalari ikkinchi qatorda chiqadi.

Kim nimani ko'rishi shu yerda QAYTA yozilmagan: `dashboard/access.py` dagi
`MANAGER_PAGES` va `TOVAROVED_PAGES` ro'yxatlaridan olinadi. Shu sababli menyu hech
qachon ochilmaydigan (403) sahifaga havola bermaydi va ruxsat ro'yxati o'zgarsa o'zi
moslanadi.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.urls import NoReverseMatch, Resolver404, resolve, reverse

from dashboard import access


@dataclass(frozen=True)
class Item:
    title: str
    name: str                     # URL nomi, masalan "dashboard:shifts"
    also: tuple[str, ...] = ()    # shu banddan ochiladigan ichki sahifalar (smena, chek …)
    prefix: str = ""              # shu nom fazosidagi hamma sahifa (masalan "kpi:")


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    icon: str                     # base.html dagi ikonka nomi (#i-<icon>)
    items: tuple[Item, ...]


#: Egasi ko'radigan to'liq menyu. Boshqaruvchiga shundan faqat ruxsat etilganlari qoladi.
MENU: tuple[Section, ...] = (
    Section("home", "Bosh sahifa", "home", (
        Item("Bosh sahifa", "dashboard:points",
             also=("dashboard:batafsil", "dashboard:avg-target", "dashboard:monthly")),
    )),
    Section("savdo", "Savdo", "chart", (
        Item("Smenalar", "dashboard:shifts", also=("dashboard:shift-detail",)),
        Item("Cheklar", "dashboard:receipts", also=("dashboard:receipt",)),
    )),
    Section("tovarlar", "Tovarlar", "box", (
        Item("Top tovarlar", "dashboard:top-products"),
        Item("Tugayotganlar", "dashboard:running-out"),
        Item("Tushgan tovarlar", "dashboard:falling-products"),
        Item("Ko'chirish tavsiyasi", "dashboard:transfer-advice"),
        Item("Sotuv narxi", "dashboard:prices"),
    )),
    Section("mijozlar", "Mijozlar", "users", (
        Item("SEVIMLI BONUS", "dashboard:bonus",
             also=("dashboard:customer-bonus", "dashboard:bonus-receipts")),
        Item("Mijozlarga SMS", "sms:bosh"),
    )),
    Section("nazorat", "Nazorat", "shield", (
        Item("Tovarovedlar", "kpi:bosh", prefix="kpi:"),
    )),
    Section("sozlash", "Sozlash", "gear", (
        Item("Kassalar", "dashboard:registers", also=("dashboard:register-edit",)),
        Item("To'lov turlari", "dashboard:payment-methods"),
        Item("Boshqaruvchilar", "dashboard:managers"),
        Item("Versiyalar", "dashboard:releases"),
    )),
)

#: Tovaroved menyusi: ikki band, ichki sahifalar KPI yorliqlarida (kpi/_tabs.html).
TOVAROVED_MENU: tuple[Section, ...] = (
    Section("reyting", "Reyting", "shield", (
        Item("Reyting", "kpi:bosh", also=("kpi:minuslar", "kpi:minus", "kpi:qayta")),
    )),
    Section("men", "Mening ishim", "user", (
        Item("Mening ishim", "kpi:men"),
    )),
)


def _current(request) -> tuple[str, dict]:
    """Joriy sahifaning URL nomi va parametrlari ("" — aniqlanmadi)."""
    match = getattr(request, "resolver_match", None)
    if match is None:  # middleware 403 sahifasini URL aniqlanmasdan oldin chizadi
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return "", {}
    return match.view_name or "", dict(match.kwargs or {})


def build(request, manager=None, tovaroved=None) -> dict:
    """Shablon uchun menyu.

    Qaytaradi: {"sections": [{key, title, icon, url, on, items: [{title, url, on}]}],
                "sub": tanlangan bo'lim sahifalari (bittadan ko'p bo'lsa), "sub_title": bo'lim nomi}.
    """
    current, kwargs = _current(request)
    if tovaroved is not None:
        source, allowed = TOVAROVED_MENU, access.TOVAROVED_PAGES
    elif manager is not None:
        source, allowed = MENU, access.MANAGER_PAGES
    else:
        source, allowed = MENU, None

    sections = []
    for sec in source:
        items = []
        for it in sec.items:
            if allowed is not None and it.name not in allowed:
                continue
            try:
                url = reverse(it.name)
            except NoReverseMatch:
                continue
            on = bool(current) and (current == it.name or current in it.also
                                    or bool(it.prefix and current.startswith(it.prefix)))
            items.append({"title": it.title, "url": url, "on": on})
        if items:
            sections.append({"key": sec.key, "title": sec.title, "icon": sec.icon, "url": items[0]["url"],
                             "on": any(i["on"] for i in items), "items": items})

    # Tovaroved o'z sahifasida (xodim/<o'zi>/) — «Mening ishim» tanlangan bo'ladi
    if tovaroved is not None and current == "kpi:xodim" and str(kwargs.get("pk")) == str(tovaroved.pk):
        for s in sections:
            if s["key"] == "men":
                s["on"] = True
                for i in s["items"]:
                    i["on"] = True

    active = next((s for s in sections if s["on"]), None)
    sub = active["items"] if active and len(active["items"]) > 1 else []
    return {"sections": sections, "sub": sub, "sub_title": active["title"] if active else ""}
