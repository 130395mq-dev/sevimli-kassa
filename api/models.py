"""API ilovasining o'z jadvallari."""
from django.db import models


class LoginThrottle(models.Model):
    """Kirish urinishlari hisoblagichi (audit I17).

    Nega bazada: gunicorn bir nechta jarayon bilan ishlaydi, Django keshi esa
    har jarayonda alohida (LocMem). Kesh bo'yicha cheklov amalda «jarayonlar
    soni x chegara» bo'lib qolardi. Baza — hamma jarayon uchun bitta.

    Faqat hisoblagich: parol, token, IP'dan boshqa shaxsiy ma'lumot yo'q.
    O'chirilsa hech narsa buzilmaydi — cheklov noldan boshlanadi.
    """

    key = models.CharField("Kalit", max_length=220, unique=True)
    failures = models.PositiveIntegerField("Xato urinishlar", default=0)
    window_start = models.DateTimeField("Hisob boshlangan")
    blocked_until = models.DateTimeField("Yopiq (gacha)", null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Kirish cheklovi"
        verbose_name_plural = "Kirish cheklovlari"

    def __str__(self) -> str:
        return self.key
