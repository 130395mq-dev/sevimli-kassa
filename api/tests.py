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

    def test_kassirsiz_ochilmaydi(self):
        r = self.post("/api/v1/shift/open", {"cashier": "  "})
        self.assertEqual(r.status_code, 400)

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
