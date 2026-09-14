"""Repair only proven legacy unit-price rounding on unpaid shipments.

The receipt, quantities, stock movements and payments are never changed.
An edited, paid, returned or unrecognised document remains blocked for review.
Position IDs are retained; retrying after a timeout is safe.
"""

import logging
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from .models import Sale

logger = logging.getLogger(__name__)


def _id(value):
    return str(UUID(str(value)))


def _reference(value, kind):
    from moysklad.client import BASE_URL

    meta = value.get("meta", {})
    prefix = f"{BASE_URL}/entity/{kind}/"
    href = meta.get("href", "")
    if meta.get("type") != kind or not href.startswith(prefix):
        raise ValueError("Unexpected reference")
    return _id(href[len(prefix):])


def _minor(value):
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def repair_legacy_rounding(client, sale, original):
    from .writer import allocate, position_price, WriteError

    if (sale.kind != Sale.SALE or not sale.ms_demand_id
            or getattr(sale, "sync_status", "") == Sale.SENT
            or not getattr(sale, "sync_error", "").startswith((
                "MoySklad summani boshqacha hisobladi:", "Yaxlitlashni tiklash:"))):
        return None

    try:
        doc_id = _id(sale.ms_demand_id)
        path = f"entity/demand/{doc_id}"
        doc = client.get(path)
        if (_id(original.get("id")) != doc_id or _id(doc.get("id")) != doc_id
                or _id(doc.get("syncId")) != str(sale.local_uuid)):
            raise ValueError("Hujjat identifikatori mos emas")
        payments = list(sale.payments.all())
        if (sum(p.amount for p in payments) != sale.net_total
                or any(p.ms_payment_id for p in payments)
                or doc.get("payedSum") != 0 or doc.get("payments") or doc.get("returns")):
            raise ValueError("Hujjatda to'lov/qaytarish bor yoki to'lov summasi mos emas")
        register = sale.shift.register
        if (_reference(doc.get("organization", {}), "organization") != str(register.organization_ms_id)
                or _reference(doc.get("store", {}), "store") != str(register.warehouse_ms_id)):
            raise ValueError("Tashkilot yoki ombor mos emas")

        items = list(sale.items.all())
        cuts = allocate(sale.points_spent * 100, [i.total for i in items])
        if not items or sum(i.total - c for i, c in zip(items, cuts)) != sale.net_total:
            raise ValueError("Chek qatorlari yig'indisi mos emas")
        page = client.get(f"{path}/positions", limit=1000)
        rows = page.get("rows", [])
        if len(rows) != len(items) or page.get("meta", {}).get("size") != len(rows):
            raise ValueError("Hujjat qatorlari soni mos emas")

        pending = []
        current_sum = Decimal(0)
        seen_ids = set()
        for row, item, cut in zip(rows, items, cuts):
            position_id = _id(row.get("id"))
            if position_id in seen_ids:
                raise ValueError("Takroriy qator identifikatori")
            seen_ids.add(position_id)
            quantity = Decimal(item.quantity)
            amount = item.total - cut
            if quantity <= 0 or amount < 0:
                raise ValueError("Miqdor yoki summa noto'g'ri")
            # Exact fingerprint of the previous writer. A resumed repair can
            # contain a mix of old and corrected prices after a timeout.
            old_price = Decimal(round(amount / float(quantity)))
            new_price = position_price(amount, quantity)
            remote_price = Decimal(str(row.get("price")))
            row_sum = _minor(remote_price * quantity)
            if (_reference(row.get("assortment", {}), "product") != str(item.ms_product_id)
                    or Decimal(str(row.get("quantity"))) != quantity
                    or (remote_price != old_price and row_sum != amount)
                    or row.get("discount", 0) != 0 or row.get("vat", 0) != 0
                    or row.get("pack")):
                raise ValueError("Qator eski yaxlitlashga mos emas; qo'lda tekshirish kerak")
            current_sum += row_sum
            if row_sum != amount:
                pending.append((position_id, new_price))
        if current_sum != doc.get("sum"):
            raise ValueError("MoySklad summasi qatorlarga mos emas")

        # All rows must pass before the first write. PUT changes only price,
        # retaining each position ID and quantity (no stock duplication).
        for position_id, price in pending:
            client.put(f"{path}/positions/{position_id}", {"price": price})
        repaired = client.get(path)
        if repaired.get("sum") != sale.net_total:
            raise ValueError("Narx aniqligi yetarli emas; chek hali bloklangan")
        logger.warning(
            "Chek #%s: yaxlitlash tuzatildi, hujjat=%s, summa=%s tiyin, qator=%s",
            sale.number, doc_id, sale.net_total, len(pending),
        )
        return repaired
    except (ValueError, ArithmeticError, TypeError, AttributeError, KeyError) as exc:
        logger.warning("Chek #%s avtomatik tuzatilmadi: %s", sale.number, exc)
        return None
    except Exception as exc:
        # The persisted receipt and document ID survive a timeout. Retry the
        # same rows, then verify the full sum before any payment is sent.
        raise WriteError(f"Yaxlitlashni tiklash: {type(exc).__name__}") from exc
