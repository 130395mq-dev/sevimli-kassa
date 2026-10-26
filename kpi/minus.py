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
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from catalog.models import Product, Warehouse
from moysklad.client import MoySkladError

from . import ms
from .models import Biriktirish, KpiQoida, MinusIssue, MinusScan, Tovaroved

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
    full = (prev is None or bool(new_wh) or now - prev.at > MAX_DELTA
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
                pre_tracking=pre, deadline=now + timedelta(hours=hours),
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
    old = (prev.per_wh or {}) if prev else {}
    per_wh = {}
    for wh in stores:
        if full:
            # Faol tovar (qoldiq ta'rifi): qoldig'i 0 dan farqli tovarlar soni
            active = sum(1 for (w, _), q in values.items() if w == wh and q != 0)
        else:
            active = (old.get(wh) or [0, None])[1]
        per_wh[wh] = [neg.get(wh, 0), active]

    scan = MinusScan.objects.create(
        at=now, first=prev is None, full=full, negative=sum(neg.values()),
        opened=opened, closed=closed, per_wh=per_wh,
    )
    logger.info("KPI minus: %s ochiq, +%s yangi, -%s yopildi (%s)",
                scan.negative, opened, closed, "to'liq" if full else "o'zgarganlar")
    return scan
