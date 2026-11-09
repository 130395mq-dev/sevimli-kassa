"""Mijozning tug'ilgan kuni (SEVIMLI BONUS, egasi 2026-11-08).

Kassada karta ochilganda tug'ilgan sana so'raladi: kun va oy majburiy,
yil ixtiyoriy (ko'p mijoz yilini aytmaydi). Tug'ilgan kunida kassir
og'zaki tabriklaydi va chekda FAQAT tabrik so'zi chiqadi — sovg'a ball yo'q.

Matn ko'rinishi: «12.05» yoki «12.05.1990». Ajratgich nuqta, chiziqcha
yoki slesh bo'lishi mumkin — kassir qanday yozsa ham tushunamiz.
"""

from __future__ import annotations

import calendar
import re
from datetime import date

_RE = re.compile(r"^\s*(\d{1,2})\s*[./-]\s*(\d{1,2})(?:\s*[./-]\s*(\d{2}|\d{4}))?\s*$")

MIN_YEAR = 1900


class BirthdayError(ValueError):
    """Sana noto'g'ri yozilgan — kassirga ko'rsatiladigan matn bilan."""


def parse(text: str, today: date | None = None) -> tuple[int, int, int | None]:
    """«KK.OO» yoki «KK.OO.YYYY» → (kun, oy, yil|None).

    Ikki xonali yil («90») 1900-yillar deb olinadi, agar u bugungi yildan
    katta bo'lmasa — «12.05.26» bo'lsa 2026 deb olinadi.
    """
    m = _RE.match(text or "")
    if not m:
        raise BirthdayError("Sana «KK.OO» yoki «KK.OO.YYYY» ko'rinishida bo'lsin, masalan 12.05 yoki 12.05.1990")
    day, month = int(m.group(1)), int(m.group(2))
    year = int(m.group(3)) if m.group(3) else None
    today = today or date.today()
    if year is not None and year < 100:
        year = 2000 + year if 2000 + year <= today.year else 1900 + year
    if not 1 <= month <= 12:
        raise BirthdayError("Oy 1 dan 12 gacha bo'lishi kerak")
    max_day = calendar.monthrange(year, month)[1] if year else (29 if month == 2 else calendar.monthrange(2001, month)[1])
    if not 1 <= day <= max_day:
        raise BirthdayError(f"Bu oyda {day}-kun yo'q")
    if year is not None:
        if year < MIN_YEAR:
            raise BirthdayError("Yil noto'g'ri")
        if date(year, month, day) > today:
            raise BirthdayError("Tug'ilgan sana kelajakda bo'lishi mumkin emas")
    return day, month, year


def text(day: int | None, month: int | None, year: int | None) -> str:
    """(12, 5, None) → «12.05»; (12, 5, 1990) → «12.05.1990»; yo'q → «»."""
    if not day or not month:
        return ""
    base = f"{day:02d}.{month:02d}"
    return f"{base}.{year}" if year else base


def is_today(day: int | None, month: int | None, today: date) -> bool:
    """Tug'ilgan kuni bugunmi. 29-fevralda tug'ilganlar kabisa bo'lmagan
    yilda 1-martda tabriklanadi."""
    if not day or not month:
        return False
    if month == 2 and day == 29 and not calendar.isleap(today.year):
        return today.month == 3 and today.day == 1
    return today.day == day and today.month == month
