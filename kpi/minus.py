"""
Minus (manfiy qoldiq) kuzatuvi.

Manba — MoySklad `report/stock/bystore/current` (rasmiy hujjat bo'yicha):
  * parametrsiz — hozirgi NOLDAN FARQLI qoldiqlar ([{assortmentId, storeId,
    stock}]); ro'yxatda yo'q juftlik — qoldig'i 0;
  * `changedSince` bilan — shu vaqtdan beri o'zgarganlar, NOL qatorlari ham
    (24 soatdan uzoq bo'lmasligi kerak);
  * qayta hisoblanayotgan qoldiq kelmaydi (yoki null) — keyingi safar olinadi.

Kassa katalogining qoldig'iga (`catalog.Stock`) TAYANILMAYDI: u boshqa
hisobotdan to'ldiriladi va bu yerdagi hisobga ta'sir qilmasligi kerak.

Har tekshiruv `MinusScan` bo'lib yoziladi — qaysi soatlarda ma'lumot bor-yo'qligi
shundan ko'rinadi (uzilish bo'lsa kun «yetarli emas» bo'ladi).

Shu tekshiruvning o'zida tovar holatlari ham yuritiladi (`tovar_holatlari`,
2026-11-10): sotuvdagi tovar tugagan, omborda bor tovar narxsiz yoki narxi
tannarxdan past. MoySklad'ga qo'shimcha so'rov yuborilmaydi.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from catalog.models import Product, Warehouse
from moysklad.client import MoySkladError

from . import ms
from .models import Biriktirish, KpiQoida, MinusIssue, MinusScan, Tovaroved, TovarHolat

logger = logging.getLogger(__name__)

FULL_EVERY = timedelta(hours=6)     # to'liq tekshiruv shu oraliqda bir marta
MAX_DELTA = timedelta(hours=23)     # changedSince 24 soatdan oshmasin
DELTA_OVERLAP = timedelta(minutes=10)
GAP = timedelta(hours=2)            # oldingi tekshiruvdan shuncha o'tsa — uzilish


def tracked_warehouses() -> dict[str, str]:
    """Kuzatiladigan omborlar — tovarovedga biriktirilganlari (tarixdagilari ham)."""
    out: dict[str, str] = {}
    for wh, name in Biriktirish.objects.values_list("warehouse_ms_id", "warehouse_name"):
        out[str(wh)] = name or out.get(str(wh), "")
    if out:
        for w in Warehouse.objects.filter(ms_id__in=list(out)):
            out[str(w.ms_id)] = w.name
    return out


def responsible(warehouse_ms_id, day) -> Tovaroved | None:
    """Shu kuni shu do'konga biriktirilgan tovaroved (do'kon bo'yicha mas'ul)."""
    b = (Biriktirish.objects.filter(warehouse_ms_id=warehouse_ms_id, boshlanish__lte=day)
         .exclude(tugash__lt=day).select_related("tovaroved").order_by("-boshlanish", "-pk").first())
    return b.tovaroved if b else None


def work_deadline(start, hours: int, kun_boshi: int, kun_oxiri: int):
    """`start` dan `hours` ISH SOATI keyingi muddat — tun (kun_oxiri..kun_boshi)
    hisoblanmaydi (egasi, 2026-11-10: muddat 12 ish soati). Kechqurun 21:00 da
    chiqqan minus 8–22 ish kunida 12 soat → ertasi 19:00.
    Ish kuni soatlari noto'g'ri bo'lsa — oddiy (astronomik) soat."""
    if not (0 <= kun_boshi < kun_oxiri <= 24):
        return start + timedelta(hours=hours)
    cur = start.astimezone(ms.TASHKENT)
    remaining = timedelta(hours=hours)
    for _ in range(hours + 400):           # har kun kamida bir qadam; cheksiz aylanmaydi
        day0 = cur.replace(hour=0, minute=0, second=0, microsecond=0)
        w0, w1 = day0 + timedelta(hours=kun_boshi), day0 + timedelta(hours=kun_oxiri)
        if cur < w0:
            cur = w0
        if cur >= w1:
            cur = w0 + timedelta(days=1)
            continue
        avail = w1 - cur
        if remaining <= avail:
            return (cur + remaining).astimezone(dt_timezone.utc)
        remaining -= avail
        cur = w0 + timedelta(days=1)
    return (cur + remaining).astimezone(dt_timezone.utc)


def selling_products(wh: str, days: int, now=None) -> dict[str, Decimal]:
    """Oxirgi `days` kunda shu do'kon kassalarida sotilgan tovarlar:
    {tovar_id: sotilgan dona}. Kassasi bog'lanmagan do'kon — bo'sh."""
    from dashboard.access import registers_of
    from sales.models import SaleItem

    regs = registers_of(wh)
    if not regs:
        return {}
    now = now or timezone.now()
    rows = (SaleItem.objects.filter(sale__shift__register_id__in=regs, sale__kind="sale",
                                    ms_product_id__isnull=False,
                                    sale__created_at__gte=now - timedelta(days=days))
            .values("ms_product_id").annotate(dona=Sum("quantity")))
    return {str(r["ms_product_id"]): Decimal(str(r["dona"] or 0)) for r in rows}


def fetch(client, stores, changed_since=None) -> dict[tuple[str, str], Decimal]:
    params = {"filter": "storeId=" + ",".join(sorted(stores))}
    if changed_since is not None:
        params["changedSince"] = ms.fmt(changed_since)
    rows = client.get("report/stock/bystore/current", **params) or []
    out = {}
    for r in rows if isinstance(rows, list) else []:
        sid, aid, st = r.get("storeId"), r.get("assortmentId"), r.get("stock")
        if not sid or not aid or st is None:
            continue  # qayta hisoblanmoqda — keyingi tekshiruvda olinadi
        out[(str(sid), str(aid))] = Decimal(str(st))
    return out


def observe(client, now=None) -> MinusScan | None:
    """Bitta tekshiruv: MoySklad'dan bitta so'rov, keyin bazada solishtirish."""
    now = now or timezone.now()
    stores = tracked_warehouses()
    if not stores:
        return None
    prev = MinusScan.objects.order_by("-at").first()
    last_full = MinusScan.objects.filter(full=True).order_by("-at").first()
    new_wh = {w for w in stores if not prev or w not in (prev.per_wh or {})}
    # Oldingi tekshiruv eski ko'rinishda (tovar holati ustunlari yo'q) bo'lsa —
    # to'liq tekshiruv: sotuvdagi tovarlar ro'yxati shunda hisoblanadi
    old_shape = bool(prev) and any(len(row or []) < 5 for row in (prev.per_wh or {}).values())
    full = (prev is None or bool(new_wh) or old_shape or now - prev.at > MAX_DELTA
            or last_full is None or now - last_full.at > FULL_EVERY)
    since = None if full else prev.at - DELTA_OVERLAP
    try:
        values = fetch(client, stores, since)
    except MoySkladError as exc:
        if full or exc.status not in (400, 412):
            raise
        # «Faqat o'zgarganlar» so'rovi qabul qilinmadi — to'liq tekshiruv
        logger.warning("KPI minus: changedSince qabul qilinmadi (%s) — to'liq tekshiruv", exc.status)
        full, values = True, fetch(client, stores, None)
    return apply(values, full=full, stores=stores, now=now)


def _product_info(pid: str) -> tuple[Product | None, str, str, str]:
    p = Product.objects.filter(ms_id=pid).first()
    if not p:
        return None, "", "", ""
    bc = p.barcodes.order_by("pk").values_list("value", flat=True).first() or ""
    return p, p.name, p.code or "", bc


@transaction.atomic
def apply(values: dict, *, full: bool, stores: dict, now=None, rule: KpiQoida | None = None) -> MinusScan:
    """Qoldiqlarni ochiq minuslar bilan solishtiradi. Takroriy chaqiruv
    (bir xil ma'lumot) yangi yozuv yaratmaydi."""
    now = now or timezone.now()
    rule = rule or KpiQoida.current()
    prev = MinusScan.objects.select_for_update().order_by("-at").first()
    seen_wh = set((prev.per_wh or {}) if prev else {})
    new_wh = {w for w in stores if w not in seen_wh}
    today = ms.local_date(now)

    open_map = {
        (str(i.warehouse_ms_id), str(i.product_ms_id)): i
        for i in MinusIssue.objects.select_for_update().filter(status=MinusIssue.OPEN)
    }
    opened = closed = 0
    owners: dict[str, Tovaroved | None] = {}

    def close(issue, qty):
        nonlocal closed
        issue.status = MinusIssue.CLOSED
        issue.closed_at = now
        issue.closed_prev_scan = prev.at if prev else None
        if qty is not None:
            issue.last_qty = qty
        issue.save(update_fields=["status", "closed_at", "closed_prev_scan", "last_qty"])
        closed += 1

    for key, qty in values.items():
        wh, pid = key
        if wh not in stores:
            continue
        issue = open_map.get(key)
        if qty < 0:
            if issue:
                issue.last_seen, issue.last_qty = now, qty
                issue.min_qty = min(issue.min_qty, qty)
                issue.save(update_fields=["last_seen", "last_qty", "min_qty"])
                continue
            pre = wh in new_wh
            product, name, code, bc = _product_info(pid)
            previous = (MinusIssue.objects.filter(warehouse_ms_id=wh, product_ms_id=pid,
                                                  status=MinusIssue.CLOSED)
                        .order_by("-closed_at").first())
            if previous and previous.closed_at and now - previous.closed_at <= timedelta(days=rule.qayta_paydo_kun):
                # Yopilgan edi, qisqa vaqtda qaytdi — avvalgi «yechim» sifatli emas
                if not previous.reopened:
                    previous.reopened = True
                    previous.save(update_fields=["reopened"])
            if wh not in owners:
                owners[wh] = responsible(wh, today)
            hours = rule.oldingi_tuzatish_soat if pre else rule.tuzatish_soat
            open_map[key] = MinusIssue.objects.create(
                warehouse_ms_id=wh, warehouse_name=stores.get(wh, ""),
                product=product, product_ms_id=pid, name=name, code=code, barcode=bc,
                first_seen=now, first_seen_prev_scan=None if pre or not prev else prev.at,
                last_seen=now, first_qty=qty, min_qty=qty, last_qty=qty,
                pre_tracking=pre, deadline=work_deadline(now, hours, rule.kun_boshi, rule.kun_oxiri),
                tovaroved=owners[wh], previous=previous,
            )
            opened += 1
        elif issue:
            close(issue, qty)
            del open_map[key]

    if full:
        # To'liq ro'yxatda yo'q juftlik — qoldig'i 0: minus yo'qolgan
        for key, issue in list(open_map.items()):
            if key[0] in stores and key not in values:
                close(issue, Decimal(0))
                del open_map[key]

    # O'zgarmagan ochiq minuslar hali ham minusda — oxirgi ko'rilgan vaqt
    MinusIssue.objects.filter(status=MinusIssue.OPEN, warehouse_ms_id__in=list(stores)).update(last_seen=now)

    neg = {
        str(wh): n for wh, n in
        MinusIssue.objects.filter(status=MinusIssue.OPEN, warehouse_ms_id__in=list(stores))
        .values("warehouse_ms_id").annotate(n=Count("id")).values_list("warehouse_ms_id", "n")
    }
    holat = tovar_holatlari(values, full=full, stores=stores, now=now, rule=rule, owners=owners, new_wh=new_wh)

    old = (prev.per_wh or {}) if prev else {}
    per_wh = {}
    for wh in stores:
        if full:
            # Faol tovar (qoldiq ta'rifi): qoldig'i 0 dan farqli tovarlar soni
            active = sum(1 for (w, _), q in values.items() if w == wh and q != 0)
        else:
            active = (old.get(wh) or [0, None])[1]
        # [minus, faol, 0 qoldiqdagi sotuv tovarlari, sotuv tovarlari jami, narx/tannarx holatlari]
        h = holat.get(wh) or {}
        prev_row = old.get(wh) or []
        selling = h.get("selling") if full else (prev_row[3] if len(prev_row) > 3 else None)
        per_wh[wh] = [neg.get(wh, 0), active, h.get("nol", 0), selling, h.get("narx", 0)]

    scan = MinusScan.objects.create(
        at=now, first=prev is None, full=full, negative=sum(neg.values()),
        opened=opened, closed=closed, per_wh=per_wh,
    )
    logger.info("KPI minus: %s ochiq, +%s yangi, -%s yopildi (%s); tovar holati: tugagan %s / sotuvdagi %s, narx %s",
                scan.negative, opened, closed, "to'liq" if full else "o'zgarganlar",
                sum(r[2] or 0 for r in per_wh.values()), sum(r[3] or 0 for r in per_wh.values()),
                sum(r[4] or 0 for r in per_wh.values()))
    return scan


# ---------------------------------------------------------------- tovar holati
# (egasi, 2026-11-10): «Tovar yo'qligi» va «Narx nazorati» mezonlari uchun.

#: Qoldig'i yuritiladigan tovar turlari — xizmat va komplektning qoldig'i
#: bo'lmaydi, ular «tugagan» hisoblanmaydi
STOCK_KINDS = (Product.KIND_PRODUCT, Product.KIND_VARIANT)


def _stocked(p: Product | None) -> bool:
    return p is not None and p.kind in STOCK_KINDS and not p.archived


def _price_kind(p: Product) -> str:
    if not p.sale_price:
        return TovarHolat.NARXSIZ
    if p.buy_price and p.sale_price < p.buy_price:
        return TovarHolat.ARZON
    return ""


def selling_products_for(wh: str, pids: list, days: int, now=None) -> dict[str, Decimal]:
    """`selling_products` ning faqat berilgan tovarlar uchun tez varianti."""
    from dashboard.access import registers_of
    from sales.models import SaleItem

    regs = registers_of(wh)
    if not regs or not pids:
        return {}
    now = now or timezone.now()
    rows = (SaleItem.objects.filter(sale__shift__register_id__in=regs, sale__kind="sale",
                                    ms_product_id__in=pids,
                                    sale__created_at__gte=now - timedelta(days=days))
            .values("ms_product_id").annotate(dona=Sum("quantity")))
    return {str(r["ms_product_id"]): Decimal(str(r["dona"] or 0)) for r in rows}


def tovar_holatlari(values, *, full, stores, now, rule, owners, new_wh) -> dict[str, dict]:
    """Qoldiq tekshiruvi natijasidan tovar holatlarini yangilaydi.

    nol     — sotuvdagi tovar (oxirgi `faol_sotuv_kun` kunda shu do'kon kassalarida
              sotilgan; xizmat, komplekt va arxivlangan tovar kirmaydi) omborda 0:
              to'liq tekshiruvda ro'yxatda yo'q, o'zgarganlar tekshiruvida qoldiq 0
              kelgan. Qoldiq paydo bo'lsa (yoki minusga o'tsa) yopiladi; sotuvdan
              chiqib ketsa (davrda sotilmagan) — to'liq tekshiruvda yopiladi.
    narxsiz — omborda bor (qoldiq > 0), chakana narxi 0.
    arzon   — omborda bor, chakana narxi tannarxdan past (tannarx ma'lum bo'lsa).
    Narx holatlari har tekshiruvda katalogdagi joriy narx bilan qayta ko'riladi;
    yangi narx holati to'liq tekshiruvda (yoki qoldig'i o'zgargan tovarda) ochiladi.

    Yozuvlar to'plab yoziladi (bulk) — birinchi tekshiruvda minglab bo'lishi mumkin.

    Qaytaradi: {ombor: {"nol": ochiq_soni, "selling": sotuv_tovarlari, "narx": ochiq_soni}}
    (selling faqat to'liq tekshiruvda hisoblanadi)."""
    out: dict[str, dict] = {}
    open_h = {
        (h.kind, str(h.warehouse_ms_id), str(h.product_ms_id)): h
        for h in TovarHolat.objects.select_for_update().filter(status=TovarHolat.OPEN, warehouse_ms_id__in=list(stores))
    }
    by_wh: dict[str, dict[str, Decimal]] = {}
    for (w, pid), q in values.items():
        if w in stores:
            by_wh.setdefault(w, {})[pid] = q

    # Sotilgan tovarlar: to'liq tekshiruvda — hammasi; o'zgarganlarda — faqat
    # qoldig'i 0 bo'lib kelganlari orasidan
    sold_by_wh: dict[str, dict[str, Decimal]] = {}
    for wh in stores:
        if full:
            sold_by_wh[wh] = selling_products(wh, rule.faol_sotuv_kun, now)
        else:
            zeros = [pid for pid, q in by_wh.get(wh, {}).items() if q == 0]
            sold_by_wh[wh] = selling_products_for(wh, zeros, rule.faol_sotuv_kun, now) if zeros else {}

    pids = list({pid for d in by_wh.values() for pid in d} | {k[2] for k in open_h}
                | {pid for d in sold_by_wh.values() for pid in d})
    product_cache: dict[str, Product] = {}
    for i in range(0, len(pids), 5000):       # bitta so'rovda juda ko'p parametr bo'lmasin
        product_cache.update({
            str(p.ms_id): p for p in Product.objects.filter(ms_id__in=pids[i:i + 5000])
            .only("ms_id", "name", "code", "kind", "archived", "sale_price", "buy_price")
        })
    # Mezon (yoki shu tur) endi kiritildi — birinchi topilganlar «kuzatuvdan oldingi»
    seen_kinds = set(TovarHolat.objects.values_list("kind", flat=True).distinct())

    to_create: list[TovarHolat] = []
    to_close: list[int] = []

    def open_(kind, wh, pid, owner, **extra):
        p = product_cache.get(pid)
        h = TovarHolat(
            kind=kind, warehouse_ms_id=wh, warehouse_name=stores.get(wh, ""),
            product=p, product_ms_id=pid, name=p.name if p else "", code=((p.code or "") if p else "")[:64],
            first_seen=now, last_seen=now, pre_tracking=wh in new_wh or kind not in seen_kinds,
            tovaroved=owner, **extra,
        )
        to_create.append(h)
        open_h[(kind, wh, pid)] = h

    def close_(key):
        h = open_h.pop(key)
        if h.pk:
            to_close.append(h.pk)
        elif h in to_create:          # shu tekshiruvda ochilgan edi — yozilmaydi
            to_create.remove(h)

    for wh in stores:
        if wh not in owners:
            owners[wh] = responsible(wh, ms.local_date(now))
        owner = owners[wh]
        q_of = by_wh.get(wh, {})
        sold = {pid: dona for pid, dona in sold_by_wh[wh].items() if _stocked(product_cache.get(pid))}
        selling = sold if full else None
        zero = {pid for pid in sold if q_of.get(pid, Decimal(0)) == 0}

        # --- nol: ochish / yopish
        for pid in zero:
            if (TovarHolat.NOL, wh, pid) not in open_h:
                open_(TovarHolat.NOL, wh, pid, owner, sotuv_dona=sold.get(pid, Decimal(0)))
        for key in [k for k in open_h if k[0] == TovarHolat.NOL and k[1] == wh and k[2] not in zero]:
            q = q_of.get(key[2])
            # Qoldiq paydo bo'ldi (yoki minusga o'tdi — endi MinusIssue da), yoki
            # to'liq tekshiruvda endi sotuv tovari emas (davrda sotilmagan)
            if (q is not None and q != 0) or full:
                close_(key)

        # --- narxsiz / arzon: qoldig'i kelgan tovarlar
        for pid, q in q_of.items():
            p = product_cache.get(pid)
            want = _price_kind(p) if q > 0 and _stocked(p) else ""
            for kind in (TovarHolat.NARXSIZ, TovarHolat.ARZON):
                key = (kind, wh, pid)
                if kind == want:
                    if key not in open_h:
                        open_(kind, wh, pid, owner, sale_price=p.sale_price, buy_price=p.buy_price)
                elif key in open_h:
                    close_(key)
        # Qoldig'i bu safar kelmagan ochiq narx holatlari: narxi katalogda
        # tuzatilgan bo'lsa yoki (to'liq tekshiruvda) omborda qolmagan bo'lsa — yopiladi
        for key in [k for k in open_h if k[0] != TovarHolat.NOL and k[1] == wh and k[2] not in q_of]:
            p = product_cache.get(key[2])
            if full or not _stocked(p) or _price_kind(p) != key[0]:
                close_(key)

        out[wh] = {
            "nol": sum(1 for k in open_h if k[0] == TovarHolat.NOL and k[1] == wh),
            "narx": sum(1 for k in open_h if k[0] != TovarHolat.NOL and k[1] == wh),
            "selling": len(selling) if selling is not None else None,
        }

    for i in range(0, len(to_close), 1000):
        TovarHolat.objects.filter(pk__in=to_close[i:i + 1000]).update(status=TovarHolat.CLOSED, closed_at=now)
    # Yopilganlardan KEYIN: bir do'kon + tovar + tur uchun bitta ochiq yozuv cheklovi
    TovarHolat.objects.bulk_create(to_create, batch_size=500)
    # O'zgarmagan ochiq holatlar hali ham shunday — oxirgi ko'rilgan vaqt
    TovarHolat.objects.filter(status=TovarHolat.OPEN, warehouse_ms_id__in=list(stores)).update(last_seen=now)
    return out
