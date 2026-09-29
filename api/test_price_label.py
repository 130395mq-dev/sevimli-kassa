"""I01/I02: pul qoidasi va miqdor aniqligi (2026-09-28, 3-nashr).

Kassa3 cheki (9d957adc, 27.09 16:28) sababi chek nusxasi bilan aniqlandi:
katalogda yo'q 21… zavod kodi kassada «narxli tarozi yorlig'i» deb o'qilib,
PLU'si mos «колбаса ТК SEVIMLI»ga aylangan, kod raqamlari narx (1 032 so'm)
deb olingan -> miqdor 0,0172028… kg. Sevimli'da narxli yorliq ishlatilmaydi
(tarozi faqat 29), shuning uchun uzun kasr — faqat xato o'qishning belgisi.

QOIDA (api/views.py::_save_sale):
  * miqdor ko'pi bilan 3 kasr xonasi — uzun kasr RAD etiladi (yaxlitlanmaydi);
  * brutto = narx x miqdor (tiyingacha HALF_UP), summa <= brutto,
    chegirma = brutto - summa (ruxsat va foiz chegarasi); tolerantlik yo'q;
  * chek jami = qatorlar yig'indisi - ball; to'lovlar = jami;
  * asl chekli qaytarish summasi validate_return'da asl pul bo'yicha.
"""
import uuid
from decimal import ROUND_HALF_UP, Decimal

from api.tests import ApiTestCase
from catalog.models import Customer, Product
from sales.models import BonusProgram, Sale, SaleItem
from sales.writer import allocate, position_price

OLD_POS_THIRD = str(Decimal(1_000_00) / Decimal(3_000_00))    # eski kassa: 0.3333…
KASSA3_QTY = str(Decimal(1_032_00) / Decimal(59_990_00))       # kassa3: 0.017202867…


def pos_line_total(price, qty):
    return int((Decimal(price) * Decimal(qty)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def pos_refund_total(item, qty):
    """Kassadagi pos/money.refund_total bilan bir xil."""
    qty = Decimal(str(qty))
    sold = Decimal(str(item["sold_qty"]))
    already = Decimal(str(item.get("returned_qty") or 0))
    target = Decimal(item["refund_total"]) * (already + qty) / sold
    return int(target.quantize(Decimal("1"), rounding=ROUND_HALF_UP)) - int(item.get("returned_total") or 0)


class MoneyRuleBase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.open_shift()
        self.settings(allow_discount=False)   # qat'iy: 1 tiyin farq ham chegirma

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
        items = list(sale.items.all())
        lines = sum(i.total for i in items)
        self.assertEqual(sale.net_total, lines - sale.points_spent * 100)
        self.assertEqual(sale.gross_total - sale.discount_total, lines)
        self.assertEqual(sum(p.amount for p in sale.payments.all()), sale.net_total)
        cuts = allocate(sale.points_spent * 100, [i.total for i in items])
        for item, cut in zip(items, cuts):
            amount = item.total - cut
            price = position_price(amount, Decimal(item.quantity))
            back = (Decimal(str(price)) * Decimal(item.quantity)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP)
            self.assertEqual(back, amount)


class QuantityPrecisionTest(MoneyRuleBase):
    def test_eski_kassa_uzun_kasri_rad_etiladi(self):
        self.assertEqual(pos_line_total(3_000_00, OLD_POS_THIRD), 1_000_00)
        r = self.post("/api/v1/sales", self.sale(OLD_POS_THIRD, 1_000_00))
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn("3 tadan ko'p kasr", r.json()["error"])
        self.assertFalse(Sale.objects.exists())

    def test_kassa3_holati_rad_etiladi(self):
        kolbasa = Product.objects.create(ms_id=uuid.uuid4(), name="колбаса ТК SEVIMLI (кг)",
                                         sale_price=59_990_00)
        self.assertEqual(pos_line_total(59_990_00, KASSA3_QTY), 1_032_00)
        r = self.post("/api/v1/sales", self.sale(KASSA3_QTY, 1_032_00, product=kolbasa))
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Sale.objects.exists())

    def test_ayni_uuid_qayta_yuborilsa_ham_rad_dublikat_yoq(self):
        p = self.sale(OLD_POS_THIRD, 1_000_00)
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)
        self.assertFalse(Sale.objects.exists())

    def test_mayda_ortiqcha_kasr_ham_rad(self):
        self.assertEqual(self.post("/api/v1/sales", self.sale("0.0004", 120)).status_code, 400)
        self.assertEqual(self.post("/api/v1/sales", self.sale("0.00100001", 300)).status_code, 400)

    def test_yangi_kassa_gramm_miqdori(self):
        total = pos_line_total(3_000_00, "0.333")
        self.assertEqual(total, 999_00)
        r = self.post("/api/v1/sales", self.sale("0.333", total))
        self.assertEqual(r.status_code, 201, r.content)
        sale = Sale.objects.get()
        self.assertEqual(sale.items.get().quantity, Decimal("0.333"))
        self.assertEqual((sale.gross_total, sale.discount_total, sale.net_total), (999_00, 0, 999_00))
        self.assert_receipt_consistent(sale)

    def test_tarozi_29_vazni(self):
        r = self.post("/api/v1/sales", self.sale("0.365", 1_095_00))
        self.assertEqual(r.status_code, 201, r.content)
        self.assert_receipt_consistent(Sale.objects.get())

    def test_dona_savdo(self):
        r = self.post("/api/v1/sales", self.sale("3", 9_000_00))
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(SaleItem.objects.get().quantity, Decimal("3.000"))
        self.assert_receipt_consistent(Sale.objects.get())

    def test_upakovka_16_dona(self):
        r = self.post("/api/v1/sales", self.sale("16.000", 48_000_00))
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(SaleItem.objects.get().quantity, Decimal("16.000"))
        self.assert_receipt_consistent(Sale.objects.get())


class HighPriceTest(MoneyRuleBase):
    def setUp(self):
        super().setUp()
        self.expensive = Product.objects.create(ms_id=uuid.uuid4(), name="Zafaron",
                                                sale_price=1_250_000_00)

    def test_qimmat_tovar_gramm_bilan(self):
        r = self.post("/api/v1/sales", self.sale("0.037", 46_250_00, product=self.expensive))
        self.assertEqual(r.status_code, 201, r.content)
        self.assert_receipt_consistent(Sale.objects.get())

    def test_bir_tiyin_ortiqcha_rad(self):
        self.assertEqual(self.post("/api/v1/sales", self.sale("0.037", 46_250_01, product=self.expensive)).status_code, 400)

    def test_yashirin_chegirma_rad(self):
        self.assertEqual(self.post("/api/v1/sales", self.sale("0.037", 45_678_00, product=self.expensive)).status_code, 400)


class DiscountBonusPaymentTest(MoneyRuleBase):
    def test_chegirmasiz_kassada_bir_tiyin_kam_rad(self):
        r = self.post("/api/v1/sales", self.sale("0.333", 998_99))
        self.assertEqual(r.status_code, 400)
        self.assertIn("chegirma", r.json()["error"].lower())

    def test_chegirma_foizi_chegarasi(self):
        self.settings(allow_discount=True, max_discount=10)
        self.assertEqual(self.post("/api/v1/sales", self.sale("0.333", 899_10)).status_code, 201)
        sale = Sale.objects.get()
        self.assertEqual((sale.gross_total, sale.discount_total, sale.net_total), (999_00, 99_90, 899_10))
        self.assert_receipt_consistent(sale)
        self.assertEqual(self.post("/api/v1/sales", self.sale("0.333", 898_00)).status_code, 400)
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
        p = self.sale("0.333", 999_00, customer_id=cust.pk, points_spent=300,
                      pays=[{"method": "naqd", "amount": 500_00},
                            {"method": "terminal-1", "amount": 199_00}])
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 201)
        sale = Sale.objects.get()
        self.assertEqual((sale.net_total, sale.points_spent), (699_00, 300))
        self.assert_receipt_consistent(sale)

    def test_aralash_tolovda_bir_tiyin_farq_rad_ball_yechilmaydi(self):
        cust = self._bonus_customer()
        p = self.sale("0.333", 999_00, customer_id=cust.pk, points_spent=300,
                      pays=[{"method": "naqd", "amount": 500_00},
                            {"method": "terminal-1", "amount": 199_01}])
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)
        cust.refresh_from_db()
        self.assertEqual(cust.bonus_points, 1_000)
        self.assertFalse(Sale.objects.exists())

    def test_ball_yetmasa_rad(self):
        cust = self._bonus_customer(points=100)
        p = self.sale("0.333", 999_00, customer_id=cust.pk, points_spent=300,
                      pays=[{"method": "naqd", "amount": 699_00}])
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)


class TamperTest(MoneyRuleBase):
    def test_summa_oshirilgan(self):
        self.assertEqual(self.post("/api/v1/sales", self.sale("0.333", 999_01)).status_code, 400)

    def test_narx_buzilgan(self):
        p = self.sale("0.333", 999_00)
        p["items"][0]["price"] = 3_010_00
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)
        p = self.sale("0.333", 333)
        p["items"][0]["price"] = 1_000
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)
        self.assertFalse(Sale.objects.exists())

    def test_miqdor_kamaytirilgan(self):
        self.assertEqual(self.post("/api/v1/sales", self.sale("0.3", 999_00)).status_code, 400)

    def test_miqdor_oshirilgan(self):
        self.assertEqual(self.post("/api/v1/sales", self.sale("0.5", 999_00)).status_code, 400)

    def test_jami_maydonlariga_ishonilmaydi(self):
        p = self.sale("0.333", 999_00)
        p.update(gross_total=999_999_00, discount_total=5_00, net_total=1_00)
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 201)
        sale = Sale.objects.get()
        self.assertEqual((sale.gross_total, sale.discount_total, sale.net_total), (999_00, 0, 999_00))


class ResendTest(MoneyRuleBase):
    def test_ayni_uuid_dublikat_yoq(self):
        p = self.sale("0.333", 999_00)
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 201)
        r = self.post("/api/v1/sales", p)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["duplicate"])
        self.assertEqual(Sale.objects.count(), 1)

    def test_ayni_uuid_buzilgan_summa_bilan_asl_chek_qoladi(self):
        p = self.sale("0.333", 999_00)
        first = self.post("/api/v1/sales", p).json()
        p["items"][0]["total"] = 5_000_00
        p["payments"] = [{"method": "naqd", "amount": 5_000_00}]
        self.assertEqual(self.post("/api/v1/sales", p).json()["id"], first["id"])
        self.assertEqual(Sale.objects.get().net_total, 999_00)


class WeightReturnTest(MoneyRuleBase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.post("/api/v1/sales", self.sale("0.333", 999_00)).status_code, 201)
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

    def test_toliq_qaytarish(self):
        self.assertEqual(self.refund("0.333").status_code, 201)
        self.assertEqual(self.refunded(), 999_00)
        self.assertEqual(self.refund("0.001").status_code, 400)

    def test_qisman_keyin_qolgani(self):
        self.assertEqual(self.refund("0.1").status_code, 201)
        self.assertEqual(self.refunded(), 300_00)
        self.assertEqual(self.refund("0.233").status_code, 201)
        self.assertEqual(self.refunded(), 999_00)
        self.assertEqual(self.refund("0.001").status_code, 400)

    def test_summa_oshirilgan_rad(self):
        self.assertEqual(self.refund("0.333", total=999_01).status_code, 400)
        self.assertEqual(self.refund("0.1", total=300_01).status_code, 400)
        self.assertEqual(self.refunded(), 0)

    def test_narx_miqdor_buzilgan_rad(self):
        self.assertEqual(self.refund("0.333", total=999_00, price=3_010_00).status_code, 400)
        self.assertEqual(self.refund("0.334", total=999_00).status_code, 400)
        self.assertEqual(self.refund(OLD_POS_THIRD, total=999_00).status_code, 400)
        self.assertEqual(self.refunded(), 0)

    def test_asl_cheksiz_qaytarish_chegarasi(self):
        s = self.register.settings
        s.allow_returns_no_reason = True
        s.save()
        self.assertEqual(self.post("/api/v1/sales", self.sale("0.333", 1_000_00, kind="return")).status_code, 400)
        self.assertEqual(self.post("/api/v1/sales", self.sale("0.333", 999_00, kind="return")).status_code, 201)


class DiagnosticLogTest(MoneyRuleBase):
    def test_log_sababni_korsatadi_token_mijoz_yoq(self):
        cust = Customer.objects.create(ms_id=uuid.uuid4(), name="Maxfiy Ism",
                                       phone="+998901112233", discount_card="CARD-777")
        p = self.sale(KASSA3_QTY, 1_000_00, customer_id=cust.pk)
        with self.assertLogs("api", level="WARNING") as logs:
            self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)
        text = "\n".join(logs.output)
        self.assertIn("qator rad etildi", text)
        self.assertIn("0.01720286", text)
        for secret in (self.register.api_token, "Maxfiy Ism", "+998901112233", "CARD-777"):
            self.assertNotIn(secret, text)

    def test_log_qatori_qisqa_bir_qatorli(self):
        p = self.sale("0.3\n" * 40, 1_000_00)
        with self.assertLogs("api", level="WARNING") as logs:
            self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)
        line = next(o for o in logs.output if "qator rad etildi" in o)
        self.assertNotIn("\n", line)
        self.assertLess(len(line), 400)
