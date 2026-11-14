"""Mijozlarga SMS (egasi, 2026-11-11: «mijozlarga SMS boradigan qilishimiz
kerak, masalan tug'ilgan kuningiz bilan deb»).

Qoidalar (2026-yil 1-noyabrdan kuchda, 15.07.2026 dagi prezident qarori):

  * reklama SMS faqat mijozning yozma yoki elektron roziligi bilan yuboriladi;
  * 18:00 dan 09:00 gacha reklama SMS yuborish mumkin emas.

Tabrik reklama hisoblanadimi — buni SMS xizmati (operator) matnni
tasdiqlaganda o'zi belgilaydi. Shuning uchun biz HAR DOIM ikkala qoidaga amal
qilamiz: SMS faqat rozi bo'lgan mijozga (`Customer.sms_rozi`) va faqat kunduzi
ketadi. Qoidalarning o'zi — sms/xizmat.py da.

Ikki jadval:

  * `SmsSozlama` — bitta qator: yoqilgan/o'chiq, matn, soat, kunlik chegara;
  * `SmsXabar` — har bir SMS jurnali: kimga, qachon, holati, xatosi.
    Bir mijozga bir yilda BITTA tabrik, bitta telefonga kuniga BITTA —
    bazadagi cheklovlar bilan kafolatlanadi (ikki jarayon bir vaqtda ishlasa
    ham ikki marta ketmaydi).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


def standart_matn() -> str:
    """Boshlang'ich tabrik matni — lotincha, oddiy belgilar bilan (1 ta SMS)."""
    market = getattr(settings, "MARKET_NAME", "") or "Sevimli Market"
    return (f"Hurmatli mijoz! {market} sizni tug'ilgan kuningiz bilan tabriklaydi. "
            "Sog'liq va baxt tilaymiz!")


class SmsSozlama(models.Model):
    """SMS sozlamasi — bitta qator (egasi panelda o'zgartiradi)."""

    #: Qonun bo'yicha reklama SMS 09:00–18:00 oralig'ida. Soat shu ichida tanlanadi.
    SOAT_MIN, SOAT_MAX = 9, 17

    yoqilgan = models.BooleanField("Tug'ilgan kun tabrigi yoqilgan", default=False)
    matn = models.CharField("Tabrik matni", max_length=480, default=standart_matn)
    soat = models.PositiveSmallIntegerField("Yuborish soati", default=10)
    kunlik_chegara = models.PositiveIntegerField("Kuniga eng ko'p SMS", default=300)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=150, blank=True)

    class Meta:
        verbose_name = "SMS sozlamasi"
        verbose_name_plural = "SMS sozlamalari"

    def __str__(self) -> str:
        return "SMS sozlamasi"

    @classmethod
    def get(cls) -> "SmsSozlama":
        obj = cls.objects.first()
        if obj is None:
            obj = cls.objects.create()
        return obj


class SmsXabar(models.Model):
    """Bitta SMS — jurnal yozuvi."""

    TUGILGAN_KUN, SINOV = "tugilgan_kun", "sinov"
    TURLAR = [(TUGILGAN_KUN, "Tug'ilgan kun tabrigi"), (SINOV, "Sinov")]

    NAVBATDA, QAYTA, YUBORILDI, YETKAZILDI, YETMADI, XATO, NOMALUM = (
        "navbatda", "qayta", "yuborildi", "yetkazildi", "yetmadi", "xato", "nomalum")
    HOLATLAR = [
        (NAVBATDA, "Navbatda"),
        (QAYTA, "Qayta uriniladi"),
        (YUBORILDI, "Yuborildi"),
        (YETKAZILDI, "Yetkazildi"),
        (YETMADI, "Yetkazilmadi"),
        (XATO, "Yuborilmadi"),
        (NOMALUM, "Noma'lum"),
    ]
    #: SMS xizmati qabul qilgan (pul yechilgan bo'lishi mumkin) holatlar
    QABUL_QILINGAN = (YUBORILDI, YETKAZILDI, YETMADI)

    customer = models.ForeignKey(
        "catalog.Customer", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="sms_xabarlar",
    )
    tur = models.CharField(max_length=16, choices=TURLAR, db_index=True)
    #: Tabrik qaysi yil uchun (bir yilda bitta) va qaysi kuni (Toshkent
    #: sanasi; bitta telefonga kuniga bitta). Sinov SMS'da ikkalasi bo'sh.
    yil = models.PositiveSmallIntegerField(null=True, blank=True)
    sana = models.DateField(null=True, blank=True)
    telefon = models.CharField(max_length=16)          # 998XXXXXXXXX
    matn = models.TextField()
    qismlar = models.PositiveSmallIntegerField(default=1)

    holat = models.CharField(max_length=12, choices=HOLATLAR, default=NAVBATDA, db_index=True)
    xato = models.CharField(max_length=300, blank=True)
    urinish = models.PositiveSmallIntegerField(default=0)

    provayder = models.CharField(max_length=16, blank=True)
    provayder_id = models.CharField(max_length=64, blank=True)
    provayder_holat = models.CharField(max_length=32, blank=True)
    #: SMS xizmati aytgan narx (so'm) va «reklama» belgisi — bilgan bo'lsa
    narx = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    reklama = models.BooleanField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    holat_at = models.DateTimeField(null=True, blank=True)   # holat oxirgi tekshirilgan vaqt
    #: Sinov SMS'ni yuborgan panel foydalanuvchisi
    kim = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        verbose_name = "SMS"
        verbose_name_plural = "SMS'lar"
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "tur", "yil"], condition=Q(tur="tugilgan_kun"),
                name="sms_yiliga_bitta_tabrik",
            ),
            # Bitta odam ikki marta ro'yxatga olingan bo'lsa (ikki yozuv, bitta
            # telefon) — o'sha kuni unga baribir bitta SMS ketadi.
            models.UniqueConstraint(
                fields=["telefon", "tur", "sana"], condition=Q(tur="tugilgan_kun"),
                name="sms_telefonga_kuniga_bitta",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_tur_display()} → {self.telefon} ({self.get_holat_display()})"

    @property
    def telefon_korinish(self) -> str:
        """998901234567 → +998 90 123 45 67."""
        t = self.telefon
        if len(t) == 12 and t.isdigit():
            return f"+{t[:3]} {t[3:5]} {t[5:8]} {t[8:10]} {t[10:]}"
        return t
