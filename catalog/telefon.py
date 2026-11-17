"""Telefon raqamini bir xil ko'rinishga keltirish.

Egasi (2026-11-17): «bazadagi nomerlarni to'g'rilab chiqish kerak — +998XXXXXXXXX,
bir xil bo'lishi uchun». Bazada raqam har xil yozilgan edi («+998 (90) 123-45-67»,
«901234567», «998901234567»…): qidirish va SMS uchun noqulay.

Qoida:

  * to'g'ri raqam — O'zbekiston raqami: 9 ta raqam (kod + nomer) yoki oldida 998;
    probel, qavs, chiziqcha, nuqta va «+» e'tiborga olinmaydi. Natija: «+998901234567»;
  * bitta maydonda bir nechta raqam (vergul, «;», «/» yoki yangi qator bilan) —
    har biri alohida tartiblanadi va «, » bilan yoziladi;
  * tushunib bo'lmaydigan yozuv (harf aralash, raqami kam yoki ko'p, chet el raqami)
    O'ZGARTIRILMAYDI — egasi panelda ko'rib, manbada (MoySklad) o'zi to'g'rilaydi.
"""

from __future__ import annotations

import re

#: Tartibli yozuv: «+998901234567» yoki «+998901234567, +998911234567»
TARTIBLI = r"^\+998[1-9][0-9]{8}(, \+998[1-9][0-9]{8})*$"

_AJRATUVCHI = re.compile(r"[,;/\n]+")
#: Raqam ichida uchrashi mumkin bo'lgan belgilar (harf bo'lsa — bu izoh, tegilmaydi)
_RAQAM_YOZUVI = re.compile(r"^[0-9+()\-. \t\u00a0]+$")


def bitta(raw: str) -> str:
    """Bitta raqamni «+998XXXXXXXXX» ga keltiradi; tushunib bo'lmasa — «»."""
    raw = (raw or "").strip()
    if not raw or not _RAQAM_YOZUVI.match(raw):
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 9:
        digits = "998" + digits
    if len(digits) == 12 and digits.startswith("998") and digits[3] != "0":
        return "+" + digits
    return ""


def tartibla(raw: str) -> str | None:
    """Maydon qiymatini bir xil ko'rinishga keltiradi.

    Bo'sh — «»; hamma raqami to'g'ri — tartibli yozuv; bittasi ham tushunarsiz
    bo'lsa — None (yozuvga tegilmasin)."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    out: list[str] = []
    for qism in _AJRATUVCHI.split(raw):
        if not qism.strip():
            continue
        tel = bitta(qism)
        if not tel:
            return None
        if tel not in out:
            out.append(tel)
    return ", ".join(out) if out else None


#: `Customer.phone` ustunining uzunligi
ENG_UZUN = 64


def saqlash_uchun(raw: str) -> tuple[str, str]:
    """Bazaga yoziladigan juftlik: (telefon, asl yozuv).

    Tartiblab bo'lsa — (tartibli, asl yozuv yoki «» agar farq qilmasa);
    bo'lmasa (yoki tartibli yozuv ustunga sig'masa) — (asl yozuvning o'zi, «»)."""
    raw = (raw or "").strip()
    yangi = tartibla(raw)
    if yangi is None or yangi == raw or len(yangi) > ENG_UZUN:
        return raw, ""
    return yangi, raw


def qidiruv_raqami(q: str) -> str:
    """Qidiruv so'zidagi raqamlar — telefon qanday yozilsa ham topilsin
    («91 588-50-00» → «915885000»). Raqam kam (4 tadan kam) bo'lsa yoki so'zning
    o'zi faqat raqam bo'lsa — «» (qo'shimcha shart kerak emas)."""
    raqamlar = "".join(ch for ch in (q or "") if ch.isdigit())
    return raqamlar if len(raqamlar) >= 4 and raqamlar != q else ""


def tartibli(raw: str) -> bool:
    """Yozuv allaqachon tartiblimi (bo'sh — ham tartibli)."""
    return not raw or re.match(TARTIBLI, raw) is not None
