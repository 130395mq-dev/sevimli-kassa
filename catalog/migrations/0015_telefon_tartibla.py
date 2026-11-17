"""Bazadagi telefon raqamlarini bir xil ko'rinishga keltiradi: +998XXXXXXXXX.

Egasi (2026-11-17): «bazadagi nomerlarni to'g'rilab chiqish kerak… bir xil bo'lishi
uchun… nomerlari o'zgarib ketmasin».

  * FAQAT YOZILISHI o'zgaradi: «915885000», «998919012189», «+998 (91) 588-50-00»
    → «+998915885000». Raqamlarning o'zi o'zgarmaydi — har bir yozuvda oxirgi
    9 ta raqam avvalgisi bilan bir xil ekani tekshiriladi, bir xil bo'lmasa
    yozuvga tegilmaydi.
  * Tushunib bo'lmaydigan yozuvlar (harf aralash, raqami kam/ko'p, chet el raqami)
    O'ZGARTIRILMAYDI.
  * Eski yozuv `phone_asl` da saqlanadi; migratsiya orqaga qaytarilsa tiklanadi.

Qoida ataylab shu faylning o'zida yozilgan (catalog/telefon.py dan olinmagan):
migratsiya keyin kod o'zgarsa ham aynan shu ishni qiladi.
"""

import re

from django.db import migrations

_AJRATUVCHI = re.compile(r"[,;/\n]+")
_RAQAM_YOZUVI = re.compile(r"^[0-9+()\-. \t\u00a0]+$")
_TARTIBLI = re.compile(r"^\+998[1-9][0-9]{8}(, \+998[1-9][0-9]{8})*$")
ENG_UZUN = 64
BOLAK = 500


def _bitta(raw):
    raw = (raw or "").strip()
    if not raw or not _RAQAM_YOZUVI.match(raw):
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 9:
        digits = "998" + digits
    if len(digits) == 12 and digits.startswith("998") and digits[3] != "0":
        return "+" + digits
    return ""


def _tartibla(raw):
    """Tartibli yozuv yoki None (tegilmasin)."""
    out = []
    for qism in _AJRATUVCHI.split((raw or "").strip()):
        if not qism.strip():
            continue
        tel = _bitta(qism)
        if not tel:
            return None
        if tel not in out:
            out.append(tel)
    return ", ".join(out) if out else None


def _oxirgi_9(raw):
    """Yozuvdagi har bir raqamning oxirgi 9 tasi — raqam o'zgarmaganini tekshirish uchun."""
    out = []
    for qism in _AJRATUVCHI.split(raw or ""):
        digits = "".join(ch for ch in qism if ch.isdigit())
        if digits and digits[-9:] not in out:
            out.append(digits[-9:])
    return out


def tartibla(apps, schema_editor):
    Customer = apps.get_model("catalog", "Customer")
    ozgardi = tegilmadi = 0
    bolak = []
    for c in Customer.objects.exclude(phone="").only("id", "phone", "phone_asl").iterator(chunk_size=2000):
        eski = c.phone
        if _TARTIBLI.match(eski):
            continue
        yangi = _tartibla(eski)
        # raqamning o'zi o'zgarmasligi shart: oxirgi 9 ta raqam bir xil bo'lsin
        if yangi is None or len(yangi) > ENG_UZUN or _oxirgi_9(yangi) != _oxirgi_9(eski):
            tegilmadi += 1
            continue
        c.phone, c.phone_asl = yangi, eski[:ENG_UZUN]
        bolak.append(c)
        ozgardi += 1
        if len(bolak) >= BOLAK:
            Customer.objects.bulk_update(bolak, ["phone", "phone_asl"])
            bolak = []
    if bolak:
        Customer.objects.bulk_update(bolak, ["phone", "phone_asl"])
    if ozgardi or tegilmadi:
        print(f" telefonlar: {ozgardi} tasi +998XXXXXXXXX ko'rinishiga keltirildi, "
              f"{tegilmadi} tasiga tegilmadi (tushunib bo'lmadi)", end="")


def qaytar(apps, schema_editor):
    Customer = apps.get_model("catalog", "Customer")
    bolak = []
    for c in Customer.objects.exclude(phone_asl="").only("id", "phone", "phone_asl").iterator(chunk_size=2000):
        c.phone, c.phone_asl = c.phone_asl, ""
        bolak.append(c)
        if len(bolak) >= BOLAK:
            Customer.objects.bulk_update(bolak, ["phone", "phone_asl"])
            bolak = []
    if bolak:
        Customer.objects.bulk_update(bolak, ["phone", "phone_asl"])


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0014_customer_phone_asl"),
    ]

    operations = [
        migrations.RunPython(tartibla, qaytar),
    ]
