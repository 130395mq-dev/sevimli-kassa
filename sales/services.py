"""
Smena yopish.

Bu yerda bitta ish qilinadi: bazadagi cheklardan smena yakunini yig'ish.
Chek modulining o'zi hech narsa hisoblamaydi (`shared/receipt.py` ga qarang),
shuning uchun hamma arifmetika shu faylda — bitta joyda.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from shared.receipt import PaymentLine, ShiftReceipt

from .models import CashOperation, Payment, Sale, Shift


class ShiftError(Exception):
    """Smenani yopib bo'lmaydigan holat."""


def build_receipt(shift: Shift, market: str = "Sevimli Market") -> ShiftReceipt:
    """Smena bo'yicha barcha raqamlarni yig'adi.

    Yopilmagan smena uchun ham ishlaydi — kun davomida oraliq hisobot
    ko'rish uchun («X-hisobot»). Faqat yopilish vaqti hozirgi vaqt bo'ladi.
    """
    sales = shift.sales.filter(kind=Sale.SALE)
    returns = shift.sales.filter(kind=Sale.RETURN)

    agg = sales.aggregate(
        n=Count("id"),
        gross=Sum("gross_total"),
        disc=Sum("discount_total"),
        pts_spent=Sum("points_spent"),
        pts_earned=Sum("points_earned"),
    )
    ret = returns.aggregate(n=Count("id"), total=Sum("net_total"))

    # To'lov turlari bo'yicha — faqat savdolar
    rows = (
        Payment.objects.filter(sale__in=sales)
        .values("method__name", "method__is_cash", "method__sort")
        .annotate(total=Sum("amount"))
        .order_by("method__sort", "method__name")
    )
    payments = [
        PaymentLine(
            name=r["method__name"],
            amount=r["total"] or 0,
            is_cash=r["method__is_cash"],
        )
        for r in rows
    ]

    # Qaytarishlarning naqd qismi — kassadan chiqqan pul
    returns_cash = (
        Payment.objects.filter(sale__in=returns, method__is_cash=True).aggregate(
            t=Sum("amount")
        )["t"]
        or 0
    )

    ops = shift.cash_ops.values("kind").annotate(t=Sum("amount"))
    cash_in = next((o["t"] for o in ops if o["kind"] == CashOperation.IN), 0) or 0
    cash_out = next((o["t"] for o in ops if o["kind"] == CashOperation.OUT), 0) or 0

    return ShiftReceipt(
        market=market,
        point=shift.register.point_name,
        register=shift.register.name,
        cashier=shift.cashier,
        shift_no=shift.number,
        opened_at=timezone.localtime(shift.opened_at),
        closed_at=timezone.localtime(shift.closed_at or timezone.now()),
        receipts_count=agg["n"] or 0,
        gross_total=agg["gross"] or 0,
        discount_total=agg["disc"] or 0,
        # Ball so'mga teng (1 ball = 1 so'm), chekda tiyin kutiladi
        paid_by_points=(agg["pts_spent"] or 0) * 100,
        payments=payments,
        returns_count=ret["n"] or 0,
        returns_total=ret["total"] or 0,
        returns_cash=returns_cash,
        opening_cash=shift.opening_cash,
        cash_in=cash_in,
        cash_out=cash_out,
        counted_cash=shift.counted_cash,
        points_earned=agg["pts_earned"] or 0,
        points_spent=agg["pts_spent"] or 0,
        doc_no=f"#{shift.pk}",
        is_final=shift.status == Shift.CLOSED,
    )


@transaction.atomic
def close_shift(shift: Shift, counted_cash: int | None = None) -> ShiftReceipt:
    """Smenani yopadi va yakuniy chek ma'lumotini qaytaradi.

    Yopishga to'sqinlik qiladigan yagona narsa — smenaning allaqachon
    yopilgani. Yuborilmagan cheklar yopishga to'sqinlik qilmaydi:
    ular navbatda qoladi va keyin yuboriladi. Kassirni internet uchun
    ushlab turish noto'g'ri bo'lardi.
    """
    shift = Shift.objects.select_for_update().get(pk=shift.pk)

    if shift.status == Shift.CLOSED:
        raise ShiftError(f"Smena #{shift.number} allaqachon yopilgan")

    if counted_cash is not None:
        shift.counted_cash = counted_cash

    shift.closed_at = timezone.now()
    shift.status = Shift.CLOSED
    shift.save(update_fields=["closed_at", "status", "counted_cash"])

    return build_receipt(shift)


def pending_count(shift: Shift) -> int:
    """Shu smenada MoySklad'ga hali yetib bormagan cheklar soni."""
    return shift.sales.exclude(sync_status=Sale.SENT).count()
