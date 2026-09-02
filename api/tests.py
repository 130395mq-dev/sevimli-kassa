"""
Kassa API testlari.

Eng muhim test — `test_takroriy_chek_ikki_marta_yozilmaydi`. Kassa javobni
olmasdan uzilib qolsa, chekni yana yuboradi. Shunda ikkinchi hujjat
yaratilmasligi kerak, aks holda kunlik savdo ikki barobar chiqib ketadi.

    python manage.py test api
"""

import json
import uuid

from django.test import Client, TestCase
from django.utils import timezone

from catalog.models import Customer, Product, RetailStore
from sales.models import PaymentMethod, Register, Sale, Shift


class ApiTestCase(TestCase):
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
        self.cash = PaymentMethod.objects.create(
            code="naqd", name="Naqd", is_cash=True, sort=1
        )
        self.card = PaymentMethod.objects.create(
            code="terminal-1", name="Terminal-1", sort=2
        )
        self.product = Product.objects.create(
            ms_id="00000000-0000-0000-0000-000000000101",
            name="Buhanka S", code="0001", sale_price=3_000_00,
        )
        self.client = Client()

    def auth(self, token=None):
        return {"HTTP_AUTHORIZATION": f"Bearer {token or self.register.api_token}"}

    def post(self, url, payload, **kw):
        return self.client.post(
            url, data=json.dumps(payload), content_type="application/json",
            **{**self.auth(), **kw},
        )

    def open_shift(self):
        return self.post("/api/v1/shift/open",
                         {"cashier": "Nilufar", "opening_cash": 300_000_00})

    def sale_payload(self, **over):
        data = {
            "local_uuid": str(uuid.uuid4()),
            "created_at": timezone.now().isoformat(),
            "gross_total": 3_000_00,
            "items": [{
                "product_id": self.product.pk,
                "ms_product_id": str(self.product.ms_id),
                "name": "Buhanka S",
                "quantity": "1.000",
                "price": 3_000_00,
                "total": 3_000_00,
            }],
            "payments": [{"method": "naqd", "amount": 3_000_00,
                          "tendered": 5_000_00, "change": 2_000_00}],
        }
        data.update(over)
        return data


class AuthTest(ApiTestCase):
    def test_tokensiz_kirish_yopiq(self):
        self.assertEqual(self.client.get("/api/v1/hello").status_code, 401)

    def test_notogri_token(self):
        r = self.client.get("/api/v1/hello", **self.auth("yolgon-token"))
        self.assertEqual(r.status_code, 401)

    def test_ochirilgan_kassa_kira_olmaydi(self):
        self.register.active = False
        self.register.save()
        r = self.client.get("/api/v1/hello", **self.auth())
        self.assertEqual(r.status_code, 401)

    def test_har_kassaning_oz_tokeni(self):
        other = Register.objects.create(
            code="kassa-2", name="Kassa-2", store=self.store
        )
        self.assertNotEqual(other.api_token, self.register.api_token)


class HelloTest(ApiTestCase):
    def test_smena_yoq(self):
        r = self.client.get("/api/v1/hello", **self.auth())
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["register"]["name"], "Kassa-1")
        self.assertEqual(data["point"], "Chilonzor")
        self.assertIsNone(data["shift"])
        self.assertEqual(len(data["payment_methods"]), 2)

    def test_smena_ochiq(self):
        self.open_shift()
        data = self.client.get("/api/v1/hello", **self.auth()).json()
        self.assertEqual(data["shift"]["cashier"], "Nilufar")
        self.assertEqual(data["shift"]["next_receipt_number"], 1)


class ShiftTest(ApiTestCase):
    def test_smena_ochiladi(self):
        r = self.open_shift()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["shift"]["number"], 1)

    def test_kassirsiz_ochilsa_kassa_nomi_yoziladi(self):
        """Kassirlar ro'yxati yo'q — smenani kassaning o'zi ochadi."""
        r = self.post("/api/v1/shift/open", {"cashier": "  "})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["shift"]["cashier"], self.register.name)

    def test_ikkita_ochiq_smena_bolmaydi(self):
        self.open_shift()
        self.assertEqual(self.open_shift().status_code, 409)

    def test_raqam_osib_boradi(self):
        self.open_shift()
        self.post("/api/v1/shift/close", {"counted_cash": 300_000_00})
        r = self.open_shift()
        self.assertEqual(r.json()["shift"]["number"], 2)

    def test_yopishda_chek_matni_qaytadi(self):
        self.open_shift()
        self.post("/api/v1/sales", self.sale_payload())
        r = self.post("/api/v1/shift/close", {"counted_cash": 303_000_00})

        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("SMENA YOPILDI", data["receipt_text"])
        self.assertIn("BUGUNGI SAVDO", data["receipt_text"])
        self.assertEqual(data["net_total"], 3_000_00)
        self.assertEqual(data["cash_total"], 3_000_00)
        self.assertEqual(data["expected_cash"], 303_000_00)
        self.assertEqual(data["cash_diff"], 0)
        self.assertEqual(data["pending"], 1)

    def test_smenasiz_yopilmaydi(self):
        self.assertEqual(
            self.post("/api/v1/shift/close", {}).status_code, 409
        )


class SaleTest(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.open_shift()

    def test_chek_yoziladi(self):
        r = self.post("/api/v1/sales", self.sale_payload())
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["number"], 1)
        self.assertFalse(r.json()["duplicate"])

        sale = Sale.objects.get()
        self.assertEqual(sale.net_total, 3_000_00)
        self.assertEqual(sale.items.count(), 1)
        self.assertEqual(sale.payments.count(), 1)
        self.assertEqual(sale.payments.first().change, 2_000_00)
        self.assertEqual(sale.sync_status, Sale.NEW)

    def test_takroriy_chek_ikki_marta_yozilmaydi(self):
        """Kassa javobni olmay qolsa, o'sha chekni yana yuboradi."""
        payload = self.sale_payload()

        first = self.post("/api/v1/sales", payload)
        second = self.post("/api/v1/sales", payload)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(first.json()["number"], second.json()["number"])
        self.assertEqual(Sale.objects.count(), 1)

    def test_tolov_mos_kelmasa_qabul_qilinmaydi(self):
        payload = self.sale_payload()
        payload["payments"] = [{"method": "naqd", "amount": 2_000_00}]

        r = self.post("/api/v1/sales", payload)
        self.assertEqual(r.status_code, 400)
        self.assertIn("teng emas", r.json()["error"])
        self.assertEqual(Sale.objects.count(), 0)

    def test_aralash_tolov(self):
        payload = self.sale_payload()
        payload["payments"] = [
            {"method": "naqd", "amount": 1_000_00},
            {"method": "terminal-1", "amount": 2_000_00},
        ]
        r = self.post("/api/v1/sales", payload)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Sale.objects.get().payments.count(), 2)

    def test_ball_summani_kamaytiradi(self):
        payload = self.sale_payload()
        payload["points_spent"] = 500  # 500 so'm
        payload["payments"] = [{"method": "naqd", "amount": 2_500_00}]

        r = self.post("/api/v1/sales", payload)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Sale.objects.get().net_total, 2_500_00)

    def test_notanish_tolov_turi(self):
        payload = self.sale_payload()
        payload["payments"] = [{"method": "bitcoin", "amount": 3_000_00}]
        self.assertEqual(self.post("/api/v1/sales", payload).status_code, 400)

    def test_bosh_chek(self):
        payload = self.sale_payload()
        payload["items"] = []
        self.assertEqual(self.post("/api/v1/sales", payload).status_code, 400)

    def test_manfiy_miqdor(self):
        payload = self.sale_payload()
        payload["items"][0]["quantity"] = "-1.000"
        self.assertEqual(self.post("/api/v1/sales", payload).status_code, 400)
        self.assertEqual(Sale.objects.count(), 0)

    def test_smenasiz_chek_yozilmaydi(self):
        self.post("/api/v1/shift/close", {})
        r = self.post("/api/v1/sales", self.sale_payload())
        self.assertEqual(r.status_code, 409)


class CatalogTest(ApiTestCase):
    def test_tovarlar_qaytadi(self):
        r = self.client.get("/api/v1/catalog", **self.auth())
        self.assertEqual(r.status_code, 200)
        products = r.json()["products"]
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["name"], "Buhanka S")
        self.assertEqual(products[0]["price"], 3_000_00)

    def test_arxivdagilar_kelmaydi(self):
        self.product.archived = True
        self.product.save()
        r = self.client.get("/api/v1/catalog", **self.auth())
        self.assertEqual(r.json()["products"], [])

    def test_deltada_arxivlangan_tovar_archived_bayrogi_bilan_keladi(self):
        """Buxgalter o'chirgan tovar kassaga «o'chir» signali bilan yetadi."""
        from datetime import timedelta

        since = (timezone.now() - timedelta(minutes=1)).isoformat()
        self.product.archived = True
        self.product.save()
        r = self.client.get("/api/v1/catalog", {"since": since}, **self.auth())
        products = r.json()["products"]
        self.assertEqual(len(products), 1)
        self.assertTrue(products[0]["archived"])

    def test_refresh_tokensiz_xatosiz_qaytadi(self):
        with self.settings(MOYSKLAD_TOKEN=""):
            r = self.post("/api/v1/catalog/refresh", {})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ran"])

    def test_refresh_hozirgina_bolgan_bolsa_cooldown(self):
        from catalog.models import SyncState

        SyncState.objects.create(entity="assortment", last_success_at=timezone.now())
        with self.settings(MOYSKLAD_TOKEN="x"):
            r = self.post("/api/v1/catalog/refresh", {})
        self.assertEqual(r.json(), {"ran": False, "reason": "cooldown"})


class ReconcileTest(TestCase):
    """MoySklad'dan butunlay o'chirilgan tovar lokalda arxivlanadi."""

    def _sync(self, live_ids):
        from unittest.mock import MagicMock

        from catalog.sync import CatalogSync

        client = MagicMock()
        client.iter_list.return_value = iter([{"id": i} for i in live_ids])
        return CatalogSync(client)

    def test_yoq_tovar_arxivlanadi(self):
        a = Product.objects.create(ms_id="00000000-0000-0000-0000-0000000000a1", name="A")
        b = Product.objects.create(ms_id="00000000-0000-0000-0000-0000000000b1", name="B")
        old_synced = b.synced_at
        count = self._sync([str(a.ms_id)]).reconcile_deleted()
        self.assertEqual(count, 1)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertFalse(a.archived)
        self.assertTrue(b.archived)
        # synced_at yangilangan — kassa delta'si buni ko'radi
        self.assertGreater(b.synced_at, old_synced)

    def test_moysklad_bosh_qaytarsa_hech_narsa_ochirilmaydi(self):
        Product.objects.create(ms_id="00000000-0000-0000-0000-0000000000a1", name="A")
        Product.objects.create(ms_id="00000000-0000-0000-0000-0000000000b1", name="B")
        count = self._sync([]).reconcile_deleted()
        self.assertEqual(count, 0)
        self.assertEqual(Product.objects.filter(archived=True).count(), 0)


class CustomerTest(ApiTestCase):
    def test_qisqa_sorov_rad_etiladi(self):
        r = self.client.get("/api/v1/customers?q=ab", **self.auth())
        self.assertEqual(r.status_code, 400)

    def test_telefon_boyicha_topiladi(self):
        Customer.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000ca",
            name="Aliyev Sardor", phone="998901234567", bonus_points=1240,
        )
        r = self.client.get("/api/v1/customers?q=901234", **self.auth())
        rows = r.json()["customers"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bonus_points"], 1240)


class CashTest(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.open_shift()

    def test_kirim_yoziladi(self):
        r = self.post("/api/v1/cash",
                      {"kind": "in", "amount": 200_000_00, "comment": "Razmen"})
        self.assertEqual(r.status_code, 201)

    def test_manfiy_summa_rad_etiladi(self):
        r = self.post("/api/v1/cash", {"kind": "in", "amount": -100})
        self.assertEqual(r.status_code, 400)

    def test_notogri_tur(self):
        r = self.post("/api/v1/cash", {"kind": "hadya", "amount": 100})
        self.assertEqual(r.status_code, 400)

    def test_chiqim_kassa_pulini_kamaytiradi(self):
        self.post("/api/v1/sales", self.sale_payload())
        self.post("/api/v1/cash", {"kind": "out", "amount": 100_000_00})

        data = self.post("/api/v1/shift/close", {}).json()
        # 300 000 + 3 000 - 100 000
        self.assertEqual(data["expected_cash"], 203_000_00)


class ReturnFlowTest(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.open_shift()
        # Asl savdo
        self.post("/api/v1/sales", self.sale_payload())
        self.origin = Sale.objects.get(kind=Sale.SALE)

    def test_returnable_savdolarni_beradi(self):
        r = self.client.get("/api/v1/sales/returnable", **self.auth())
        self.assertEqual(r.status_code, 200)
        sales = r.json()["sales"]
        self.assertEqual(len(sales), 1)
        self.assertEqual(sales[0]["number"], self.origin.number)
        self.assertEqual(len(sales[0]["items"]), 1)
        self.assertEqual(sales[0]["items"][0]["returned_qty"], 0)

    def test_qaytarish_yoziladi_va_asl_chekka_boglanadi(self):
        import uuid
        payload = {
            "local_uuid": str(uuid.uuid4()), "kind": "return",
            "origin_id": self.origin.pk,
            "gross_total": 3_000_00, "net_total": 3_000_00,
            "items": [{
                "product_id": self.product.pk,
                "ms_product_id": str(self.product.ms_id),
                "name": "Buhanka S", "quantity": "1.000",
                "price": 3_000_00, "total": 3_000_00,
            }],
            "payments": [{"method": "naqd", "amount": 3_000_00}],
        }
        r = self.post("/api/v1/sales", payload)
        self.assertEqual(r.status_code, 201)

        ret = Sale.objects.get(kind=Sale.RETURN)
        self.assertEqual(ret.origin_id, self.origin.pk)
        self.assertEqual(ret.net_total, 3_000_00)

    def test_qaytargandan_keyin_returned_qty_osadi(self):
        import uuid
        self.post("/api/v1/sales", {
            "local_uuid": str(uuid.uuid4()), "kind": "return",
            "origin_id": self.origin.pk,
            "gross_total": 3_000_00, "net_total": 3_000_00,
            "items": [{"product_id": self.product.pk,
                       "ms_product_id": str(self.product.ms_id),
                       "name": "Buhanka S", "quantity": "1.000",
                       "price": 3_000_00, "total": 3_000_00}],
            "payments": [{"method": "naqd", "amount": 3_000_00}],
        })
        sales = self.client.get("/api/v1/sales/returnable", **self.auth()).json()["sales"]
        self.assertEqual(sales[0]["items"][0]["returned_qty"], 1.0)


class UpdateTest(ApiTestCase):
    """Kassa ilovasining o'zini yangilashi."""

    def _release(self, version, mandatory=False, content=b"MZ-fake-exe"):
        import hashlib

        from django.core.files.base import ContentFile

        from sales.models import KassaRelease

        rel = KassaRelease(
            version=version, mandatory=mandatory, notes="Sinov",
            size=len(content), sha256=hashlib.sha256(content).hexdigest(),
        )
        rel.file.save(f"SevimliKassa-{version}.exe", ContentFile(content), save=True)
        return rel

    def test_versiya_yoq_bolsa_env_zaxira(self):
        with self.settings(APP_VERSION="1.0.0", APP_DOWNLOAD_URL=""):
            r = self.client.get("/api/v1/version", **self.auth())
        self.assertEqual(r.json()["version"], "1.0.0")
        self.assertFalse(r.json()["mandatory"])

    def test_eng_katta_raqamli_versiya_qaytadi(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d, self.settings(MEDIA_ROOT=d):
            self._release("1.9.0")
            self._release("1.10.0", mandatory=True)   # matn bo'yicha kichik, raqam bo'yicha katta
            r = self.client.get("/api/v1/version", **self.auth())
            data = r.json()
            self.assertEqual(data["version"], "1.10.0")
            self.assertTrue(data["mandatory"])
            self.assertIn("/api/v1/update/download?v=1.10.0", data["url"])
            self.assertEqual(data["size"], len(b"MZ-fake-exe"))

            # Yuklab olish — faqat token bilan
            dl = self.client.get("/api/v1/update/download?v=1.10.0", **self.auth())
            self.assertEqual(dl.status_code, 200)
            self.assertEqual(b"".join(dl.streaming_content), b"MZ-fake-exe")
            self.assertEqual(
                self.client.get("/api/v1/update/download?v=1.10.0").status_code, 401
            )
            self.assertEqual(
                self.client.get("/api/v1/update/download?v=9.9.9", **self.auth()).status_code,
                404,
            )

    def test_kassa_versiyasi_sarlavhadan_yoziladi(self):
        self.client.get("/api/v1/hello", HTTP_X_KASSA_VERSION="1.2.3", **self.auth())
        self.register.refresh_from_db()
        self.assertEqual(self.register.app_version, "1.2.3")

    def test_version_key(self):
        from sales.models import version_key

        self.assertLess(version_key("1.9.0"), version_key("1.10.0"))
        self.assertEqual(version_key("2"), (2, 0, 0))
        self.assertEqual(version_key("v1.2.3-beta"), (1, 2, 3))


class PriceTypeTest(ApiTestCase):
    """Narx turlari: hello ro'yxat beradi, katalog hamma narxni beradi."""

    def setUp(self):
        super().setUp()
        from catalog.models import PriceType

        self.ulgurji = PriceType.objects.create(
            ms_id="00000000-0000-0000-0000-00000000aaaa", name="Улугржи нархи", sort=0
        )
        self.chakana = PriceType.objects.create(
            ms_id="00000000-0000-0000-0000-00000000bbbb", name="Чакана нарх", sort=1
        )
        self.product.prices = {
            "00000000-0000-0000-0000-00000000aaaa": 5200000,
            "00000000-0000-0000-0000-00000000bbbb": 5500000,
        }
        self.product.save()

    def test_hello_narx_turlari_va_asosiysi(self):
        data = self.client.get("/api/v1/hello", **self.auth()).json()
        self.assertEqual([p["name"] for p in data["price_types"]],
                         ["Улугржи нархи", "Чакана нарх"])
        # Nuqtada tur yo'q, sozlamada yo'q → «чакана» so'zi bo'yicha
        self.assertEqual(data["default_price_type"], "00000000-0000-0000-0000-00000000bbbb")
        self.assertTrue(data["settings"]["allow_price_type_switch"])

    def test_nuqta_narx_turi_ustun(self):
        self.store.price_type_ms_id = "00000000-0000-0000-0000-00000000aaaa"
        self.store.save()
        data = self.client.get("/api/v1/hello", **self.auth()).json()
        self.assertEqual(data["default_price_type"], "00000000-0000-0000-0000-00000000aaaa")

    def test_sozlamadagi_nom_eng_ustun(self):
        self.store.price_type_ms_id = "00000000-0000-0000-0000-00000000aaaa"
        self.store.save()
        st = self.register.settings
        st.price_type = "Чакана нарх"
        st.save()
        data = self.client.get("/api/v1/hello", **self.auth()).json()
        self.assertEqual(data["default_price_type"], "00000000-0000-0000-0000-00000000bbbb")

    def test_katalogda_hamma_narx(self):
        r = self.client.get("/api/v1/catalog", **self.auth()).json()
        self.assertEqual(r["products"][0]["prices"]["00000000-0000-0000-0000-00000000aaaa"], 5200000)

    def test_chekda_narx_turi_saqlanadi(self):
        self.open_shift()
        self.post("/api/v1/sales", self.sale_payload(price_type="Улугржи нархи"))
        self.assertEqual(Sale.objects.get().price_type, "Улугржи нархи")


class CashierLoginTest(ApiTestCase):
    """Kassir login + parol bilan kiradi; ro'yxat ko'rsatilmaydi."""

    def setUp(self):
        super().setUp()
        from sales.models import Cashier

        self.nilufar = Cashier(name="Rahimova Nilufar", login="nilufar")
        self.nilufar.set_password("sevimli2026")
        self.nilufar.save()
        self.aziz = Cashier(name="Aziz Karimov", login="aziz")
        self.aziz.set_password("1234")
        self.aziz.save()

    def test_parol_bilan_kiradi(self):
        r = self.post("/api/v1/login", {"login": "nilufar", "password": "sevimli2026"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["cashier"]["name"], "Rahimova Nilufar")

    def test_eski_kassa_pin_maydoni_bilan_ham_kiradi(self):
        r = self.post("/api/v1/login", {"login": "aziz", "pin": "1234"})
        self.assertEqual(r.status_code, 200)

    def test_notogri_parol(self):
        r = self.post("/api/v1/login", {"login": "nilufar", "password": "boshqa"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("parol", r.json()["error"])

    def test_yoq_login_ham_bir_xil_xato(self):
        r = self.post("/api/v1/login", {"login": "yoq", "password": "x"})
        self.assertEqual(r.status_code, 401)

    def test_ochirilgan_kassir_kira_olmaydi(self):
        self.aziz.active = False
        self.aziz.save()
        self.assertEqual(
            self.post("/api/v1/login", {"login": "aziz", "password": "1234"}).status_code,
            401,
        )

    def test_kassa_ozining_login_paroli_bilan_kiradi(self):
        """Asosiy yo'l: kassirlar ro'yxati yo'q, kassaning o'z login-paroli."""
        self.register.login = "chilonzor-1"
        self.register.set_password("777888")
        self.register.save()

        ok = self.post(
            "/api/v1/login", {"login": "chilonzor-1", "password": "777888"}
        )
        self.assertEqual(ok.status_code, 200)
        who = ok.json()["cashier"]
        self.assertEqual(who["login"], "chilonzor-1")
        self.assertEqual(who["name"], self.register.name)
        self.assertTrue(who["is_manager"])

        no = self.post(
            "/api/v1/login", {"login": "chilonzor-1", "password": "000000"}
        )
        self.assertEqual(no.status_code, 401)


class WarehouseGuardTest(ApiTestCase):
    """Ombor tanlanmagan filialda smena ochilmaydi."""

    def test_omborsiz_smena_ochilmaydi(self):
        self.store.store_ms_id = None
        self.store.save()
        r = self.open_shift()
        self.assertEqual(r.status_code, 409)
        self.assertIn("ombor", r.json()["error"].lower())

    def test_ombor_bor_bolsa_ochiladi(self):
        self.assertEqual(self.open_shift().status_code, 201)

    def test_qoldiq_filial_ombori_boyicha(self):
        from catalog.models import Stock

        boshqa = "00000000-0000-0000-0000-0000000000cc"
        Stock.objects.create(product=self.product, store_ms_id=self.store.store_ms_id,
                             quantity=7)
        Stock.objects.create(product=self.product, store_ms_id=boshqa, quantity=99)

        r = self.client.get("/api/v1/catalog", **self.auth()).json()
        self.assertEqual(r["products"][0]["stock"], 7.0)

        # Ombor qo'lda boshqasiga o'zgartirilsa — qoldiq ham o'sha ombornikiga
        self.store.manual_warehouse_ms_id = boshqa
        self.store.save()
        r = self.client.get("/api/v1/catalog", **self.auth()).json()
        self.assertEqual(r["products"][0]["stock"], 99.0)


class RegisterWarehouseBindingTest(ApiTestCase):
    """Ombor kassa sozlamasida — MoySklad'dagidek («Tovarlar» bo'limi)."""

    def setUp(self):
        super().setUp()
        from catalog.models import Warehouse

        self.wh_shahar = Warehouse.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000b2", name="Sevimli Shaxar"
        )
        self.wh_bozor = Warehouse.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000f1", name="Sevimli Bozor"
        )

    def test_kassa_sozlamasi_savdo_nuqtasidan_ustun(self):
        st = self.register.settings
        st.warehouse_ms_id = self.wh_bozor.ms_id
        st.save()
        self.assertEqual(str(self.register.warehouse_ms_id), str(self.wh_bozor.ms_id))
        self.assertEqual(self.register.warehouse_name, "Sevimli Bozor")

    def test_tanlanmagan_bolsa_savdo_nuqtasiniki(self):
        self.assertEqual(str(self.register.warehouse_ms_id), str(self.wh_shahar.ms_id))

    def test_hello_ombor_nomini_beradi(self):
        st = self.register.settings
        st.warehouse_ms_id = self.wh_bozor.ms_id
        st.save()
        data = self.client.get("/api/v1/hello", **self.auth()).json()
        self.assertEqual(data["point"], "Sevimli Bozor")

    def test_yagona_tashkilot_avtomatik(self):
        from catalog.models import Organization

        self.store.organization_ms_id = None
        self.store.save()
        self.assertIsNone(self.register.organization_ms_id)

        org = Organization.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000e1", name="SEVIMLI SUPER MARKET"
        )
        self.assertEqual(str(self.register.organization_ms_id), str(org.ms_id))

        # Ikkita bo'lsa — taxmin qilmaymiz, kassada tanlash kerak
        Organization.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000e2", name="Ikkinchi"
        )
        self.assertIsNone(self.register.organization_ms_id)

        st = self.register.settings
        st.organization_ms_id = org.ms_id
        st.save()
        self.assertEqual(str(self.register.organization_ms_id), str(org.ms_id))
