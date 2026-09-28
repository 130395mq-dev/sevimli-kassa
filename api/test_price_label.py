"""I01/I02: pul qoidasi — narxli tarozi yorlig'i va miqdorni grammgacha saqlash.

2026-09-27: kassa3 chekini server «3 tadan ko'p kasr xonasi» deb rad etgan,
chek soatlab navbatda tiqilib qolgan. Kassa (<=1.18.6) narxli yorliqda
miqdorni «yorliq summasi / narx» qilib hisoblaydi: 1 000 / 3 000 = 0,3333...

QOIDA (api/views.py::_save_sale, «PUL QOIDASI» izohi):
  * pul KASSA YUBORGAN aniq miqdor bilan tekshiriladi — brutto = narx x miqdor
    (tiyingacha HALF_UP), summa <= brutto, chegirma = brutto - summa;
    hech qanday qo'shimcha tolerantlik yo'q;
  * faqat saqlanadigan miqdor grammgacha yaxlitlanadi, summa (pul) o'zgarmaydi;
  * asl chekli qaytarish summasi validate_return'da asl pul bo'yicha aniq.

Bu fayldagi testlar talab qilingan matritsani qoplaydi: eski kassa uzun
kasri, yangi kassa gramm miqdori, dona, upakovka, qimmat tovar, chegirma,
bonus + aralash to'lov, qisman/to'liq qaytarish, ayni chekni qayta yuborish,
narx/miqdor/summa buzilgan so'rov, chek summasi = qatorlar yig'indisi,
MoySklad narxi summani aniq qaytarishi va diagnostika logi tozaligi.
"""
import uuid
from decimal import ROUND_HALF_UP, Decimal

from api.tests import ApiTestCase
from catalog.models import Customer, Product
from sales.models import BonusProgram, Sale, SaleItem
from sales.writer import allocate, position_price

# Eski kassa (<=1.18.6) aynan shunday hisoblaydi: Decimal(yorliq)/Decimal(narx),
# Python'ning standart 28 xonali konteksti bilan.
OLD_POS_THIRD = str(Decimal(1_000_00) / Decimal(3_000_00))   # 0.3333...(28 ta 3)


def pos_line_total(price, qty):
    """Kassadagi pos/money.line_total bilan bir xil (1.18.6 va 1.18.7)."""
    return int((Decimal(price) * Decimal(qty)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def pos_refund_total(item, qty):
    """Kassadagi pos/money.refund_total bilan bir xil (1.18.6 va 1.18.7)."""
    qty = Decimal(str(qty))
    sold = Decimal(str(item["sold_qty"]))
    already = Decimal(str(item.get("returned_qty") or 0))
    target = Decimal(item["refund_total"]) * (already + qty) / sold
    return int(target.quantize(Decimal("1"), rounding=ROUND_HALF_UP)) - int(item.get("returned_total") or 0)


class MoneyRuleBase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.open_shift()
        # Qat'iy boshlang'ich holat: chegirma yo'q. Standart sozlama (1%)
        # 1 tiyinlik farqni «chegirma» sifatida o'tkazib yuborib, testni
        # yumshatib qo'ymasin. Chegirma testlari o'zi yoqadi.
        self.settings(allow_discount=False)

    def settings(self, allow_discount=False, max_discount=0):
        s = self.register.settings
        s.allow_discount = allow_discount
        s.max_discount = max_discount
        s.save()

    def sale(self, quantity, total, product=None, pays=None, **over):
        product = product or self.product
        p = self.sale_payload(**over)
        p["items"][0].update(product_id=product.pk, ms_product_id=str(product.ms_id),
                             price=int(product.sale_price), quantity=quantity, total=total)
        p["gross_total"] = total
        if pays is None:
            pays = [{"method": "naqd", "amount": total - int(over.get("points_spent") or 0) * 100}]
        p["payments"] = pays
        return p

    def assert_receipt_consistent(self, sale):
        """Chek summasi = qatorlar yig'indisi (ball ayirilgan) = to'lovlar."""
        items = list(sale.items.all())
        lines = sum(i.total for i in items)
        self.assertEqual(sale.net_total, lines - sale.points_spent * 100)
        self.assertEqual(sale.gross_total - sale.discount_total, lines)
        self.assertEqual(sum(p.amount for p in sale.payments.all()), sale.net_total)
        # MoySklad'ga ketadigan narx har qatorda summani aniq qaytaradi
        cuts = allocate(sale.points_spent * 100, [i.total for i in items])
        for item, cut in zip(items, cuts):
            amount = item.total - cut
            price = position_price(amount, Decimal(item.quantity))
            back = (Decimal(str(price)) * Decimal(item.quantity)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP)
            self.assertEqual(back, amount)


class OldAndNewPosQuantityTest(MoneyRuleBase):
    def test_eski_kassa_uzun_kasri_qabul_qilinadi_pul_ozgarmaydi(self):
        self.assertEqual(pos_line_total(3_000_00, OLD_POS_THIRD), 1_000_00)
        r = self.post("/api/v1/sales", self.sale(OLD_POS_THIRD, 1_000_00))
        self.assertEqual(r.status_code, 201, r.content)
        sale = Sale.objects.get()
        item = sale.items.get()
        self.assertEqual(item.quantity, Decimal("0.333"))   # faqat miqdor grammgacha
        self.assertEqual(item.total, 1_000_00)               # kassa olgan pul
        self.assertEqual(sale.net_total, 1_000_00)
        self.assertEqual(sale.gross_total, 1_000_00)
        self.assertEqual(sale.discount_total, 0)             # yaxlitlash chegirma emas
        self.assert_receipt_consistent(sale)

    def test_eski_kassa_boshqa_narx_bolinmaydigan(self):
        product = Product.objects.create(ms_id=uuid.uuid4(), name="Pishloq", sale_price=7_000_00)
        qty = str(Decimal(1_000_00) / Decimal(7_000_00))       # 0.142857...
        r = self.post("/api/v1/sales", self.sale(qty, 1_000_00, product=product))
        self.assertEqual(r.status_code, 201, r.content)
        item = SaleItem.objects.get()
        self.assertEqual((item.quantity, item.total), (Decimal("0.143"), 1_000_00))
        self.assert_receipt_consistent(Sale.objects.get())

    def test_yangi_kassa_gramm_miqdori(self):
        # 1.18.7: label_quantity -> 0.333, summa = narx x 0.333
        total = pos_line_total(3_000_00, "0.333")
        self.assertEqual(total, 999_00)
        r = self.post("/api/v1/sales", self.sale("0.333", total))
        self.assertEqual(r.status_code, 201, r.content)
        sale = Sale.objects.get()
        self.assertEqual(sale.items.get().quantity, Decimal("0.333"))
        self.assertEqual((sale.gross_total, sale.discount_total, sale.net_total), (999_00, 0, 999_00))
        self.assert_receipt_consistent(sale)

    def test_dona_savdo_avvalgidek(self):
        r = self.post("/api/v1/sales", self.sale("3", 9_000_00))
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(SaleItem.objects.get().quantity, Decimal("3.000"))
        self.assert_receipt_consistent(Sale.objects.get())

    def test_upakovka_16_dona(self):
        # Kassa 4: upakovka shtrix-kodi 16 donani bitta qatorda yuboradi
        r = self.post("/api/v1/sales", self.sale("16.000", 48_000_00))
        self.assertEqual(r.status_code, 201, r.content)
        item = SaleItem.objects.get()
        self.assertEqual((item.quantity, item.total), (Decimal("16.000"), 48_000_00))
        self.assert_receipt_consistent(Sale.objects.get())

    def test_oddiy_3_xonali_miqdor_avvalgidek(self):
        r = self.post("/api/v1/sales", self.sale("0.365", 1_095_00))
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(SaleItem.objects.get().quantity, Decimal("0.365"))

    def test_juda_kichik_miqdor_rad_etiladi(self):
        r = self.post("/api/v1/sales", self.sale("0.0004", pos_line_total(3_000_00, "0.0004")))
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Sale.objects.exists())


class HighPriceTest(MoneyRuleBase):
    """Qimmat tovar: yarim gramm = 625 so'm. Pul baribir tiyingacha aniq."""

    def setUp(self):
        super().setUp()
        self.expensive = Product.objects.create(ms_id=uuid.uuid4(), name="Zafaron",
                                                sale_price=1_250_000_00)
        self.qty = str(Decimal(45_678_00) / Decimal(1_250_000_00))   # 0.0365424

    def test_qimmat_tovar_eski_kassa_yorligi(self):
        r = self.post("/api/v1/sales", self.sale(self.qty, 45_678_00, product=self.expensive))
        self.assertEqual(r.status_code, 201, r.content)
        sale = Sale.objects.get()
        item = sale.items.get()
        self.assertEqual((item.quantity, item.total), (Decimal("0.037"), 45_678_00))
        self.assertEqual(sale.discount_total, 0)
        self.assert_receipt_consistent(sale)

    def test_qimmat_tovarda_bir_tiyin_ortiqcha_ham_rad(self):
        r = self.post("/api/v1/sales", self.sale(self.qty, 45_678_01, product=self.expensive))
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Sale.objects.exists())

    def test_qimmat_tovarda_yarim_gramm_niqobidagi_chegirma_rad(self):
        # Saqlangan 0.037 kg x 1 250 000 = 46 250 so'm; 45 678 so'm kam —
        # lekin tekshiruv yuborilgan 0.0365424 bilan: chegirma 0 so'm.
        # Endi 45 000 so'm yuborilsa — bu 678 so'm chegirma, ruxsat yo'q.
        r = self.post("/api/v1/sales", self.sale(self.qty, 45_000_00, product=self.expensive))
        self.assertEqual(r.status_code, 400)


class DiscountBonusPaymentTest(MoneyRuleBase):
    def test_chegirmasiz_kassada_bir_tiyin_kam_ham_rad(self):
        # Tolerantlik yo'q: 1 tiyin kam = chegirma, ruxsatsiz kassada rad
        self.settings(allow_discount=False)
        r = self.post("/api/v1/sales", self.sale(OLD_POS_THIRD, 999_99))
        self.assertEqual(r.status_code, 400)
        self.assertIn("chegirma", r.json()["error"].lower())

    def test_chegirma_foizi_aniq_miqdordan_hisoblanadi(self):
        self.settings(allow_discount=True, max_discount=10)
        r = self.post("/api/v1/sales", self.sale(OLD_POS_THIRD, 900_00))      # 10%
        self.assertEqual(r.status_code, 201, r.content)
        sale = Sale.objects.get()
        self.assertEqual((sale.gross_total, sale.discount_total, sale.net_total),
                         (1_000_00, 100_00, 900_00))
        self.assert_receipt_consistent(sale)
        r = self.post("/api/v1/sales", self.sale(OLD_POS_THIRD, 899_00))      # 10,1%
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Sale.objects.count(), 1)

    def _bonus_customer(self, points=1_000):
        prog = BonusProgram.get()
        prog.active = True
        prog.redeem_enabled = True
        prog.max_redeem_percent = 100
        prog.save()
        return Customer.objects.create(ms_id=uuid.uuid4(), name="Mijoz", phone="+998900000000",
                                       bonus_points=points)

    def test_bonus_va_aralash_tolov(self):
        cust = self._bonus_customer()
        # 1 000 so'm yorliq: 300 ball + 500 so'm naqd + 200 so'm karta
        p = self.sale(OLD_POS_THIRD, 1_000_00, customer_id=cust.pk, points_spent=300,
                      pays=[{"method": "naqd", "amount": 500_00},
                            {"method": "terminal-1", "amount": 200_00}])
        r = self.post("/api/v1/sales", p)
        self.assertEqual(r.status_code, 201, r.content)
        sale = Sale.objects.get()
        self.assertEqual((sale.net_total, sale.points_spent), (700_00, 300))
        self.assert_receipt_consistent(sale)

    def test_aralash_tolov_bir_tiyin_farq_rad(self):
        cust = self._bonus_customer()
        p = self.sale(OLD_POS_THIRD, 1_000_00, customer_id=cust.pk, points_spent=300,
                      pays=[{"method": "naqd", "amount": 500_00},
                            {"method": "terminal-1", "amount": 200_01}])
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)
        cust.refresh_from_db()
        self.assertEqual(cust.bonus_points, 1_000)          # ball yechilmadi
        self.assertFalse(Sale.objects.exists())

    def test_ball_mijozda_yoq_bolsa_rad(self):
        cust = self._bonus_customer(points=100)
        p = self.sale(OLD_POS_THIRD, 1_000_00, customer_id=cust.pk, points_spent=300,
                      pays=[{"method": "naqd", "amount": 700_00}])
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)


class TamperTest(MoneyRuleBase):
    def test_summa_bir_tiyin_oshirilgan(self):
        r = self.post("/api/v1/sales", self.sale(OLD_POS_THIRD, 1_000_01))
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Sale.objects.exists())

    def test_yaxlitlashdan_katta_ortiqcha_summa_rad_etiladi(self):
        r = self.post("/api/v1/sales", self.sale(OLD_POS_THIRD, 1_010_00))
        self.assertEqual(r.status_code, 400)

    def test_narx_buzilgan(self):
        p = self.sale(OLD_POS_THIRD, 1_000_00)
        p["items"][0]["price"] = 3_010_00              # katalogda 3 000 so'm
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)
        p = self.sale("0.333", 333)
        p["items"][0]["price"] = 1_000                  # 10 so'm/kg
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)
        self.assertFalse(Sale.objects.exists())

    def test_miqdor_kamaytirilgan_summa_ozgarmagan(self):
        # 0.3 kg uchun 1 000 so'm — brutto 900 so'm: rad
        r = self.post("/api/v1/sales", self.sale("0.3", 1_000_00))
        self.assertEqual(r.status_code, 400)

    def test_miqdor_oshirilgan_summa_ozgarmagan(self):
        # 0.5 kg uchun 1 000 so'm — bu 33% chegirma, ruxsat yo'q: rad
        r = self.post("/api/v1/sales", self.sale("0.5", 1_000_00))
        self.assertEqual(r.status_code, 400)

    def test_kassa_yuborgan_jami_maydonlariga_ishonilmaydi(self):
        p = self.sale(OLD_POS_THIRD, 1_000_00)
        p.update(gross_total=999_999_00, discount_total=5_00, net_total=1_00)
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 201)
        sale = Sale.objects.get()
        self.assertEqual((sale.gross_total, sale.discount_total, sale.net_total),
                         (1_000_00, 0, 1_000_00))


class ResendTest(MoneyRuleBase):
    def test_ayni_uuid_qayta_yuborilsa_dublikat_yoq(self):
        p = self.sale(OLD_POS_THIRD, 1_000_00)
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 201)
        r = self.post("/api/v1/sales", p)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["duplicate"])
        self.assertEqual(Sale.objects.count(), 1)

    def test_ayni_uuid_buzilgan_summa_bilan_kelsa_asl_chek_qoladi(self):
        p = self.sale(OLD_POS_THIRD, 1_000_00)
        first = self.post("/api/v1/sales", p).json()
        p["items"][0]["total"] = 5_000_00
        p["payments"] = [{"method": "naqd", "amount": 5_000_00}]
        r = self.post("/api/v1/sales", p)
        self.assertEqual(r.json()["id"], first["id"])
        self.assertEqual(Sale.objects.get().net_total, 1_000_00)
        self.assertEqual(SaleItem.objects.get().total, 1_000_00)


class LabelReturnTest(MoneyRuleBase):
    """Grammgacha saqlangan qatorni qaytarish: pul asl chekdagidan oshmaydi."""

    def setUp(self):
        super().setUp()
        r = self.post("/api/v1/sales", self.sale(OLD_POS_THIRD, 1_000_00))
        self.assertEqual(r.status_code, 201, r.content)
        self.origin = Sale.objects.get(kind=Sale.SALE)

    def returnable_item(self):
        sales = self.client.get("/api/v1/sales/returnable", **self.auth()).json()["sales"]
        return next(s for s in sales if s["id"] == self.origin.pk)["items"][0]

    def refund(self, qty, total=None, **changes):
        item = self.returnable_item()
        if total is None:
            total = pos_refund_total(item, qty)
        line = {"origin_item_id": item["origin_item_id"], "product_id": item["product_id"],
                "ms_product_id": item["ms_product_id"], "name": item["name"],
                "quantity": str(qty), "price": item["price"], "total": total}
        line.update(changes)
        payload = {"local_uuid": str(uuid.uuid4()), "kind": "return", "origin_id": self.origin.pk,
                   "gross_total": total, "net_total": total, "items": [line],
                   "payments": [{"method": "naqd", "amount": total}] if total else []}
        return self.post("/api/v1/sales", payload)

    def refunded(self):
        return sum(s.net_total for s in Sale.objects.filter(kind=Sale.RETURN))

    def test_toliq_qaytarish_kassa_bergan_miqdor_bilan(self):
        item = self.returnable_item()
        self.assertEqual((item["sold_qty"], item["refund_total"]), ("0.333", 1_000_00))
        r = self.refund("0.333")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.refunded(), 1_000_00)          # olingan pul to'liq qaytdi
        ret = Sale.objects.get(kind=Sale.RETURN)
        self.assertEqual(ret.items.get().quantity, Decimal("0.333"))
        self.assertEqual(self.refund("0.001").status_code, 400)   # boshqa qolmadi

    def test_qisman_keyin_qolgani(self):
        self.assertEqual(self.refund("0.1").status_code, 201)
        self.assertEqual(self.refunded(), 300_30)                 # 1 000 x 0.1/0.333
        self.assertEqual(self.refund("0.233").status_code, 201)
        self.assertEqual(self.refunded(), 1_000_00)               # jami — aynan olingan pul
        self.assertEqual(self.refund("0.001").status_code, 400)

    def test_eski_kassa_uzun_miqdor_bilan_qaytarsa(self):
        r = self.refund(OLD_POS_THIRD, total=1_000_00)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.refunded(), 1_000_00)

    def test_qaytarish_summasi_oshirilgan_rad(self):
        self.assertEqual(self.refund("0.333", total=1_000_01).status_code, 400)
        self.assertEqual(self.refund("0.1", total=300_31).status_code, 400)
        self.assertEqual(self.refunded(), 0)

    def test_qaytarish_narxi_yoki_miqdori_buzilgan_rad(self):
        self.assertEqual(self.refund("0.333", total=1_000_00, price=3_010_00).status_code, 400)
        self.assertEqual(self.refund("0.334", total=1_000_00).status_code, 400)
        self.assertEqual(self.refunded(), 0)

    def test_asl_cheksiz_qaytarishda_narx_x_miqdor_chegarasi_saqlanadi(self):
        s = self.register.settings
        s.allow_returns_no_reason = True
        s.save()
        p = self.sale("0.333", 1_000_00, kind="return")      # 999 so'mlik tovarga 1 000
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)
        p = self.sale("0.333", 999_00, kind="return")
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 201)


class DiagnosticLogTest(MoneyRuleBase):
    def test_rad_etilgan_qator_logida_token_va_mijoz_yoq(self):
        cust = Customer.objects.create(ms_id=uuid.uuid4(), name="Maxfiy Ism",
                                       phone="+998901112233", discount_card="CARD-777")
        p = self.sale(OLD_POS_THIRD, 1_000_00, customer_id=cust.pk)
        p["items"][0]["quantity"] = "0.3\n" * 40              # uzun, qator ko'chirishli
        with self.assertLogs("api", level="WARNING") as logs:
            self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)
        text = "\n".join(logs.output)
        self.assertIn("rad etildi", text)
        for secret in (self.register.api_token, "Maxfiy Ism", "+998901112233", "CARD-777"):
            self.assertNotIn(secret, text)
        line = next(o for o in logs.output if "qator rad etildi" in o)
        self.assertNotIn("\n", line)
        self.assertLess(len(line), 400)
