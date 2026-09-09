"""To'lov turlari sahifasi.

Eng muhim qoida: to'lov turi O'CHIRILMAYDI, faqat yashiriladi. Aks holda
o'sha tur bilan qilingan eski savdolar, cheklar va Z-hisobotlar buziladi.
"""

from django.contrib.auth.models import User
from django.test import TestCase

from sales.models import PaymentMethod


class PaymentMethodsPageTest(TestCase):
    def setUp(self):
        User.objects.create_superuser("t_admin", "t@a.uz", "pw12345")
        self.client.login(username="t_admin", password="pw12345")
        self.naqd = PaymentMethod.objects.create(
            code="naqd", name="Naqd", is_cash=True, sort=0,
        )
        self.term = PaymentMethod.objects.create(
            code="terminal-1", name="Terminal-1", is_cash=False, sort=1,
        )

    def _post(self, **data):
        return self.client.post("/tolov-turlari/", data)

    def test_sahifa_ochiladi(self):
        r = self.client.get("/tolov-turlari/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Terminal-1")

    # ---- qo'shish ---------------------------------------------------

    def test_yangi_tur_qoshiladi(self):
        self._post(action="create", name="Karta")
        m = PaymentMethod.objects.get(code="karta")
        self.assertEqual(m.name, "Karta")
        self.assertFalse(m.is_cash)
        self.assertTrue(m.active)

    def test_naqd_belgisi_ishlaydi(self):
        self._post(action="create", name="Naqd 2", is_cash="on")
        self.assertTrue(PaymentMethod.objects.get(code="naqd-2").is_cash)

    def test_takroriy_kod_rad_etiladi(self):
        self._post(action="create", name="Naqd")
        self.assertEqual(PaymentMethod.objects.filter(code="naqd").count(), 1)

    def test_qisqa_nom_rad_etiladi(self):
        self._post(action="create", name="K")
        self.assertEqual(PaymentMethod.objects.count(), 2)

    # ---- nom o'zgartirish -------------------------------------------

    def test_nomi_ozgartiriladi(self):
        self._post(action="rename", id=self.term.pk, name="UzCard")
        self.term.refresh_from_db()
        self.assertEqual(self.term.name, "UzCard")
        # Kod tegilmaydi — eski savdolar shu kod bilan bog'langan
        self.assertEqual(self.term.code, "terminal-1")

    # ---- yashirish (o'chirish EMAS) ---------------------------------

    def test_olib_tashlash_ochirmaydi_yashiradi(self):
        self._post(action="toggle", id=self.term.pk)
        self.term.refresh_from_db()
        self.assertFalse(self.term.active)
        # Eng muhimi: yozuv bazada QOLADI
        self.assertTrue(PaymentMethod.objects.filter(pk=self.term.pk).exists())

    def test_qaytariladi(self):
        self._post(action="toggle", id=self.term.pk)
        self._post(action="toggle", id=self.term.pk)
        self.term.refresh_from_db()
        self.assertTrue(self.term.active)

    def test_oxirgi_turni_ochirib_bolmaydi(self):
        """Hech bo'lmasa bitta tur qolsin — aks holda to'lov qilib bo'lmaydi."""
        self._post(action="toggle", id=self.term.pk)   # bittasi yashirildi
        self._post(action="toggle", id=self.naqd.pk)   # oxirgisi — rad etilishi kerak
        self.naqd.refresh_from_db()
        self.assertTrue(self.naqd.active)

    # ---- kassa nimani ko'radi ---------------------------------------

    def test_yashirilgan_tur_kassaga_bermaydi(self):
        """Kassa faqat FAOL turlarni oladi (hello javobi shunga tayanadi)."""
        self._post(action="toggle", id=self.term.pk)
        faol = list(
            PaymentMethod.objects.filter(active=True).values_list("code", flat=True)
        )
        self.assertIn("naqd", faol)
        self.assertNotIn("terminal-1", faol)
