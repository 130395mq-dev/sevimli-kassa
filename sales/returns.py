"""Authoritative, cumulative allocations for original receipt lines."""
from decimal import Decimal, ROUND_HALF_UP

from .models import Sale, SaleItem
from .writer import allocate


def rounded(value):
    return int(value.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def same_product(item, raw):
    pid, mid = raw.get('product_id'), raw.get('ms_product_id')
    if not pid and not mid:
        return False
    return ((not pid or str(item.product_id) == str(pid)) and
            (not mid or str(item.ms_product_id) == str(mid)))


def allocations(origin, exclude_sale=None):
    """Cash/points are apportioned once; partial refunds use cumulative rounding.

    Pre-migration returns are conservatively allocated FIFO to matching products.
    Ambiguous or inconsistent historical data requires review, never a new refund.
    """
    items = list(origin.items.order_by('position', 'pk'))
    weight = sum(it.total for it in items)
    cuts = allocate(origin.points_spent * 100, [it.total for it in items])
    rows, cumulative = {}, 0
    totals = {'cash': origin.net_total, 'spent': origin.points_spent,
              'earned': origin.points_earned}
    previous = dict.fromkeys(totals, 0)
    for it, cut in zip(items, cuts):
        cumulative += it.total
        row = {'item': it, 'quantity': Decimal(0), 'returned_cash': 0}
        for key, total in totals.items():
            target = rounded(Decimal(total) * cumulative / weight) if weight else 0
            row[key] = target - previous[key]
            previous[key] = target
        row["cash"] = it.total - cut
        rows[it.pk] = row
    returns = SaleItem.objects.filter(sale__kind=Sale.RETURN, sale__origin=origin)
    if exclude_sale:
        returns = returns.exclude(sale_id=exclude_sale.pk)
    for ret in returns.order_by('sale__created_at', 'sale_id', 'position', 'pk'):
        if ret.origin_item_id:
            candidates = [rows[ret.origin_item_id]] if ret.origin_item_id in rows else []
        else:
            raw = {'product_id': ret.product_id, 'ms_product_id': ret.ms_product_id}
            candidates = [r for r in rows.values() if same_product(r['item'], raw)]
        remaining, assigned_cash = ret.quantity, 0
        for row in candidates:
            qty = min(remaining, max(Decimal(0), row['item'].quantity - row['quantity']))
            if qty <= 0:
                continue
            target_cash = rounded(Decimal(ret.total) * (ret.quantity - remaining + qty) / ret.quantity)
            row['quantity'] += qty
            row['returned_cash'] += target_cash - assigned_cash
            assigned_cash = target_cash
            remaining -= qty
            if remaining <= 0:
                break
        if remaining > 0:
            raise ValueError("Eski qaytarish asl chek qatorlariga mos emas. Menejer tekshirsin.")
    return rows


def cash_for(row, quantity):
    target = rounded(Decimal(row['cash']) * (row['quantity'] + quantity) / row['item'].quantity)
    return target - row['returned_cash']


def validate_return(origin, lines):
    rows = allocations(origin)
    for _, raw, qty, total in lines:
        line_id = raw.get('origin_item_id')
        if line_id:
            try:
                candidates = [rows[int(line_id)]]
            except (KeyError, ValueError, TypeError):
                raise ValueError("Qaytarish qatori asl chekda topilmadi")
        else:
            candidates = [r for r in rows.values() if same_product(r['item'], raw)]
            if len(candidates) != 1:
                raise ValueError("Asl chek qatorini qayta tanlang: mahsulot topilmadi yoki bir necha qatorda bor")
        row = candidates[0]
        item = row['item']
        if not same_product(item, raw):
            raise ValueError("Qaytarilayotgan mahsulot asl chek qatoriga mos emas")
        if row['quantity'] + qty > item.quantity:
            raise ValueError("Qaytarish miqdori asl chekdagi qolgan miqdordan oshib ketdi")
        expected = cash_for(row, qty)
        if expected < 0 or total != expected:
            raise ValueError(f"Qaytarish summasi mos emas: {expected} tiyin qaytarish mumkin. Asl chekni yangilang.")
        if int(raw.get('price') or 0) != item.price:
            raise ValueError("Qaytarish narxi asl chek qatoriga mos emas")
        raw['_origin_item'] = item
        # Normalize identity and descriptive data to the original receipt.
        raw.update(product_id=item.product_id, ms_product_id=item.ms_product_id,
                   name=item.name, barcode=item.barcode)
        row['quantity'] += qty
        row['returned_cash'] += total


def reversed_points(origin, exclude_sale=None):
    result = {'spent': 0, 'earned': 0}
    for row in allocations(origin, exclude_sale).values():
        for key in result:
            result[key] += rounded(Decimal(row[key]) * row['quantity'] / row['item'].quantity)
    return result
