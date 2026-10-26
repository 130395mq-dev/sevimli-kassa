"""MoySklad bilan ishlash uchun kichik yordamchilar (faqat o'qish).

Vaqt: MoySklad rasmiy hujjatida — «значения в полях типа дата-время ...
указаны в часовом поясе: MSK», audit filtridagi `moment` ham MSK. Moskva
UTC+3 (2014 dan beri yozgi vaqt yo'q). Toshkent = MSK + 2 soat.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

MSK = timezone(timedelta(hours=3), "MSK")
TASHKENT = ZoneInfo("Asia/Tashkent")

# Auditdagi `source`: «app» — MoySklad sayti/ilovasida odam qo'lda qilgan
# ish. Qolganlari (jsonapi, remap-1.2, posapi, import, connectors …) —
# dastur yoki integratsiya (masalan, kassa cheklari «Музаффар» nomidan
# JSON API orqali yoziladi — bu uning qo'lda qilgan ishi EMAS).
MANUAL_SOURCES = frozenset({"app"})


def parse(value: str | None) -> datetime | None:
    """«2026-10-26 12:50:00.123» (MSK) → aware datetime."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=MSK)
        except ValueError:
            continue
    return None


def fmt(value: datetime) -> str:
    """Filtr uchun: aware datetime → MSK «YYYY-MM-DD HH:MM:SS»."""
    return value.astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S")


def ms_id(meta: dict | None) -> str | None:
    if not meta:
        return None
    href = (meta.get("meta") or meta).get("href", "") if isinstance(meta, dict) else ""
    if not href:
        return None
    return href.rstrip("/").split("/")[-1].split("?")[0]


def entity_type(meta: dict | None) -> str:
    if not meta:
        return ""
    m = meta.get("meta") or meta
    return m.get("type", "") or ""


def local_date(value: datetime) -> date:
    return value.astimezone(TASHKENT).date()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Toshkent kuni → [boshi, oxiri) aware."""
    start = datetime.combine(day, time.min, tzinfo=TASHKENT)
    return start, start + timedelta(days=1)
