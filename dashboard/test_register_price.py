"""Yangi kassa ochilganda sotuv narxi tanlanadi (egasining so'rovi, 2026-09-29).

Har kassa — o'z narxida. Paneldan qaysi kassaga qaysi narx belgilansa,
u shunda sotadi. Yangi kassa formasida narx tanlash qo'shildi; bo'sh
qoldirilsa avvalgidek savdo nuqtasiniki (odatda chakana). Boshqa
kassalarning narxiga tegilmaydi.
"""

from django.contrib.auth.models import User
from django.test import TestCase

from api import pricing
from catalog.models import PriceType, RetailStore, Warehouse
from sales.models import Register, RegisterPricePolicy


class NewRegisterPriceTypeTest(TestCase):
    def setUp(self):
        User.objects.create_superuser("t_admin", "t@a.uz", "pw12345")
        self.client.login(username="t_admin", password="pw12345")
        self.store = RetailStore.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000f1", name="Nuqta", active=True,
        )
        self.warehouse = Warehouse.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000e1", name="Chilonzor",
        )
        self.ulgurji = PriceType.objects.create(
            ms_id="00000000-0000-0000-0000-00000000aaaa", name="Улугржи нархи", sort=0)
        self.chakana = PriceType.objects.create(
            ms_id="00000000-0000-0000-0000-00000000bbbb", name="Чакана нарх", sort=1)

    def create(self, **extra):
        data = {"action": "create", "warehouse": str(self.warehouse.ms_id), "password": "1234"}
        data.update(extra)
        response = self.client.post("/kassalar/", data)
        self.assertEqual(response.status_code, 302, response.content)
        return Register.objects.latest("pk")

    def target_of(self, register):
        _, target, _, accepted = pricing.policy_for(register)
        return target, accepted

    def test_forma_narx_tanlovini_korsatadi(self):
        html = self.client.get("/kassalar/").content.decode()
        self.assertIn('name="price_type"', html)
        self.assertIn("Улугржи нархи", html)
        self.assertIn("Чакана нарх", html)

    def test_ulgurji_tanlansa_yangi_kassa_ulgurjida_sotadi(self):
        reg = self.create(name="Ulgurji kassa", price_type="Улугржи нархи")
        self.assertEqual(reg.settings.price_type, "Улугржи нархи")
        self.assertFalse(reg.settings.allow_price_type_switch)
        target, accepted = self.target_of(reg)
        self.assertEqual(target, str(self.ulgurji.ms_id))
        self.assertEqual(accepted, {str(self.ulgurji.ms_id)})

    def test_bosh_qoldirilsa_avvalgidek_chakana(self):
        reg = self.create(name="Oddiy kassa")
        self.assertEqual(reg.settings.price_type, "")
        target, _ = self.target_of(reg)
        self.assertEqual(target, str(self.chakana.ms_id))

    def test_notogri_nom_eterib_yuboriladi(self):
        reg = self.create(name="Kassa X", price_type="Yo'q narx")
        self.assertEqual(reg.settings.price_type, "")
        target, _ = self.target_of(reg)
        self.assertEqual(target, str(self.chakana.ms_id))

    def test_har_kassa_oz_narxida_boshqasiga_tegilmaydi(self):
        chakana = self.create(name="Chakana kassa", login="chakana-1")
        ulgurji = self.create(name="Ulgurji kassa", login="ulgurji-1", price_type="Улугржи нархи")
        self.assertEqual(self.target_of(chakana)[0], str(self.chakana.ms_id))
        self.assertEqual(self.target_of(ulgurji)[0], str(self.ulgurji.ms_id))
        # Ikkinchi kassaga ulgurji berilgani birinchisini o'zgartirmaydi
        self.assertEqual(self.target_of(chakana)[0], str(self.chakana.ms_id))
        self.assertEqual(RegisterPricePolicy.objects.get(register=chakana).accepted_types,
                         [str(self.chakana.ms_id)])
