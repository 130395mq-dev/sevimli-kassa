"""Kassa3 tiqilgan cheki (9d957adc, 27.09.2026) — bir martalik tuzatish.

Egasining qarori (01.10.2026): 3-qator (xato o'qilgan «колбаса», 1 032 so'm)
olib tashlanadi, to'lov shuncha kamayadi, qolgan qatorlar qabul qilinadi.
Tuzatish FAQAT shu chek va FAQAT aynan shu qator uchun.
"""
import uuid
from decimal import Decimal

from api.test_price_label import KASSA3_QTY, MoneyRuleBase
from catalog.models import Product
from sales.models import Sale

KASSA3_UUID = "9d957adc-32c6-4935-bf3d-58bf9cd100e0"


class Kassa3StuckReceiptTest(MoneyRuleBase):
    def setUp(self):
        super().setUp()
        self.kolbasa = Product.objects.create(pk=52876, ms_id=uuid.uuid4(),
                                              name="колбаса ТК SEVIMLI (кг)", sale_price=59_990_00)

    def receipt(self, local_uuid=KASSA3_UUID, bad_total=1_032_00, pays=None):
        p = self.sale_payload(local_uuid=local_uuid)
        line = dict(p["items"][0])
        p["items"] = [
            dict(line, quantity="2.000", total=6_000_00),                       # 2 × 3 000
            dict(line, quantity="1.000", total=3_000_00),
            dict(line, product_id=self.kolbasa.pk, ms_product_id=str(self.kolbasa.ms_id),
                 name="колбаса ТК SEVIMLI", price=59_990_00, quantity=KASSA3_QTY,
                 total=bad_total),
        ]
        whole = 9_000_00 + bad_total
        p["gross_total"] = whole
        p["payments"] = pays if pays is not None else [
            {"method": "naqd", "amount": whole, "tendered": 20_000_00, "change": 20_000_00 - whole}]
        return p

    def test_hozir_rad_etilishi_isboti(self):
        """Tuzatishsiz (boshqa uuid) aynan shu chek rad etiladi."""
        r = self.post("/api/v1/sales", self.receipt(local_uuid=str(uuid.uuid4())))
        self.assertEqual(r.status_code, 400)
        self.assertIn("3 tadan ko'p kasr", r.json()["error"])

    def test_kassa3_cheki_qabul_qilinadi_3_qatorsiz(self):
        with self.assertLogs("api", level="WARNING") as logs:
            r = self.post("/api/v1/sales", self.receipt())
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(any("qo'lda tuzatildi" in m for m in logs.output))
        sale = Sale.objects.get(local_uuid=KASSA3_UUID)
        self.assertEqual([(i.product_id, i.quantity, i.total) for i in sale.items.order_by("pk")],
                         [(self.product.pk, Decimal("2.000"), 6_000_00),
                          (self.product.pk, Decimal("1.000"), 3_000_00)])
        self.assertEqual(sale.net_total, 9_000_00)
        pay = sale.payments.get()
        self.assertEqual((pay.amount, pay.tendered), (9_000_00, 20_000_00))
        self.assert_receipt_consistent(sale)

    def test_takror_yuborilsa_dublikat_yoq(self):
        self.assertEqual(self.post("/api/v1/sales", self.receipt()).status_code, 201)
        r = self.post("/api/v1/sales", self.receipt())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["duplicate"])
        self.assertEqual(Sale.objects.filter(local_uuid=KASSA3_UUID).count(), 1)

    def test_aralash_tolov_avval_naqd_kamayadi(self):
        pays = [{"method": "terminal-1", "amount": 5_000_00},
                {"method": "naqd", "amount": 5_032_00}]
        r = self.post("/api/v1/sales", self.receipt(pays=pays))
        self.assertEqual(r.status_code, 201, r.content)
        sale = Sale.objects.get(local_uuid=KASSA3_UUID)
        got = {p.method.code: p.amount for p in sale.payments.all()}
        self.assertEqual(got, {"terminal-1": 5_000_00, "naqd": 4_000_00})

    def test_naqd_yetmasa_qolgani_kartadan_nol_tolov_tashlanadi(self):
        pays = [{"method": "terminal-1", "amount": 9_532_00},
                {"method": "naqd", "amount": 500_00}]
        r = self.post("/api/v1/sales", self.receipt(pays=pays))
        self.assertEqual(r.status_code, 201, r.content)
        sale = Sale.objects.get(local_uuid=KASSA3_UUID)
        self.assertEqual({p.method.code: p.amount for p in sale.payments.all()},
                         {"terminal-1": 9_000_00})

    def test_qator_boshqacha_bolsa_tuzatilmaydi(self):
        r = self.post("/api/v1/sales", self.receipt(bad_total=1_033_00))
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Sale.objects.filter(local_uuid=KASSA3_UUID).exists())

    def test_boshqa_chekka_tasir_yoq(self):
        from api import corrections

        p = self.receipt(local_uuid=str(uuid.uuid4()))
        before = [dict(i) for i in p["items"]]
        self.assertIsNone(corrections.apply(p["local_uuid"], p))
        self.assertEqual(p["items"], before)
