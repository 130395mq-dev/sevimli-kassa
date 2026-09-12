"""Panel: tiqilib qolgan (stuck) cheklarni «Qayta yuborish» tugmasi.

Sabab tuzatilgach (masalan MoySklad'da xarajat moddasi) do'kon egasi
konsolsiz, panelning o'zidan navbatga qaytara olishi kerak.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from catalog.models import RetailStore
from sales.models import Register, Sale, Shift


class RetryStuckTest(TestCase):
    def setUp(self):
        User.objects.create_superuser("t_admin", "t@a.uz", "pw12345")
        self.client.login(username="t_admin", password="pw12345")
        store = RetailStore.objects.create(ms_id="00000000-0000-0000-0000-0000000000de", name="N")
        reg = Register.objects.create(code="k1", name="Kassa-1", store=store)
        self.shift = Shift.objects.create(register=reg, opened_at=timezone.now(), number=1)

    def _sale(self, n, status):
        return Sale.objects.create(
            shift=self.shift, number=n, created_at=timezone.now(),
            sync_status=status, sync_attempts=5, sync_error="MoySklad 412: expenseItem",
        )

    def test_tugma_faqat_tiqilgan_bolsa_korinadi(self):
        html = self.client.get("/").content.decode()
        self.assertNotIn('value="retry_stuck"', html)
        self._sale(1, Sale.STUCK)
        html = self.client.get("/").content.decode()
        self.assertIn('value="retry_stuck"', html)
        self.assertIn("Qayta yuborish", html)

    def test_qayta_yuborish_navbatga_qaytaradi(self):
        s1 = self._sale(1, Sale.STUCK)
        s2 = self._sale(2, Sale.STUCK)
        sent = self._sale(3, Sale.SENT)

        r = self.client.post("/", {"action": "retry_stuck"})
        self.assertEqual(r.status_code, 302)

        for s in (s1, s2):
            s.refresh_from_db()
            self.assertEqual(s.sync_status, Sale.NEW)
            self.assertEqual(s.sync_attempts, 0)
            self.assertIsNone(s.next_attempt_at)
        sent.refresh_from_db()
        self.assertEqual(sent.sync_status, Sale.SENT, "yuborilganlarga tegilmaydi")

        html = self.client.get("/").content.decode()
        self.assertIn("2 ta chek navbatga qaytarildi", html)

    def test_login_talab_qiladi(self):
        self.client.logout()
        self._sale(1, Sale.STUCK)
        r = self.client.post("/", {"action": "retry_stuck"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/kirish/", r["Location"])
        self.assertEqual(Sale.objects.get(number=1).sync_status, Sale.STUCK)
