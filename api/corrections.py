"""Bir martalik chek tuzatishlari (egasining qarori bilan).

Kassada tiqilib qolgan va serverga hech qachon o'tmaydigan ANIQ cheklar uchun.
Kassa chekni o'zgartira olmaydi (pul olingan, chek berilgan), server esa
noto'g'ri qatorni to'g'ri ravishda rad etadi. Shuning uchun tuzatish shu
yerda: chek kelganda, FAQAT ro'yxatdagi local_uuid uchun va FAQAT qator aynan
kutilgan ko'rinishda bo'lsa qo'llanadi. Boshqa hech bir chekka ta'sir yo'q.
Mos kelmasa — chek o'zgarmaydi (avvalgidek rad etiladi).
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

logger = logging.getLogger("api")

FIXES: dict[str, dict] = {
    # Kassa3, 27.09.2026 16:28. 3-qator: katalogda yo'q 21… zavod kodi kassa
    # 1.18.6 da «narxli yorliq» deb o'qilib, «колбаса ТК SEVIMLI» 0,0172028… kg
    # (1 032 so'm) bo'lib qolgan (kassa 1.18.7 da tuzatilgan). Server log,
    # 01.10.2026 13:29: «3-qator rad etildi: product_id=52876 quantity=
    # '0.01720286714452408734789131522' price=5999000 total=103200».
    # Egasining qarori (01.10.2026): 3-qator olib tashlanadi, to'lov 1 032
    # so'mga kamayadi; qolgan qatorlar MoySklad'ga yoziladi.
    "9d957adc-32c6-4935-bf3d-58bf9cd100e0": {
        "drop_line": 3,
        "product_id": 52876,
        "quantity": "0.01720286714452408734789131522",
        "total": 103200,
    },
}


def apply(local_uuid, data: dict) -> str | None:
    """Ro'yxatdagi chek bo'lsa — `data` ni joyida tuzatadi va izoh qaytaradi.
    Aks holda hech narsa qilmaydi va None qaytaradi."""
    fix = FIXES.get(str(local_uuid or ""))
    if not fix:
        return None
    items = data.get("items")
    payments = data.get("payments")
    idx = fix["drop_line"] - 1
    if not isinstance(items, list) or not isinstance(payments, list) or len(items) <= idx:
        return None
    line = items[idx]
    if not isinstance(line, dict):
        return None
    try:
        same = (
            int(line.get("product_id") or 0) == fix["product_id"]
            and Decimal(str(line.get("quantity"))) == Decimal(fix["quantity"])
            and int(line.get("total") or 0) == fix["total"]
        )
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not same:
        return None

    new_payments = _reduce_payments(payments, fix["total"])
    if new_payments is None:
        return None
    data["items"] = items[:idx] + items[idx + 1:]
    data["payments"] = new_payments
    note = (f"{fix['drop_line']}-qator olib tashlandi (product_id={fix['product_id']}, "
            f"{fix['total']} tiyin), to'lov shuncha kamaydi")
    logger.warning("Chek %s qo'lda tuzatildi: %s", local_uuid, note)
    return note


def _reduce_payments(payments: list, cut: int) -> list | None:
    """To'lovlarni `cut` tiyinga kamaytiradi: avval naqd, keyin oxirgisidan.
    0 ga tushgan to'lov olib tashlanadi. Yetmasa — None (tuzatilmaydi).
    tendered/change o'zgarmaydi: ular kassada haqiqatda berilgan pul."""
    from sales.models import PaymentMethod

    if not all(isinstance(p, dict) for p in payments):
        return None
    cash = set(PaymentMethod.objects.filter(is_cash=True).values_list("code", flat=True))
    pays = [dict(p) for p in payments]
    order = sorted(range(len(pays)), key=lambda i: (pays[i].get("method") not in cash, -i))
    left = int(cut)
    for i in order:
        if left <= 0:
            break
        try:
            amount = int(pays[i].get("amount") or 0)
        except (TypeError, ValueError):
            return None
        take = min(max(amount, 0), left)
        pays[i]["amount"] = amount - take
        left -= take
    if left:
        return None
    return [p for p in pays if int(p.get("amount") or 0) > 0]
