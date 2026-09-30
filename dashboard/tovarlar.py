"""Panel: bonusli cheklar va eng ko'p sotilgan tovarlar (Top-100).

Egasining so'rovi (2026-09-26): «bonus bo'limidan qaysi chekdan qancha
ball berilgani, qancha yechilgani, chekda nimalar borligi ko'rinsin; eng
ko'p sotilgan 100 ta tovar kodi, shtrix kodi, nomi bilan chiqsin».

Hammasi faqat O'QIYDI — bazaga hech narsa yozilmaydi, yangi jadval yo'q.
Ball harakatlarining haqiqat manbai — `BonusEntry` reyestri (savdoda
«berildi»/«sarflandi», qaytarishda teskari yozuvlar ham shu yerda).
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.db.models import Count, Q, Sum

from catalog.models import Barcode, Product
from sales.models import POINT_TIYIN, BonusEntry, Register, Sale, SaleItem

from .savdo import PRESETS, _bounds, parse_range, period_label

TOP_SIZES = (50, 100, 200)
TOP_DEFAULT = 100
#: Saralash: URL qiymati → hisob kaliti
SORTS = {"summa": "total", "soni": "qty", "cheklar": "n"}
SORT_NAMES = [("summa", "Summa"), ("soni", "Soni"), ("cheklar", "Cheklar")]
BONUS_KINDS = [("", "Hammasi"), ("berildi", "Ball berilgan"), ("yechildi", "Ball yechilgan")]
PAGE_SIZE = 50


def fmt_qty(value) -> str:
    """Miqdor: butun bo'lsa «12», kasr bo'lsa «0,734» (vaznli tovar)."""
    if value is None:
        return "0"
    d = Decimal(value)
    if d == d.to_integral_value():
        return f"{int(d):,}".replace(",", " ")
    return f"{d.normalize():f}".replace(".", ",")


def _period(params) -> dict:
    start, end, preset = parse_range(params)
    return {"start": start, "end": end, "preset": preset,
            "label": period_label(start, end), "presets": PRESETS}


# ------------------------------------------------------------ bonusli cheklar


def bonus_receipts(params) -> dict:
    """Davrdagi ball harakati bo'lgan cheklar + tepadagi jami raqamlar.

    Har chek uchun: berildi (+) va yechildi (−) — shu chekka bog'langan
    reyestr yozuvlari yig'indisi. Qaytarish chekida «+» mijozga qaytgan
    ball, «−» qaytarib olingan ball bo'ladi.
    """
    period = _period(params)
    a, b = _bounds(period["start"], period["end"])
    tur = (params.get("tur") or "").strip()
    q = (params.get("q") or "").strip()

    qs = (
        Sale.objects.filter(created_at__gte=a, created_at__lt=b)
        .annotate(
            given=Sum("bonus_entries__delta", filter=Q(bonus_entries__delta__gt=0)),
            taken=Sum("bonus_entries__delta", filter=Q(bonus_entries__delta__lt=0)),
        )
        .filter(Q(given__gt=0) | Q(taken__lt=0))
    )
    if tur == "berildi":
        qs = qs.filter(given__gt=0)
    elif tur == "yechildi":
        qs = qs.filter(taken__lt=0)
    else:
        tur = ""
    if q:
        cond = (
            Q(customer__name__icontains=q) | Q(customer__phone__icontains=q)
            | Q(customer__discount_card__icontains=q) | Q(receipt_number=q)
        )
        if q.isdigit():
            cond |= Q(number=int(q))
        qs = qs.filter(cond)
    qs = qs.select_related("customer", "shift__register__store").order_by("-created_at", "-pk")

    # Jami — butun davr bo'yicha (qidiruv/tur filtriga bog'liq emas)
    ent = BonusEntry.objects.filter(sale__created_at__gte=a, sale__created_at__lt=b)
    agg = ent.aggregate(
        earned=Sum("delta", filter=Q(kind=BonusEntry.EARN)),
        spent=Sum("delta", filter=Q(kind=BonusEntry.SPEND)),
        ret_plus=Sum("delta", filter=Q(kind=BonusEntry.RETURN, delta__gt=0)),
        ret_minus=Sum("delta", filter=Q(kind=BonusEntry.RETURN, delta__lt=0)),
        customers=Count("customer", distinct=True),
    )
    sales = Sale.objects.filter(pk__in=ent.values("sale_id"), kind=Sale.SALE)
    s_agg = sales.aggregate(n=Count("id"), net=Sum("net_total"), spent=Sum("points_spent"))
    totals = {
        "earned": agg["earned"] or 0,
        "spent": -(agg["spent"] or 0),
        "ret_plus": agg["ret_plus"] or 0,
        "ret_minus": -(agg["ret_minus"] or 0),
        "customers": agg["customers"] or 0,
        "receipts": s_agg["n"] or 0,
        # Chek summasi = pul bilan to'langani + ball bilan to'langani
        "sum": ((s_agg["net"] or 0) + (s_agg["spent"] or 0) * POINT_TIYIN) / 100,
    }
    return {**period, "qs": qs, "tur": tur, "q": q, "totals": totals,
            "kinds": BONUS_KINDS}


def decorate_receipts(sales) -> list:
    """Sahifadagi cheklarga ko'rsatish uchun maydonlar qo'shadi.

    `point_name` har chaqiriqda bazaga boradi — kassa bo'yicha keshlanadi.
    """
    names: dict[int, str] = {}
    out = []
    for s in sales:
        reg = s.shift.register
        if reg.pk not in names:
            names[reg.pk] = reg.point_name
        s.point = names[reg.pk]
        s.register_name = reg.name
        s.full_sum = (s.net_total + s.points_spent * POINT_TIYIN) / 100
        s.given_pts = s.given or 0
        s.taken_pts = -(s.taken or 0)
        out.append(s)
    return out


# ------------------------------------------------------------ chek ichi


def receipt_lines(sale) -> list[dict]:
    """Chek qatorlari: nom, kod, shtrix kod, soni, narx, chegirma, summa.

    Shtrix kod — kassada skanerlangani (qatorda saqlangan); bo'lmasa
    (qo'lda tanlangan tovar) tovarning asosiy kodi.
    """
    items = list(sale.items.select_related("product").order_by("position", "pk"))
    need = [it.product_id for it in items if it.product_id and not it.barcode]
    main = main_barcodes(need)
    rows = []
    for i, it in enumerate(items, 1):
        p = it.product
        rows.append({
            "n": i,
            "name": it.name,
            "code": (p.code if p else "") or "",
            "barcode": it.barcode or main.get(it.product_id, ""),
            "qty": fmt_qty(it.quantity),
            "uom": (p.uom_name if p else "") or "",
            "price": it.price / 100,
            "discount": it.discount / 100,
            "total": it.total / 100,
            "product_id": it.product_id,
        })
    return rows


def main_barcodes(product_ids) -> dict:
    """Har tovarning asosiy shtrix kodi: donali (upakovka emas) birinchisi."""
    ids = {pid for pid in product_ids if pid}
    if not ids:
        return {}
    out: dict = {}
    for pid, value, pack in (
        Barcode.objects.filter(product_id__in=ids)
        .order_by("product_id", "pack_quantity", "pk")
        .values_list("product_id", "value", "pack_quantity")
    ):
        if pid not in out:
            out[pid] = value
    return out


# ------------------------------------------------------------ Top tovarlar


def top_products(params) -> dict:
    """Davrda eng ko'p sotilgan tovarlar — kod, shtrix kod, nom bilan.

    Tovar bo'yicha guruhlanadi (nom bo'yicha emas: bir tovar nomi keyin
    o'zgarsa ham bitta qator bo'lib qoladi). Bazada tovari yo'q qatorlar
    (eski yoki o'chirilgan tovar) nom bo'yicha qo'shiladi.
    """
    period = _period(params)
    a, b = _bounds(period["start"], period["end"])
    sort = params.get("tartib") if params.get("tartib") in SORTS else "summa"
    key = SORTS[sort]
    try:
        size = int(params.get("soni") or TOP_DEFAULT)
    except (TypeError, ValueError):
        size = TOP_DEFAULT
    if size not in TOP_SIZES:
        size = TOP_DEFAULT
    bonus_only = params.get("bonus") == "1"

    base = SaleItem.objects.filter(sale__created_at__gte=a, sale__created_at__lt=b)
    if bonus_only:
        base = base.filter(sale__customer__isnull=False)
    sold = base.filter(sale__kind=Sale.SALE)
    returned = base.filter(sale__kind=Sale.RETURN)
    measures = {"total": Sum("total"), "qty": Sum("quantity"),
                "n": Count("sale", distinct=True)}

    by_pid = list(
        sold.filter(product__isnull=False).values("product_id")
        .annotate(**measures).order_by(f"-{key}", "-total", "product_id")[:size]
    )
    by_name = list(
        sold.filter(product__isnull=True).values("name")
        .annotate(**measures).order_by(f"-{key}", "-total", "name")[:size]
    )
    rows = [{**r, "name": None} for r in by_pid] + [{**r, "product_id": None} for r in by_name]
    rows.sort(key=lambda r: (-(r[key] or 0), -(r["total"] or 0)))
    rows = rows[:size]

    pids = [r["product_id"] for r in rows if r["product_id"]]
    names = [r["name"] for r in rows if not r["product_id"]]
    products = {p.pk: p for p in Product.objects.filter(pk__in=pids)}
    codes = main_barcodes(pids)
    # Tovarda shtrix kod bo'lmasa — kassada eng ko'p skanerlangani
    missing = [pid for pid in pids if pid not in codes]
    if missing:
        for r in (
            sold.filter(product_id__in=missing).exclude(barcode="")
            .values("product_id", "barcode").annotate(c=Count("id"))
            .order_by("product_id", "-c")
        ):
            codes.setdefault(r["product_id"], r["barcode"])
    ret = {r["product_id"]: r["qty"] for r in
           returned.filter(product_id__in=pids).values("product_id").annotate(qty=Sum("quantity"))}
    ret_names = {r["name"]: r["qty"] for r in
                 returned.filter(product__isnull=True, name__in=names)
                 .values("name").annotate(qty=Sum("quantity"))}

    agg = sold.aggregate(total=Sum("total"), n=Count("sale", distinct=True))
    grand = agg["total"] or 0
    out = []
    for i, r in enumerate(rows, 1):
        p = products.get(r["product_id"]) if r["product_id"] else None
        total = r["total"] or 0
        rq = ret.get(r["product_id"]) if p else ret_names.get(r["name"])
        out.append({
            "rank": i,
            "product_id": r["product_id"],
            "code": (p.code if p else "") or "",
            "barcode": codes.get(r["product_id"], "") if p else "",
            "name": p.name if p else r["name"],
            "uom": (p.uom_name if p else "") or "",
            "qty": r["qty"] or 0,
            "qty_text": fmt_qty(r["qty"]),
            "returned": rq or 0,
            "returned_text": fmt_qty(rq) if rq else "",
            "total": total / 100,
            "n": r["n"] or 0,
            "share": (total / grand * 100) if grand else 0,
        })
    top_share = max((r["share"] for r in out), default=0)
    for r in out:
        # Chiziq eng kattasiga nisbatan — kichik ulushlar ham ko'rinsin
        r["bar"] = (r["share"] / top_share * 100) if top_share else 0
        # CSS uchun nuqta bilan: shablondagi floatformat «38,6» (vergul)
        # qaytaradi va brauzer bunday kenglikni tashlab yuboradi
        r["bar_css"] = f"{r['bar']:.1f}"
    distinct = (
        sold.filter(product__isnull=False).values("product_id").distinct().count()
        + sold.filter(product__isnull=True).values("name").distinct().count()
    )
    return {
        **period, "rows": out, "sort": sort, "sorts": SORT_NAMES, "size": size,
        "sizes": TOP_SIZES, "bonus_only": bonus_only,
        "grand": grand / 100, "receipts": agg["n"] or 0, "distinct": distinct,
    }


def _csv_text(value: str) -> str:
    """Raqamli kodni Excel matn deb o'qisin: aks holda shtrix kod
    «4,78E+12» bo'lib, kodning boshidagi nollar yo'qolib qoladi."""
    return f'="{value}"' if value and value.isdigit() else value


def _csv_num(value) -> str:
    if not value:
        return ""
    d = Decimal(value)
    if d == d.to_integral_value():
        return str(int(d))
    return f"{d.normalize():f}".replace(".", ",")


def top_csv(data: dict) -> tuple[str, str]:
    """(fayl nomi, CSV matni) — Excel ochadigan «;» ajratgichli."""
    buf = StringIO()
    w = csv.writer(buf, delimiter=";")
    title = f"Top {data['size']} tovar — {data['label']}"
    if data["bonus_only"]:
        title += " (faqat bonus kartali cheklar)"
    w.writerow([title])
    w.writerow(["#", "Kodi", "Shtrix kodi", "Nomi", "O'lchov", "Sotildi",
                "Qaytarildi", "Summa, so'm", "Cheklar", "Ulush, %"])
    for r in data["rows"]:
        w.writerow([
            r["rank"], _csv_text(r["code"]), _csv_text(r["barcode"]), r["name"], r["uom"],
            _csv_num(r["qty"]), _csv_num(r["returned"]),
            f"{r['total']:.2f}".replace(".", ","), r["n"],
            f"{r['share']:.1f}".replace(".", ","),
        ])
    name = f"sevimli-top-{data['size']}-{data['start']:%Y%m%d}-{data['end']:%Y%m%d}.csv"
    return name, buf.getvalue()


# ------------------------------------------------- tushib ketgan tovarlar

#: «Hozir» — oxirgi shuncha TO'LIQ kun (bugun hali tugamagan, alohida)
RECENT_DAYS = 3
#: «Oldin» — undan oldingi shuncha kun
BASE_DAYS = 14
#: Oldin kunlarning kamida shuncha qismida sotilgan bo'lsa — «muntazam»
REGULAR_SHARE = 0.5
#: Hozir kuniga oldingidan shuncha (yoki ko'proq) kam — «kamaygan»
DROP_SHARE = 0.5
#: Kun «to'liq ishlagan» hisoblanadi, agar cheklar soni o'sha oynadagi eng
#: ko'p kunning shuncha qismidan kam bo'lmasa (tizim ishga tushgan
#: 13–16.09 kunlari kassalarning bir qismigina ulangan edi)
ACTIVE_DAY_SHARE = 0.3
FALL_CACHE_TTL = 15 * 60


def _active_days(start: date, end: date, registers=None) -> list[date]:
    """Oynadagi to'liq ishlagan kunlar (yarim kunlik ishga tushish kunlari
    va savdosiz kunlar hisobdan chiqadi — aks holda hamma tovar «muntazam
    emas» bo'lib qolardi)."""
    from django.db.models.functions import TruncDate
    from django.utils import timezone as tz

    a, b = _bounds(start, end)
    sales = Sale.objects.filter(kind=Sale.SALE, created_at__gte=a, created_at__lt=b)
    if registers is not None:
        sales = sales.filter(shift__register_id__in=registers)
    counts = {
        r["day"]: r["n"]
        for r in sales
        .annotate(day=TruncDate("created_at", tzinfo=tz.get_current_timezone()))
        .values("day").annotate(n=Count("id")).order_by()
    }
    if not counts:
        return []
    top = max(counts.values())
    return sorted(d for d, n in counts.items() if n >= top * ACTIVE_DAY_SHARE)


ALL_WAREHOUSES = "hammasi"


def kassa_warehouses() -> list[dict]:
    """Kassalar sotadigan omborlar: [{"id", "name", "registers": [pk…]}].

    Egasining so'rovi (2026-09-30): tushib ketgan tovarlar omborma-ombor
    ko'rinsin — keyin boshqa marketlar ham qo'shiladi. Chek omborni o'zida
    saqlamaydi, shuning uchun kassa → ombor bog'lanishidan olinadi
    (arxivlangan kassalar ham: ularning eski savdosi o'z omboriga tushadi).
    """
    from catalog.models import Warehouse

    groups: dict[str, list[int]] = {}
    for reg in Register.objects.select_related("store", "settings_row"):
        ms = reg.warehouse_ms_id
        if ms:
            groups.setdefault(str(ms), []).append(reg.pk)
    names = {str(w.ms_id): w.name for w in Warehouse.objects.filter(ms_id__in=list(groups))}
    return sorted(
        ({"id": k, "name": names.get(k) or "Nomsiz ombor", "registers": v} for k, v in groups.items()),
        key=lambda w: w["name"].lower(),
    )


def falling_products(now=None, warehouse=None) -> dict:
    """Oldin yaxshi sotilgan, lekin hozir kamaygan yoki umuman to'xtagan tovarlar.

    Egasining so'rovi (2026-09-27): «sotilishi yaxshi bo'lib birdan to'xtagan
    tovarlarni ko'rsatib turadigan ko'rsatkich». Qoidalar (sodda, tushuntirsa
    bo'ladigan):
      * «oldin» — oxirgi 3 to'liq kundan oldingi 14 kun (faqat to'liq
        ishlagan kunlar); tovar ulardan kamida yarmida sotilgan va kuniga
        o'rtacha kamida 1 dona ketgan bo'lsa — muntazam sotiladigan tovar;
      * TO'XTAGAN — muntazam tovar oxirgi 3 kun va bugun umuman sotilmagan;
      * KAMAYGAN — oxirgi 3 kunda kuniga oldingidan 2 barobar (50%) va
        undan ham kam sotilgan (oldin kuniga kamida 2 dona ketgan bo'lsa).
    Qoldiq tanlangan omborniki (`catalog.Stock`); 0 bo'lsa — «tugagan»:
    demak sabab talab emas, tovar kelmay qolgan. «Boshqa omborlarda» —
    MoySklad'dagi qolgan omborlar qoldig'i (ko'chirib kelish mumkinmi).

    `warehouse` — ombor ms_id yoki «hammasi». Berilmasa: kassalar bitta
    omborda bo'lsa — o'sha, bir nechta bo'lsa — hammasi.
    """
    from django.core.cache import cache
    from django.db.models.functions import TruncDate
    from django.utils import timezone as tz

    today = tz.localdate(now)
    warehouses = kassa_warehouses()
    by_id = {w["id"]: w for w in warehouses}
    if warehouse not in by_id and warehouse != ALL_WAREHOUSES:
        warehouse = warehouses[0]["id"] if len(warehouses) == 1 else ALL_WAREHOUSES
    if warehouse == ALL_WAREHOUSES:
        registers = None
        stock_ids = list(by_id)
        wh_name = "Hamma omborlar"
    else:
        registers = by_id[warehouse]["registers"]
        stock_ids = [warehouse]
        wh_name = by_id[warehouse]["name"]
    options = [{"id": w["id"], "name": w["name"]} for w in warehouses]
    if len(warehouses) > 1:
        options.insert(0, {"id": ALL_WAREHOUSES, "name": "Hamma omborlar"})

    key = f"tushgan:v2:{warehouse}:{today.isoformat()}:{tz.localtime(now).hour}"
    hit = cache.get(key)
    if hit is not None:
        return hit

    recent_start = today - timedelta(days=RECENT_DAYS)
    recent_end = today - timedelta(days=1)
    base_end = recent_start - timedelta(days=1)
    base_start = base_end - timedelta(days=BASE_DAYS - 1)
    base_days = _active_days(base_start, base_end, registers)
    recent_days = _active_days(recent_start, recent_end, registers)

    out = {"warehouse": warehouse, "warehouse_name": wh_name, "warehouses": options,
           "stopped": [], "dropped": [], "base_days": len(base_days),
           "recent_days": len(recent_days), "base_start": base_start,
           "base_end": base_end, "recent_start": recent_start,
           "recent_end": recent_end, "ready": len(base_days) >= 5 and len(recent_days) >= 2}
    if not out["ready"]:
        cache.set(key, out, FALL_CACHE_TTL)
        return out

    a, _ = _bounds(base_start, base_end)
    _, b = _bounds(today, today)
    items = SaleItem.objects.filter(sale__kind=Sale.SALE, sale__created_at__gte=a,
                                    sale__created_at__lt=b, product__isnull=False)
    if registers is not None:
        items = items.filter(sale__shift__register_id__in=registers)
    rows = (
        items
        .annotate(day=TruncDate("sale__created_at", tzinfo=tz.get_current_timezone()))
        .values("product_id", "day")
        .annotate(qty=Sum("quantity"), total=Sum("total"))
        .order_by()
    )
    base_set, recent_set = set(base_days), set(recent_days)
    stats: dict[int, dict] = {}
    for r in rows:
        s = stats.setdefault(r["product_id"], {"b_qty": Decimal(0), "b_sum": 0, "b_days": 0,
                                               "r_qty": Decimal(0), "r_sum": 0,
                                               "t_qty": Decimal(0), "last": None})
        d = r["day"]
        if d in base_set:
            s["b_qty"] += r["qty"] or 0
            s["b_sum"] += r["total"] or 0
            s["b_days"] += 1
        elif d in recent_set:
            s["r_qty"] += r["qty"] or 0
            s["r_sum"] += r["total"] or 0
        elif d == today:
            s["t_qty"] += r["qty"] or 0
        if (r["qty"] or 0) > 0 and (s["last"] is None or d > s["last"]):
            s["last"] = d

    nb, nr = len(base_days), len(recent_days)
    stopped, dropped = [], []
    for pid, s in stats.items():
        if s["b_days"] < nb * REGULAR_SHARE:
            continue
        b_day_qty = s["b_qty"] / nb
        if b_day_qty < 1:
            continue
        r_day_qty = s["r_qty"] / nr
        row = {
            "product_id": pid, "last": s["last"],
            "before_qty": b_day_qty, "before_sum": s["b_sum"] / nb / 100,
            "now_qty": r_day_qty, "now_sum": s["r_sum"] / nr / 100,
            "today_qty": s["t_qty"],
        }
        if s["r_qty"] <= 0 and s["t_qty"] <= 0:
            row["lost"] = row["before_sum"]
            stopped.append(row)
        elif b_day_qty >= 2 and r_day_qty <= b_day_qty * Decimal(str(DROP_SHARE)):
            row["lost"] = row["before_sum"] - row["now_sum"]
            row["change"] = round(float((r_day_qty - b_day_qty) / b_day_qty * 100))
            dropped.append(row)

    stopped.sort(key=lambda r: -r["lost"])
    dropped.sort(key=lambda r: -r["lost"])
    ids = [r["product_id"] for r in stopped + dropped]
    products = {p.pk: p for p in Product.objects.filter(pk__in=ids)}
    codes = main_barcodes(ids)
    from catalog.models import Stock
    in_stock = Stock.objects.filter(product_id__in=ids)
    stock = {
        r["product_id"]: r["q"]
        for r in in_stock.filter(store_ms_id__in=stock_ids)
        .values("product_id").annotate(q=Sum("quantity")).order_by()
    }
    elsewhere = {
        r["product_id"]: r["q"]
        for r in in_stock.exclude(store_ms_id__in=stock_ids)
        .values("product_id").annotate(q=Sum("quantity")).order_by()
    }
    for r in stopped + dropped:
        p = products.get(r["product_id"])
        r.update({
            "name": p.name if p else "—", "code": (p.code if p else "") or "",
            "uom": (p.uom_name if p else "") or "", "barcode": codes.get(r["product_id"], ""),
            "stock": stock.get(r["product_id"]),
            "before_text": fmt_qty(round(r["before_qty"], 1) if r["before_qty"] % 1 else r["before_qty"]),
            "now_text": fmt_qty(round(r["now_qty"], 1) if r["now_qty"] % 1 else r["now_qty"]),
        })
        r["out"] = r["stock"] is not None and r["stock"] <= 0
        r["stock_text"] = fmt_qty(r["stock"]) if r["stock"] is not None else "—"
        other = elsewhere.get(r["product_id"])
        r["elsewhere"] = other if other and other > 0 else None
        r["elsewhere_text"] = fmt_qty(other) if r["elsewhere"] else ""
        r["days_idle"] = (today - r["last"]).days if r["last"] else None

    out.update({
        "stopped": stopped, "dropped": dropped,
        "stopped_out": sum(1 for r in stopped if r["out"]),
        "lost_per_day": sum(r["lost"] for r in stopped + dropped),
    })
    cache.set(key, out, FALL_CACHE_TTL)
    return out


def falling_csv(data: dict) -> tuple[str, str]:
    buf = StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([f"Tushib ketgan tovarlar — {data['warehouse_name']} — oldin "
                f"{data['base_start']:%d.%m}–{data['base_end']:%d.%m}, "
                f"hozir {data['recent_start']:%d.%m}–{data['recent_end']:%d.%m}"])
    w.writerow(["Holat", "Kodi", "Shtrix kodi", "Nomi", "O'lchov", "Oldin kuniga",
                "Hozir kuniga", "Bugun", "Oxirgi sotilgan", "Qoldiq",
                "Boshqa omborlarda", "Yo'qotish kuniga, so'm"])
    for kind, rows in (("To'xtagan", data["stopped"]), ("Kamaygan", data["dropped"])):
        for r in rows:
            w.writerow([
                kind, _csv_text(r["code"]), _csv_text(r["barcode"]), r["name"], r["uom"],
                _csv_num(round(r["before_qty"], 2)), _csv_num(round(r["now_qty"], 2)),
                _csv_num(r["today_qty"]),
                r["last"].strftime("%d.%m.%Y") if r["last"] else "",
                "tugagan" if r["out"] else _csv_num(r["stock"]) if r["stock"] is not None else "",
                _csv_num(r["elsewhere"]) if r["elsewhere"] else "",
                f"{r['lost']:.0f}",
            ])
    return f"sevimli-tushgan-{data['recent_end']:%Y%m%d}.csv", buf.getvalue()
