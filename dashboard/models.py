"""Panelning o'z jadvallari (savdo va katalog jadvallari — sales/, catalog/ da)."""

from django.db import models


class OxirgiKirim(models.Model):
    """Tovarning oxirgi kirimi — MoySklad priyomkasi: kimdan kelgan.

    Egasi (2026-11-12): «tugayotgan tovarlarni kim kimga zakaz berishi kerak —
    oxirgi marta kim tomonidan kirim qilingan, shuni olib ko'rsatsin».

    Bitta tovarga bitta qator: butun tarmoq bo'yicha eng oxirgi O'TKAZILGAN
    priyomka (postavshik, sana, ombor, miqdor, kirim narxi). To'ldirish va
    yangilab turish — dashboard/kirim.py (MoySklad'dan faqat o'qiydi).

    Holatlar:
      * `checked_at` bo'sh       — ANIQ EMAS: priyomkasi o'chirilgan/tuzatilgan
                                   yoki juda eski hujjatdan olingan — maydonlar
                                   to'ldirilgan bo'lishi mumkin, lekin sahifada
                                   «aniqlanmoqda» ko'rinadi; tovar MoySklad'dan
                                   bittalab so'ralgach aniq bo'ladi;
      * `topildi=False`          — so'raldi, priyomkasi yo'q (masalan o'zimiz
                                   ishlab chiqaradigan yoki oprixodovaniye bilan
                                   kirgan tovar);
      * `topildi=True`           — pastdagi maydonlar to'ldirilgan;
        `checked_at` to'ldirilgan bo'lsa — shu priyomka aniq oxirgisi.
    """

    product = models.OneToOneField(
        "catalog.Product", on_delete=models.CASCADE, related_name="oxirgi_kirim",
    )
    topildi = models.BooleanField(default=False)

    agent_ms_id = models.UUIDField(null=True, blank=True, db_index=True)
    agent_name = models.CharField(max_length=255, blank=True)
    store_ms_id = models.UUIDField(null=True, blank=True)
    store_name = models.CharField(max_length=255, blank=True)
    moment = models.DateTimeField(null=True, blank=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    price = models.BigIntegerField(null=True, blank=True, help_text="Kirim narxi, tiyinlarda")
    doc_ms_id = models.UUIDField(null=True, blank=True, db_index=True)
    doc_number = models.CharField(max_length=255, blank=True)

    #: MoySklad'dan oxirgi marta so'ralgan vaqt; bo'sh — (qayta) so'rash kerak
    checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Oxirgi kirim"
        verbose_name_plural = "Oxirgi kirimlar"

    def __str__(self) -> str:
        return f"{self.product_id}: {self.agent_name or '—'}"
