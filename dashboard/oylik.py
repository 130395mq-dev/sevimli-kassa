"""Oylar kesimida savdo: o'sish bormi yoki yo'qmi — MoySklad tarixidan.

Egasining so'rovi (2026-09-27): «oylar kesimida o'sish bor yoki yo'q aytib
tursin». Bizning bazada to'liq savdo faqat 2026-09-17 dan bor, oldingi oylar
(F-Kassa davri) faqat MoySklad'da — shuning uchun oylik raqamlar MoySklad
«Показатели продаж» hisobotidan (`report/sales/plotseries`) olinadi. Egasi
shu yo'lni tanladi. MoySklad'dan faqat O'QILADI.

Himoyalar:
  * sahifa MoySklad'ni kutib qolmasin — bosh sahifa bu bo'lakni keyin
    alohida so'raydi (`/oylik/`), natija 1 soat keshda turadi;
  * bir vaqtda faqat bitta ishchi MoySklad'ga boradi (kesh-qulf), qolganlari
    eski natijani ko'rsatadi;
  * har so'rov bitta urinish, qisqa kutish — kassalar xizmat qiladigan ishchi
    uzoq band bo'lib qolmaydi;
  * MoySklad javob bermasa — oxirgi yaxshi natija (24 soatgacha) yoki
    tushunarli xabar; sahifa yiqilmaydi.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from . import grafik

logger = logging.getLogger(__name__)

MONTHS = 12
FRESH_TTL = 60 * 60          # 1 soat — yangi natija
STALE_TTL = 24 * 60 * 60     # 24 soat — MoySklad javob bermasa shu ko'rsatiladi
LOCK_TTL = 45
TIMEOUT = 12                 # soniya, bitta so'rov uchun
CACHE_KEY = "oylik:v1"
MONTH_NAMES = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul",
               "avgust", "sentyabr", "oktyabr", "noyabr", "dekabr"]
MONTH_SHORT = ["yan", "fev", "mar", "apr", "may", "iyn", "iyl", "avg", "sen",
               "okt", "noy", "dek"]


class NoData(Exception):
    """MoySklad'dan ma'lumot olib bo'lmadi — sababi bilan."""


def _month_start(d: date, back: int = 0) -> date:
    y, m = d.year, d.month - back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def _ms_moment(d: date, end: bool = False) -> str:
    """Mahalliy kun chegarasi → MoySklad hisobi vaqti (writer.ms_moment)."""
    from sales.writer import ms_moment

    tz = timezone.get_current_timezone()
    dt = timezone.make_aware(datetime.combine(d, time.max if end else time.min), tz)
    return ms_moment(dt.replace(microsecond=0))


def _parse_day(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def _get(client, path: str, **params):
    """Bitta urinish (client.request qayta-qayta urinib, ishchini uzoq
    band qilib qo'yardi — panel uchun bunga arzimaydi)."""
    from moysklad.client import MoySkladClient

    resp = client._session.get(f"{client.base_url}/{path}", params=params,
                               timeout=client.timeout)
    if resp.status_code != 200:
        raise MoySkladClient._build_error(resp)
    return resp.json()


def fetch(client, today: date) -> dict:
    """MoySklad'dan ikki so'rov: oxirgi 12 oy (oylar) va o'tgan oy
    boshidan bugungacha (kunlar). Qaytaradi: xom qatorlar."""
    first_month = _month_start(today, MONTHS - 1)
    months = _get(
        client, "report/sales/plotseries",
        momentFrom=_ms_moment(first_month), momentTo=_ms_moment(today, end=True),
        interval="month",
    )
    prev_first = _month_start(today, 1)
    days = _get(
        client, "report/sales/plotseries",
        momentFrom=_ms_moment(prev_first), momentTo=_ms_moment(today, end=True),
        interval="day",
    )
    return {
        "months": [(_parse_day(r["date"]), float(r.get("sum") or 0) / 100,
                    int(r.get("quantity") or 0)) for r in months.get("series", [])],
        "days": [(_parse_day(r["date"]), float(r.get("sum") or 0) / 100)
                 for r in days.get("series", [])],
    }


def build(raw: dict, today: date) -> dict:
    """Xom qatorlardan: oylar jadvali, grafik va «o'sish bormi» xulosasi.

    Joriy oy ADOLATLI solishtiriladi: o'tgan oyning xuddi shu kunlari bilan
    (bugun hali tugamagan — shuning uchun kechagacha to'liq kunlar).
    """
    cur_first = _month_start(today)
    prev_first = _month_start(today, 1)
    by_month = {d.replace(day=1): (s, q) for d, s, q in raw["months"]}
    months = []
    for back in range(MONTHS - 1, -1, -1):
        m = _month_start(today, back)
        s, q = by_month.get(m, (0.0, 0))
        months.append({"month": m, "name": f"{MONTH_NAMES[m.month - 1]} {m.year}",
                       "short": MONTH_SHORT[m.month - 1], "total": s, "n": q,
                       "current": m == cur_first})
    for i, row in enumerate(months):
        prev = months[i - 1]["total"] if i else 0
        row["delta"] = (round((row["total"] - prev) / prev * 100, 1)
                        if prev and not row["current"] else None)

    # Joriy oy: 1..(bugun-1) va o'tgan oyning 1..(bugun-1)
    full_days = today.day - 1
    daily = dict(raw["days"])
    verdict = None
    if full_days >= 1:
        cur_sum = sum(daily.get(cur_first + timedelta(days=i), 0) for i in range(full_days))
        prev_days = [prev_first + timedelta(days=i) for i in range(full_days)]
        prev_sum = sum(daily.get(d, 0) for d in prev_days if d < cur_first)
        verdict = {
            "kind": "cur",
            "label": f"1–{full_days} {MONTH_NAMES[cur_first.month - 1]}",
            "prev_label": f"1–{min(full_days, (cur_first - prev_first).days)} "
                          f"{MONTH_NAMES[prev_first.month - 1]}",
            "cur": cur_sum, "prev": prev_sum,
        }
    else:
        # Oyning 1-kuni: o'tgan to'liq oy undan oldingisi bilan
        a, b = months[-2], months[-3]
        verdict = {"kind": "full", "label": a["name"], "prev_label": b["name"],
                   "cur": a["total"], "prev": b["total"]}
    p = verdict["prev"]
    verdict["delta"] = round((verdict["cur"] - p) / p * 100, 1) if p else None
    verdict["growth"] = verdict["delta"] is not None and verdict["delta"] > 0

    # Oxirgi 3 ta to'liq oy qaysi tomonga ketyapti
    full = [m for m in months if not m["current"] and m["total"] > 0][-3:]
    ups = sum(1 for m in full if m["delta"] is not None and m["delta"] > 0)
    chart = grafik.chart_bars([m["total"] for m in months],
                              [m["short"] for m in months], w=520, h=200)
    for bar, m in zip(chart.get("bars", []), months):
        bar["current"] = m["current"]
        bar["name"] = m["name"]
    return {"months": list(reversed(months)), "verdict": verdict, "chart": chart,
            "ups": ups, "full_n": len(full), "today": today}


def get(now=None, client=None) -> dict:
    """Keshdan yoki MoySklad'dan. Hech qachon istisno otmaydi:
    {"ok": True, ...} yoki {"ok": False, "error": "..."}."""
    today = timezone.localdate(now)
    key = f"{CACHE_KEY}:{today.isoformat()}"
    fresh = cache.get(key)
    if fresh:
        return fresh
    stale = cache.get(f"{CACHE_KEY}:stale")

    token = getattr(settings, "MOYSKLAD_TOKEN", "")
    if client is None and not token:
        return stale or {"ok": False, "error": "MoySklad tokeni sozlanmagan"}
    if not cache.add(f"{CACHE_KEY}:lock", 1, LOCK_TTL):
        return stale or {"ok": False, "error": "Hozir yuklanmoqda — bir daqiqadan keyin yangilang"}
    try:
        if client is None:
            from moysklad.client import MoySkladClient
            client = MoySkladClient(token=token, timeout=TIMEOUT)
        raw = fetch(client, today)
        data = {"ok": True, **build(raw, today), "fetched_at": timezone.localtime(now)}
        # Tekshirish uchun logga: oxirgi oylar va oxirgi 7 kun (bizning
        # bazadagi kunlik savdo bilan solishtirsa bo'ladi)
        logger.info("Oylik savdo MoySklad'dan olindi: %s | kunlar: %s",
                    ", ".join(f"{m['short']} {m['total']:.0f}" for m in data["months"][:4]),
                    ", ".join(f"{d:%d.%m} {v:.0f}" for d, v in raw["days"][-7:]))
        cache.set(key, data, FRESH_TTL)
        cache.set(f"{CACHE_KEY}:stale", {**data, "stale": True}, STALE_TTL)
        return data
    except Exception as exc:  # noqa: BLE001 — panel yiqilmasin
        logger.warning("Oylik savdo MoySklad'dan olinmadi: %s", exc)
        return stale or {"ok": False, "error": f"MoySklad javob bermadi ({str(exc)[:120]})"}
    finally:
        cache.delete(f"{CACHE_KEY}:lock")
