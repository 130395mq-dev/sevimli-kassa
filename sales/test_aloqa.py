"""Aloqa chiroqlari: server ↔ MoySklad va kassa ↔ server holati.

Yashil / sariq / qizil qanday aniqlanishi, `hello` javobida borligi va
panelning tepasida hamda /aloqa.json da chiqishi tekshiriladi.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from catalog.models import RetailStore, SyncState
from sales import aloqa
from sales.models import PaymentMethod, Register, Sale, Shift


def _ago(**kw):
    return timezone.now() - timedelta(**kw)


class MoySkladHealthTest(TestCase):
    def setUp(self):
        cache.clear()

    def health(self):
        with self.settings(MOYSKLAD_TOKEN="x"):
            return aloqa.moysklad_health(use_cache=False)

    def test_tokensiz_qizil(self):
        with self.settings(MOYSKLAD_TOKEN=""):
            h = aloqa.moysklad_health(use_cache=False)
        self.assertEqual(h["state"], "bad")

    def test_hali_sinxron_bolmagan_qizil(self):
        self.assertEqual(self.health()["state"], "bad")

    def test_yaqinda_sinxron_yashil(self):
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=3))
        h = self.health()
        self.assertEqual(h["state"], "ok")
        self.assertTrue(h["last_ok"])

    def test_15_daqiqa_kechiksa_sariq(self):
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=20))
        self.assertEqual(self.health()["state"], "warn")

    def test_30_daqiqa_javob_bolmasa_qizil(self):
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=45))
        h = self.health()
        self.assertEqual(h["state"], "bad")
        self.assertIn("45 daqiqa", h["text"])

    def test_oxirgi_urinish_xato_bolsa_sariq(self):
        SyncState.objects.create(
            entity="assortment", last_success_at=_ago(minutes=4),
            last_run_at=_ago(minutes=1), last_error="MoySklad 500",
        )
        h = self.health()
        self.assertEqual(h["state"], "warn")
        self.assertIn("MoySklad 500", h["text"])

    def _sale(self, status, minutes=0):
        store = RetailStore.objects.create(ms_id="00000000-0000-0000-0000-0000000000de", name="N")
        reg = Register.objects.create(code="k", name="K", store=store)
        shift = Shift.objects.create(register=reg, opened_at=timezone.now(), number=1)
        return Sale.objects.create(
            shift=shift, number=1, created_at=_ago(minutes=minutes), sync_status=status,
        )

    def test_tiqilib_qolgan_chek_qizil(self):
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=1))
        self._sale(Sale.STUCK)
        h = self.health()
        self.assertEqual(h["state"], "bad")
        self.assertIn("tiqilib", h["text"])

    def test_xato_bilan_qayta_urinilayotgan_chek_sariq(self):
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=1))
        self._sale(Sale.FAILED)
        self.assertEqual(self.health()["state"], "warn")

    def test_5_daqiqadan_beri_navbatdagi_chek_sariq(self):
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=1))
        self._sale(Sale.NEW, minutes=9)
        self.assertEqual(self.health()["state"], "warn")

    def test_hozirgina_yaratilgan_chek_hali_yashil(self):
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=1))
        self._sale(Sale.NEW, minutes=1)
        self.assertEqual(self.health()["state"], "ok")

    def test_kesh_10_soniya(self):
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=1))
        with self.settings(MOYSKLAD_TOKEN="x"):
            first = aloqa.moysklad_health()
            SyncState.objects.update(last_success_at=_ago(hours=2))
            second = aloqa.moysklad_health()
        self.assertEqual(first["state"], "ok")
        self.assertEqual(second["state"], "ok", "10 soniya ichida kesh javob beradi")


class RegisterStateTest(TestCase):
    def setUp(self):
        self.store = RetailStore.objects.create(ms_id="00000000-0000-0000-0000-0000000000de", name="N")

    def reg(self, seen):
        return Register.objects.create(code="k", name="Kassa-1", store=self.store, last_seen_at=seen)

    def test_hali_ulanmagan_qizil(self):
        self.assertEqual(aloqa.register_state(self.reg(None))["state"], "bad")

    def test_1_daqiqa_oldin_korinsa_yashil(self):
        self.assertEqual(aloqa.register_state(self.reg(_ago(seconds=70)))["state"], "ok")

    def test_3_daqiqa_jim_bolsa_sariq(self):
        self.assertEqual(aloqa.register_state(self.reg(_ago(minutes=3)))["state"], "warn")

    def test_6_daqiqa_jim_bolsa_qizil(self):
        s = aloqa.register_state(self.reg(_ago(minutes=6)))
        self.assertEqual(s["state"], "bad")
        self.assertIn("6 daqiqa", s["text"])

    def test_arxivlangan_kassa_royxatga_kirmaydi(self):
        self.reg(_ago(seconds=10))
        Register.objects.create(code="old", name="Eski", store=self.store, archived=True, active=False)
        names = [r["name"] for r in aloqa.registers_health()]
        self.assertEqual(names, ["Kassa-1"])

    def test_umumiy_holat_eng_yomonini_oladi(self):
        cache.clear()
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=1))
        self.reg(_ago(minutes=10))  # qizil
        with self.settings(MOYSKLAD_TOKEN="x"):
            snap = aloqa.snapshot()
        self.assertEqual(snap["moysklad"]["state"], "ok")
        self.assertEqual(snap["overall"], "bad")


class HelloLinksTest(TestCase):
    """Kassa `hello` javobida MoySklad chirog'ini oladi."""

    def setUp(self):
        cache.clear()
        store = RetailStore.objects.create(ms_id="00000000-0000-0000-0000-0000000000de", name="N")
        self.register = Register.objects.create(code="k", name="Kassa-1", store=store)
        PaymentMethod.objects.create(code="naqd", name="Naqd", is_cash=True)

    def test_hello_links_moysklad(self):
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=2))
        with self.settings(MOYSKLAD_TOKEN="x"):
            r = Client().get(
                "/api/v1/hello", HTTP_AUTHORIZATION=f"Bearer {self.register.api_token}",
            )
        self.assertEqual(r.status_code, 200)
        ms = r.json()["links"]["moysklad"]
        self.assertEqual(ms["state"], "ok")
        self.assertIn("text", ms)


class PanelAloqaTest(TestCase):
    """Panel: tepadagi chiroqlar qatori va 15 soniyalik JSON."""

    def setUp(self):
        cache.clear()
        User.objects.create_superuser("t_admin", "t@a.uz", "pw12345")
        self.client.login(username="t_admin", password="pw12345")
        store = RetailStore.objects.create(ms_id="00000000-0000-0000-0000-0000000000de", name="N")
        self.reg = Register.objects.create(
            code="k1", name="Kassa-1", store=store, last_seen_at=_ago(seconds=20),
        )

    def test_json_login_talab_qiladi(self):
        self.client.logout()
        r = self.client.get("/aloqa.json")
        self.assertEqual(r.status_code, 302)

    def test_json_holatni_beradi(self):
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=50))
        with self.settings(MOYSKLAD_TOKEN="x"):
            data = self.client.get("/aloqa.json").json()
        self.assertEqual(data["moysklad"]["state"], "bad")
        self.assertEqual(data["registers"][0]["id"], self.reg.pk)
        self.assertEqual(data["registers"][0]["state"], "ok")
        self.assertEqual(data["overall"], "bad")

    def test_har_sahifada_chiroqlar_qatori_bor(self):
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=1))
        with self.settings(MOYSKLAD_TOKEN="x"):
            html = self.client.get("/kassalar/").content.decode()
        self.assertIn('id="aloqa"', html)
        self.assertIn('data-aloqa="moysklad"', html)
        self.assertIn(f'data-aloqa="reg-{self.reg.pk}"', html)
        self.assertIn("/aloqa.json", html, "15 soniyalik yangilash skripti")
        # Kassa qatorida ham nuqta bor (tepadagi + jadvaldagi = 2)
        self.assertEqual(html.count(f'data-aloqa="reg-{self.reg.pk}"'), 2)

    def test_asosiy_sahifada_kassa_nuqtasi(self):
        with self.settings(MOYSKLAD_TOKEN="x"):
            html = self.client.get("/").content.decode()
        self.assertIn(f'data-aloqa="reg-{self.reg.pk}"', html)

    def test_kirish_sahifasida_chiroq_yoq(self):
        self.client.logout()
        html = self.client.get("/kirish/").content.decode()
        self.assertNotIn('id="aloqa"', html)
