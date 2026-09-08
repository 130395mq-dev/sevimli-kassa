"""Panel testlari — sahifalash (pagination) va asosiy sahifalar ochilishi."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from catalog.models import RetailStore
from sales.models import Register, Shift


class ShiftsPaginationTest(TestCase):
    def setUp(self):
        self.store = RetailStore.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000de",
            name="Chilonzor",
            organization_ms_id="00000000-0000-0000-0000-0000000000a1",
            store_ms_id="00000000-0000-0000-0000-0000000000b2",
        )
        self.register = Register.objects.create(
            code="kassa-1", name="Kassa-1", store=self.store
        )
        # 60 ta smena — bu 50 tadan ko'p, demak 2 sahifa bo'lishi kerak.
        now = timezone.now()
        for i in range(60):
            Shift.objects.create(
                register=self.register, number=i + 1, cashier="Nilufar",
                opened_at=now - timezone.timedelta(hours=i),
            )
        User.objects.create_superuser("admin", "a@a.uz", "parol123")
        self.client = Client()
        self.client.login(username="admin", password="parol123")

    def test_birinchi_sahifada_50_ta(self):
        r = self.client.get("/smenalar/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["page"].object_list), 50)
        self.assertEqual(r.context["page"].paginator.count, 60)
        self.assertEqual(r.context["page"].paginator.num_pages, 2)
        # Pager ko'rinadi
        self.assertContains(r, 'class="pager"')
        self.assertContains(r, "Keyingi")

    def test_ikkinchi_sahifada_qolgan_10_ta(self):
        r = self.client.get("/smenalar/?page=2")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["page"].object_list), 10)
        self.assertFalse(r.context["page"].has_next())

    def test_notogri_sahifa_oxirgisiga_tushadi(self):
        # get_page noto'g'ri raqamda ham xato bermaydi (oxirgi sahifa).
        r = self.client.get("/smenalar/?page=999")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["page"].number, 2)
