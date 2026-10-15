"""Tushib ketgan tovarlar — Sevimli Kassa hali ulanmagan marketlar (MoySklad'dan).

Egasining so'rovi (2026-10-14): «nega un zavod market chiqmayapti». Sabab:
«Tushib ketganlar» savdoni Sevimli Kassa cheklaridan hisoblaydi, un zavod
kassalari esa hali MoySklad kassasida ishlaydi — ularning savdosi bizning
bazaga kelmaydi. Egasi tanlovi: shunday marketlar savdosini MoySklad'dan
o'qish.

Market — MoySklad savdo nuqtasi (`RetailStore`) biriktirilgan ombor, unda
bitta ham Sevimli Kassa yo'q. Savdo — «Прибыльность по товарам»
(`report/profit/byproduct`, `filter=store=…` — «Ko'chirish tavsiyasi» shu
hisobotni ishlatadi), har KUN uchun alohida so'rov: qoida kunlarga
bog'liq («oldin kunlarning kamida yarmida sotilgan»). MoySklad'dan faqat
O'QILADI — hech narsa yozilmaydi.

Himoya (Ko'chirish tavsiyasi bilan bir xil): MoySklad sekin, shuning uchun
yuklash FONDA, bitta oqim, kesh-qulf; sahifa hech qachon kutib qolmaydi.
Har kunning natijasi umumiy keshda (bazada, 4 ishchi uchun bitta):
o'tgan kunlar bir marta olinadi, keyin faqat bugun (soatda bir) va
oxirgi 2 kun (6 soatda bir — MoySklad kassasi oflayn bo'lsa cheklari
kechroq keladi) yangilanadi. Birinchi ochilishda 18 kun ~1–2 daqiqa.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.conf import settings
from django.core.cache import caches
from django.utils import timezone

logger = logging.getLogger(__name__)

CACHE_KEY = "tushgan-ms:v1"
DAY_TTL = 30 * 24 * 60 * 60          # kunlik natija shuncha saqlanadi
TODAY_REFRESH = 60 * 60              # tugamagan kun — soatda bir
RECENT_REFRESH = 6 * 60 * 60         # tugagan, lekin yaqin kun — 6 soatda bir
RECENT_DAYS = 2                      # shundan eski kun yakuniy — qayta so'ralmaydi
LOCK_TTL = 15 * 60
ERROR_TTL = 10 * 60                  # xatodan keyin shuncha vaqt MoySklad'ga bormaydi
TIMEOUT = 30
NETWORK_RETRIES = 2
PAGE = 1000

_thread_lock = threading.Lock()


def _cache():
    return caches["shared"]


def _key(wh: str, d: date) -> str:
    return f"{CACHE_KEY}:{wh}:{d.isoformat()}"


def _lock_key(wh: str) -> str:
    return f"{CACHE_KEY}:lock:{wh}"


def _error_key(wh: str) -> str:
    return f"{CACHE_KEY}:error:{wh}"


def markets(exclude=()) -> dict:
    """Sevimli Kassa ulanmagan marketlar: {ombor ms_id: nomi}.

    Faol MoySklad savdo nuqtalari biriktirilgan omborlar, `exclude`
    (kassasi bor omborlar) dan tashqari."""
    from catalog.models import RetailStore, Warehouse

    skip = {str(x) for x in exclude}
    ids = {str(rs.warehouse_ms_id) for rs in RetailStore.objects.filter(active=True)
           if rs.warehouse_ms_id and str(rs.warehouse_ms_id) not in skip}
    if not ids:
        return {}
    return {str(w.ms_id): w.name
            for w in Warehouse.objects.filter(ms_id__in=list(ids), archived=False)}


def fetch_day(client, wh: str, d: date) -> dict:
    """Bir kunlik savdo: {"rows": {tovar ms_id: [soni (matn), summa tiyin]},
    "total": hamma qatorlar summasi (kun «to'liq ishlagan»mi — shunga qarab)}."""
    from .kochirish import _id_from_href, _ms_moment

    href = f"{client.base_url}/entity/store/{wh}"
    out: dict = {}
    total = 0
    offset = 0
    while True:
        data = client.get(
            "report/profit/byproduct",
            momentFrom=_ms_moment(d), momentTo=_ms_moment(d, end=True),
            filter=f"store={href}", limit=PAGE, offset=offset,
        ) or {}
        rows = data.get("rows") or []
        for r in rows:
            s = int(r.get("sellSum") or 0)
            total += s
            meta = (r.get("assortment") or {}).get("meta") or {}
            if meta.get("type") not in ("product", None):
                continue            # modifikatsiya/xizmat — bizning tovarlar ro'yxatida yo'q
            ms_id = _id_from_href(meta.get("href"))
            qty = Decimal(str(r.get("sellQuantity") or 0))
            if not ms_id or qty <= 0:
                continue
            if ms_id in out:
                q, old = out[ms_id]
                out[ms_id] = [str(Decimal(q) + qty), old + s]
            else:
                out[ms_id] = [str(qty), s]
        offset += len(rows)
        size = (data.get("meta") or {}).get("size")
        if len(rows) < PAGE or (size is not None and offset >= size):
            break
    return {"rows": out, "total": total}


def _needs_refresh(entry: dict, d: date, today: date, now_ts: float) -> bool:
    age = now_ts - float(entry.get("at") or 0)
    if not entry.get("final"):
        return age >= TODAY_REFRESH                 # kun hali tugamagan edi
    if (today - d).days <= RECENT_DAYS:
        return age >= RECENT_REFRESH
    return False


def days_data(wh: str, days: list[date], now=None) -> dict:
    """Keshdagi kunlar; yetishmagan yoki eskirganlari uchun fon yuklashni
    boshlaydi. Hech qachon kutmaydi va istisno otmaydi.

    Qaytaradi: {"days": {kun: {"rows", "total", "at", "final"}},
    "missing": yetishmagan kunlar soni, "loading": fonda yuklanyaptimi,
    "error": matn yoki None, "fetched_at": bugungi ma'lumot vaqti}."""
    cache = _cache()
    now = now or timezone.now()
    today = timezone.localdate(now)
    now_ts = now.timestamp()
    found, todo = {}, []
    for d in days:
        e = cache.get(_key(wh, d))
        if e is None:
            todo.append(d)
            continue
        found[d] = e
        if _needs_refresh(e, d, today, now_ts):
            todo.append(d)
    error = cache.get(_error_key(wh))
    loading = False
    if todo and not error:
        token = getattr(settings, "MOYSKLAD_TOKEN", "")
        if not token:
            error = "MoySklad tokeni sozlanmagan"
        else:
            # Boshlab bo'lmasa ham (boshqa ishchi/ombor band) — yuklanyapti:
            # sahifa 5 soniyada qayta so'raydi va navbat kelganda boshlanadi.
            _start(token, wh, todo)
            loading = True
    at = (found.get(today) or {}).get("at")
    return {
        "days": found, "missing": sum(1 for d in days if d not in found),
        "loading": loading, "error": error,
        "fetched_at": timezone.localtime(datetime.fromtimestamp(at, tz=dt_timezone.utc))
        if at else None,
    }


def _start(token: str, wh: str, todo: list[date]) -> bool:
    cache = _cache()
    if not _thread_lock.acquire(blocking=False):
        return False
    if not cache.add(_lock_key(wh), 1, LOCK_TTL):
        _thread_lock.release()
        return False
    try:
        threading.Thread(target=_run, args=(token, wh, sorted(set(todo), reverse=True)),
                         name="tushgan-ms", daemon=True).start()
        return True
    except Exception:  # noqa: BLE001
        cache.delete(_lock_key(wh))
        _thread_lock.release()
        return False


def load(client, wh: str, todo: list[date]) -> None:
    """Kunlarni MoySklad'dan olib keshga yozadi (xatoda istisno otadi)."""
    cache = _cache()
    for d in todo:
        data = fetch_day(client, wh, d)
        now = timezone.now()
        data["at"] = now.timestamp()
        data["final"] = timezone.localdate(now) > d    # kun tugagandan keyin olinganmi
        cache.set(_key(wh, d), data, DAY_TTL)
    cache.delete(_error_key(wh))


def _run(token: str, wh: str, todo: list[date]) -> None:
    from django.db import connections

    from moysklad.client import MoySkladClient

    cache = _cache()
    try:
        load(MoySkladClient(token=token, timeout=TIMEOUT, network_retries=NETWORK_RETRIES),
             wh, todo)
        logger.info("Tushib ketganlar (MoySklad): ombor %s — %s kun yuklandi", wh, len(todo))
    except Exception as exc:  # noqa: BLE001 — fon oqimi yiqilmasin
        logger.warning("Tushib ketganlar (MoySklad) yuklanmadi: ombor %s: %s", wh, exc)
        cache.set(_error_key(wh), f"MoySklad javob bermadi ({str(exc)[:150]})", ERROR_TTL)
    finally:
        cache.delete(_lock_key(wh))
        _thread_lock.release()
        connections.close_all()
