"""Do'konlar o'rtasida ko'chirish TAVSIYASI — faqat aytadi, hech narsa ko'chirmaydi.

Egasining so'rovi (2026-09-30): «mening do'konimda sotilmayotgan narsa boshqa
tochkada yaxshi sotilayotgan bo'lishi mumkin — avtomat aniqlab, "senda
sotilmayapti, u tochkada yaxshi sotilyapti" deb tavsiya bersin». Qaror: faqat
tavsiya; MoySklad'da Перемещение hujjati YARATILMAYDI.

Ma'lumot:
  * savdo — MoySklad «Прибыльность по товарам» hisoboti (`report/profit/
    byproduct`), har ombor uchun alohida (`filter=store=…`). Bizning bazada
    faqat kassalarimiz turgan do'kon savdosi bor, boshqa tochkalarniki —
    faqat MoySklad'da. MoySklad'dan faqat O'QILADI;
  * qoldiq — `catalog.Stock` (har 5 daqiqada MoySklad'dan sinxronlanadi).

Qoida (sodda, tushuntirsa bo'ladigan):
  * YOTIB QOLGAN (yuboruvchi) — qoldiq bor, lekin oxirgi 30 kunlik savdo
    sur'atida 60 kundan ko'proqqa yetadi (yoki umuman sotilmagan). O'zida
    30 kunlik savdosi qoladi, ortig'i ko'chirilishi mumkin;
  * YETMAYAPTI (oluvchi) — 30 kunda kamida 10 dona sotilgan (muntazam
    talab), qoldig'i esa 7 kunga ham yetmaydi. Unga 14 kunlik savdo
    yetkaziladi;
  * juftlash — eng tez sotadigan oluvchiga eng ko'p ortig'i bor yuboruvchidan;
    arzimas tavsiya (50 000 so'mdan kam) ko'rsatilmaydi;
  * tartib — ko'chirilsa sotilishi mumkin bo'lgan summa bo'yicha.
Faqat SAVDO QILADIGAN omborlar orasida: 30 kunlik savdosi umumiy savdoning
kamida 1 % i bo'lgan omborlar do'kon hisoblanadi. Birinchi jonli hisobda
(2026-09-30) asosiy sklad 30 kunda 550 000 so'm sotgan (0,006 %) — «> 0»
qoidasi bilan u do'kon bo'lib qolib, skladdagi tovarlar «yotib qolgan» deb
chiqqan va do'kondan do'konga tavsiyalarni siqib chiqargan edi.

Himoya: MoySklad sekin — hisob FONDA bajariladi (bitta oqim, kesh-qulf),
sahifa oxirgi natijani ko'rsatadi, hech qachon MoySklad'ni kutib qolmaydi.
"""

from __future__ import annotations

import csv
import logging
import threading
from datetime import date, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal
from io import StringIO

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

PERIOD_DAYS = 30
MIN_SOLD = 10
SLOW_COVER_DAYS = 60
KEEP_DAYS = 30
LOW_COVER_DAYS = 7
TARGET_DAYS = 14
MIN_VALUE = 50_000 * 100          # tiyin
MIN_SHARE_PCT = 1                 # do'kon: umumiy savdoning kamida 1 % i

FRESH_TTL = 6 * 60 * 60           # 6 soat
STALE_TTL = 3 * 24 * 60 * 60      # MoySklad javob bermasa — 3 kungacha eski natija
LOCK_TTL = 15 * 60
TIMEOUT = 30
CACHE_KEY = "kochirish:v2"         # v2: sklad chiqarildi — eski natija ishlatilmasin
PAGE = 1000

_thread_lock = threading.Lock()


# ---------------------------------------------------------------- hisob

def _is_weighed(product) -> bool:
    uom = (product.get("uom") or "").lower()
    return bool(product.get("is_weight")) or uom in ("кг", "kg", "kilogramm", "килограмм")


def _floor(qty: Decimal, weighed: bool) -> Decimal:
    step = Decimal("0.1") if weighed else Decimal("1")
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def recommend(sales: dict, stock: dict, products: dict, warehouses: dict) -> list[dict]:
    """Sof hisob (bazaga, MoySklad'ga tegmaydi).

    sales — {(ombor_id, product_id): 30 kunda sotilgan soni (qaytarishsiz)}
    stock — {(ombor_id, product_id): hozirgi qoldiq}
    products — {product_id: {"name", "code", "uom", "price" (tiyin), "is_weight"}}
    warehouses — {ombor_id: nomi} — faqat savdo qiladigan omborlar
    """
    per_product: dict = {}
    for (wh, pid), qty in sales.items():
        if wh in warehouses:
            per_product.setdefault(pid, {}).setdefault(wh, [Decimal(0), Decimal(0)])[0] += Decimal(qty)
    # Faqat qayerdadir sotilgan tovarlar — hech qayerda sotilmagan tovarni
    # boshqa joyga ko'chirishdan foyda yo'q.
    for (wh, pid), qty in stock.items():
        if wh in warehouses and pid in per_product:
            per_product[pid].setdefault(wh, [Decimal(0), Decimal(0)])[1] += Decimal(qty)

    days = Decimal(PERIOD_DAYS)
    rows = []
    for pid, by_wh in per_product.items():
        p = products.get(pid)
        if not p or not p.get("price"):
            continue
        weighed = _is_weighed(p)
        sources, targets = [], []
        for wh, (sold, st) in by_wh.items():
            rate = max(sold, Decimal(0)) / days
            cover = (st / rate) if rate > 0 else None
            if st > 0 and (cover is None or cover > SLOW_COVER_DAYS):
                surplus = st - rate * KEEP_DAYS
                if surplus > 0:
                    sources.append({"wh": wh, "sold": sold, "stock": st, "rate": rate,
                                    "cover": cover, "left": surplus})
            if sold >= MIN_SOLD and st / rate < LOW_COVER_DAYS:
                need = rate * TARGET_DAYS - max(st, Decimal(0))
                if need > 0:
                    targets.append({"wh": wh, "sold": sold, "stock": st, "rate": rate,
                                    "cover": st / rate if st > 0 else Decimal(0), "left": need})
        if not sources or not targets:
            continue
        targets.sort(key=lambda t: -t["rate"])
        sources.sort(key=lambda s: (s["rate"], -s["left"]))
        for t in targets:
            for s in sources:
                if t["left"] <= 0:
                    break
                if s["left"] <= 0 or s["wh"] == t["wh"]:
                    continue
                qty = _floor(min(s["left"], t["left"]), weighed)
                if qty <= 0:
                    continue
                value = int(qty * p["price"])
                if value < MIN_VALUE:
                    continue
                s["left"] -= qty
                t["left"] -= qty
                rows.append({
                    "product_id": pid, "name": p["name"], "code": p.get("code") or "",
                    "uom": p.get("uom") or "", "price": p["price"],
                    "from_id": s["wh"], "from": warehouses[s["wh"]],
                    "from_stock": s["stock"], "from_sold": s["sold"],
                    "from_cover": int(s["cover"]) if s["cover"] is not None else None,
                    "to_id": t["wh"], "to": warehouses[t["wh"]],
                    "to_stock": t["stock"], "to_sold": t["sold"],
                    "to_rate": t["rate"], "to_cover": round(float(t["cover"]), 1),
                    "qty": qty, "value": value / 100,
                    # Oluvchida shu miqdor necha kunda sotilib ketadi
                    "sell_days": int((qty / t["rate"]).to_integral_value()) if t["rate"] else None,
                })
    rows.sort(key=lambda r: -r["value"])
    return rows


# ------------------------------------------------------- MoySklad'dan olish

def _ms_moment(d: date, end: bool = False) -> str:
    from sales.writer import ms_moment

    tz = timezone.get_current_timezone()
    dt = timezone.make_aware(datetime.combine(d, time.max if end else time.min), tz)
    return ms_moment(dt.replace(microsecond=0))


def _id_from_href(href: str) -> str:
    return (href or "").rstrip("/").split("/")[-1].split("?")[0]


def fetch_sales(client, warehouses: dict, start: date, end: date) -> tuple[dict, dict]:
    """Har ombor bo'yicha tovar savdosi. Qaytaradi: ({(ombor, ms_id): soni}, {ombor: summa})."""
    from moysklad.client import MoySkladClient

    out: dict = {}
    totals: dict = {}
    for wh in warehouses:
        href = f"{client.base_url}/entity/store/{wh}"
        offset = 0
        totals[wh] = 0
        while True:
            resp = client._session.get(
                f"{client.base_url}/report/profit/byproduct",
                params={"momentFrom": _ms_moment(start), "momentTo": _ms_moment(end, end=True),
                        "filter": f"store={href}", "limit": PAGE, "offset": offset},
                timeout=client.timeout,
            )
            if resp.status_code != 200:
                raise MoySkladClient._build_error(resp)
            data = resp.json()
            rows = data.get("rows") or []
            for r in rows:
                # Ombor savdosi jami — hamma qatorlar (modifikatsiya ham)
                totals[wh] += int(r.get("sellSum") or 0) - int(r.get("returnSum") or 0)
                meta = ((r.get("assortment") or {}).get("meta") or {})
                if meta.get("type") not in ("product", None):
                    continue            # modifikatsiya/xizmat — tavsiyaga kirmaydi
                ms_id = _id_from_href(meta.get("href"))
                qty = Decimal(str(r.get("sellQuantity") or 0)) - Decimal(str(r.get("returnQuantity") or 0))
                if ms_id and qty:
                    out[(wh, ms_id)] = out.get((wh, ms_id), Decimal(0)) + qty
            offset += len(rows)
            size = (data.get("meta") or {}).get("size")
            if len(rows) < PAGE or (size is not None and offset >= size):
                break
    return out, totals


def shop_warehouses(all_wh: dict, totals: dict) -> dict:
    """Do'kon hisoblanadigan omborlar: 30 kunlik savdosi umumiy savdoning
    kamida MIN_SHARE_PCT % i. Tasodifiy bitta-ikkita sotuvi bor sklad
    (masalan, asosiy sklad) do'kon emas."""
    grand = sum(s for s in totals.values() if s > 0)
    if grand <= 0:
        return {}
    return {wh: name for wh, name in all_wh.items()
            if totals.get(wh, 0) > 0 and totals.get(wh, 0) * 100 >= grand * MIN_SHARE_PCT}


def build(now=None, client=None) -> dict:
    """MoySklad savdosi + bazadagi qoldiq → tavsiyalar (sekin: MoySklad)."""
    from catalog.models import Product, Stock, Warehouse

    today = timezone.localdate(now)
    end = today - timedelta(days=1)                  # to'liq kunlar
    start = end - timedelta(days=PERIOD_DAYS - 1)
    all_wh = {str(w.ms_id): w.name for w in Warehouse.objects.filter(archived=False)}
    sales_ms, totals = fetch_sales(client, all_wh, start, end)
    shops = shop_warehouses(all_wh, totals)

    by_ms = {str(ms): pk for pk, ms in Product.objects.values_list("pk", "ms_id")}
    sales = {}
    for (wh, ms), qty in sales_ms.items():
        pk = by_ms.get(ms)
        if pk:
            sales[(wh, pk)] = qty
    pids = {pk for _, pk in sales}
    stock = {
        (str(s), pid): q
        for pid, s, q in Stock.objects.filter(product_id__in=pids)
        .values_list("product_id", "store_ms_id", "quantity")
    }
    products = {
        p.pk: {"name": p.name, "code": p.code, "uom": p.uom_name, "price": p.sale_price,
               "is_weight": p.is_weight}
        for p in Product.objects.filter(pk__in=pids)
    }
    rows = recommend(sales, stock, products, shops)
    logger.info(
        "Ko'chirish tavsiyasi: %s ta; savdo 30 kun (MoySklad): %s",
        len(rows), ", ".join(f"{all_wh.get(w)} {s / 100:.0f}" + ("" if w in shops else " (do'kon emas)")
                             for w, s in totals.items()),
    )
    return {"ok": True, "rows": rows, "start": start, "end": end,
            "warehouses": [{"id": w, "name": n, "sum": totals.get(w, 0) / 100}
                           for w, n in sorted(shops.items(), key=lambda x: x[1].lower())],
            "fetched_at": timezone.localtime(now)}


def _run(token: str, key: str) -> None:
    from django.db import connections

    from moysklad.client import MoySkladClient

    try:
        data = build(client=MoySkladClient(token=token, timeout=TIMEOUT))
        cache.set(key, data, FRESH_TTL)
        cache.set(f"{CACHE_KEY}:stale", {**data, "stale": True}, STALE_TTL)
    except Exception as exc:  # noqa: BLE001 — fon oqimi yiqilmasin
        logger.warning("Ko'chirish tavsiyasi hisoblanmadi: %s", exc)
        cache.set(f"{CACHE_KEY}:error", str(exc)[:200], 10 * 60)
    finally:
        cache.delete(f"{CACHE_KEY}:lock")
        _thread_lock.release()
        connections.close_all()


def get(now=None, refresh=False) -> dict:
    """Keshdan. Yangi natija bo'lmasa hisobni FONDA boshlaydi va eski
    natijani (yoki «hisoblanmoqda») qaytaradi. Hech qachon kutmaydi."""
    today = timezone.localdate(now)
    key = f"{CACHE_KEY}:{today.isoformat()}"
    fresh = None if refresh else cache.get(key)
    if fresh:
        return fresh
    stale = cache.get(f"{CACHE_KEY}:stale")
    token = getattr(settings, "MOYSKLAD_TOKEN", "")
    if not token:
        return stale or {"ok": False, "error": "MoySklad tokeni sozlanmagan"}
    started = False
    if cache.add(f"{CACHE_KEY}:lock", 1, LOCK_TTL) and _thread_lock.acquire(blocking=False):
        try:
            threading.Thread(target=_run, args=(token, key), name="kochirish", daemon=True).start()
            started = True
        except Exception:  # noqa: BLE001
            _thread_lock.release()
            cache.delete(f"{CACHE_KEY}:lock")
    error = cache.get(f"{CACHE_KEY}:error")
    base = stale or {"ok": False}
    return {**base, "loading": True, "started": started, "error": base.get("error") or error}


def filtered(data: dict, src: str = "", dst: str = "") -> list[dict]:
    rows = data.get("rows") or []
    if src:
        rows = [r for r in rows if r["from_id"] == src]
    if dst:
        rows = [r for r in rows if r["to_id"] == dst]
    return rows


def to_csv(data: dict, rows: list[dict]) -> tuple[str, str]:
    from .tovarlar import _csv_num, _csv_text

    buf = StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([f"Ko'chirish tavsiyasi — savdo {data['start']:%d.%m}–{data['end']:%d.%m} (30 kun)"])
    w.writerow(["Kodi", "Nomi", "O'lchov", "Qayerdan", "U yerda qoldiq", "U yerda 30 kunda sotildi",
                "Qayerga", "U yerda qoldiq", "U yerda 30 kunda sotildi", "Tavsiya: soni",
                "Summa, so'm"])
    for r in rows:
        w.writerow([_csv_text(r["code"]), r["name"], r["uom"], r["from"], _csv_num(r["from_stock"]),
                    _csv_num(r["from_sold"]), r["to"], _csv_num(r["to_stock"]), _csv_num(r["to_sold"]),
                    _csv_num(r["qty"]), f"{r['value']:.0f}"])
    return f"sevimli-kochirish-{data['end']:%Y%m%d}.csv", buf.getvalue()
