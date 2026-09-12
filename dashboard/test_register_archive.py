"""Kassani «o'chirish» — arxivlash (savdo tarixi yo'qolmasligi kerak).

Kassaga smenalar, cheklar va Z-hisobotlar bog'langan. Ularni yo'qotib
bo'lmaydi, shuning uchun smenasi bor kassa BUTUNLAY o'chirilmaydi —
arxivlanadi: ro'yxatdan yo'qoladi va kira olmaydi, lekin bazada qoladi.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from catalog.models import RetailStore
from sales.models import Register, Shift


class RegisterArchiveTest(TestCase):
    def setUp(self):
        User.objects.create_superuser("t_admin", "t@a.uz", "pw12345")
        self.client.login(username="t_admin", password="pw12345")
        self.store = RetailStore.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000f1", name="Nuqta", active=True,
        )

    def _register(self, code="k1", login="kassa-1"):
        reg = Register(code=code, name="Kassa 1", store=self.store, login=login)
        reg.set_password("1234")
        reg.save()
        return reg

    def _delete(self, reg):
        return self.client.post("/kassalar/", {"action": "delete", "id": reg.pk})

    # ---- smenasi BOR kassa: arxivlanadi, o'chmaydi -------------------

    def test_smenasi_bor_kassa_arxivlanadi_tarix_saqlanadi(self):
        reg = self._register()
        Shift.objects.create(register=reg, opened_at=timezone.now(), number=1)

        self._delete(reg)

        reg.refresh_from_db()
        self.assertTrue(reg.archived, "kassa arxivlanishi kerak")
        self.assertFalse(reg.active, "arxivlangan kassa kira olmasligi kerak")
        # Eng muhimi: kassa ham, smenasi ham bazada turibdi
        self.assertTrue(Register.objects.filter(pk=reg.pk).exists())
        self.assertEqual(Shift.objects.filter(register=reg).count(), 1)

    def test_arxivlangan_kassa_royxatda_kormaydi(self):
        reg = self._register()
        Shift.objects.create(register=reg, opened_at=timezone.now(), number=1)
        self._delete(reg)

        html = self.client.get("/kassalar/").content.decode()
        self.assertNotIn("kassa-1", html, "arxivlangan kassa ro'yxatda ko'rinmasligi kerak")

    def test_arxiv_royxati_sorov_bilan_korinadi(self):
        reg = self._register()
        Shift.objects.create(register=reg, opened_at=timezone.now(), number=1)
        self._delete(reg)

        html = self.client.get("/kassalar/?arxiv=1").content.decode()
        self.assertIn("kassa-1", html)
        self.assertIn("Arxivdan qaytarish", html)

    def test_arxivdan_qaytariladi(self):
        reg = self._register()
        Shift.objects.create(register=reg, opened_at=timezone.now(), number=1)
        self._delete(reg)

        self.client.post("/kassalar/", {"action": "restore", "id": reg.pk})

        reg.refresh_from_db()
        self.assertFalse(reg.archived)
        html = self.client.get("/kassalar/").content.decode()
        self.assertIn("kassa-1", html)

    # ---- smenasi YO'Q kassa: butunlay o'chadi ------------------------

    def test_smenasiz_kassa_butunlay_ochadi(self):
        reg = self._register()
        self._delete(reg)
        self.assertFalse(Register.objects.filter(pk=reg.pk).exists())

    # ---- xavfsizlik: arxivlangan kassa API'ga kira olmaydi -----------

    def test_arxivlangan_kassa_login_qila_olmaydi(self):
        reg = self._register()
        Shift.objects.create(register=reg, opened_at=timezone.now(), number=1)
        self._delete(reg)

        r = self.client.post(
            "/api/v1/connect",
            {"login": "kassa-1", "password": "1234"},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 401)

    def test_arxivlangan_kassa_tokeni_ishlamaydi(self):
        reg = self._register()
        token = reg.api_token
        self.assertTrue(token)
        Shift.objects.create(register=reg, opened_at=timezone.now(), number=1)
        self._delete(reg)

        r = self.client.get("/api/v1/hello", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(r.status_code, 401)
