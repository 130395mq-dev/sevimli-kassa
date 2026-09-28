"""I01/I02: narxli tarozi yorlig'i — eski kassa uzun kasrli miqdor yuboradi.

2026-09-27: kassa3 chekini server «3 tadan ko'p kasr xonasi» deb rad etgan,
chek soatlab navbatda tiqilib qolgan. Kassa (<=1.18.6) narxli yorliqda
miqdorni «yorliq summasi ÷ narx» qilib hisoblaydi: 1 000 ÷ 3 000 = 0,3333…
"""
from decimal import Decimal

from api.tests import ApiTestCase
from sales.models import Sale, SaleItem


class PriceLabelQuantityTest(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.open_shift()

    def _label_sale(self, quantity, total, pay=None):
        # Mahsulot narxi 3 000 so'm (300 000 tiyin), yorliq 1 000 so'm
        p = self.sale_payload()
        p["items"][0].update(quantity=quantity, total=total)
        p["gross_total"] = total
        p["payments"] = [{"method": "naqd", "amount": pay if pay is not None else total}]
        return p

    def test_eski_kassa_uzun_kasri_qabul_qilinadi_pul_ozgarmaydi(self):
        p = self._label_sale("0.33333333333333333333", 1_000_00)
        r = self.post("/api/v1/sales", p)
        self.assertEqual(r.status_code, 201, r.content)
        item = SaleItem.objects.get()
        self.assertEqual(item.quantity, Decimal("0.333"))   # grammgacha
        self.assertEqual(item.total, 1_000_00)                # kassa olgan pul
        self.assertEqual(Sale.objects.get().net_total, 1_000_00)

    def test_ayni_uuid_qayta_yuborilsa_dublikat_yoq(self):
        p = self._label_sale("0.33333333333333333333", 1_000_00)
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 201)
        r = self.post("/api/v1/sales", p)
        self.assertIn(r.status_code, (200, 201), r.content)
        self.assertEqual(Sale.objects.count(), 1)

    def test_yaxlitlashdan_katta_ortiqcha_summa_rad_etiladi(self):
        # 0,333 kg × 3 000 = 999 so'm; 1 010 so'm — yarim grammdan ancha ko'p
        p = self._label_sale("0.33333333333333333333", 1_010_00)
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)
        self.assertFalse(Sale.objects.exists())

    def test_yaxlitlash_niqobi_ostida_chegirma_otmaydi(self):
        # Chegirma ruxsat etilmagan kassa: 0,3333 kg uchun 900 so'm — rad
        p = self._label_sale("0.33333333333333333333", 900_00)
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)

    def test_juda_kichik_miqdor_rad_etiladi(self):
        p = self._label_sale("0.0001", 30)
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)

    def test_oddiy_3_xonali_miqdor_avvalgidek(self):
        p = self._label_sale("0.365", 1_095_00)
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 201)
        self.assertEqual(SaleItem.objects.get().quantity, Decimal("0.365"))
