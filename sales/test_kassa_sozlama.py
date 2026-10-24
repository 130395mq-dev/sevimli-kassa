"""Kassa sozlamalari (egasi, 2026-10-24): «Shtrix-kodli tovarni faqat
skaner bilan qo'shish», «Kassada qidiruv faqat tovar kodi bo'yicha» va
«Chekni keyinga qoldirishga ruxsat» — panelda
yoqib-o'chiriladi, kassaga `hello` orqali boradi."""

from django.contrib.auth.models import User
from django.test import Client

from api.tests import ApiTestCase
from sales.models import RegisterSettings


class KassaSozlamaTest(ApiTestCase):
    def hello_settings(self):
        r = self.client.get("/api/v1/hello", **self.auth())
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["settings"]

    def test_standart_qiymatlar(self):
        s = self.hello_settings()
        self.assertIs(s["scan_only_barcoded"], True)    # egasining qarori — yoqiq
        self.assertIs(s["allow_park"], False)           # 2026-10-15 dan beri o'chiq
        self.assertIs(s["search_code_only"], True)      # «faqat kod bilan»

    def test_paneldan_saqlanadi_va_kassaga_boradi(self):
        owner = User.objects.create_user("egasi", password="egasi-parol-1")
        panel = Client()
        panel.force_login(owner)
        url = f"/kassa/{self.register.pk}/sozlash/"

        page = panel.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'name="scan_only_barcoded"')
        self.assertContains(page, 'name="allow_park"')
        self.assertContains(page, 'name="search_code_only"')

        # Qoldirish yoqiladi, «faqat skaner» o'chiriladi (checkbox kelmaydi)
        r = panel.post(url, {"enabled": "on", "allow_park": "on", "max_discount": "1"})
        self.assertEqual(r.status_code, 302)
        st = RegisterSettings.objects.get(register=self.register)
        self.assertTrue(st.allow_park)
        self.assertFalse(st.scan_only_barcoded)
        s = self.hello_settings()
        self.assertIs(s["allow_park"], True)
        self.assertIs(s["scan_only_barcoded"], False)
        self.assertIs(s["search_code_only"], False)

        # Qaytadan: faqat skaner yoqiq, qoldirish o'chiq
        panel.post(url, {"enabled": "on", "scan_only_barcoded": "on",
                         "search_code_only": "on", "max_discount": "1"})
        s = self.hello_settings()
        self.assertIs(s["search_code_only"], True)
        self.assertIs(s["allow_park"], False)
        self.assertIs(s["scan_only_barcoded"], True)
