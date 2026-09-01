"""
Yozuvchi modul testlari.

Bu yerda MoySklad'ga haqiqiy so'rov yuborilmaydi — o'rniga soxta klient
qo'yiladi. Tekshiriladigan narsa: **qanday JSON yuborilishi** va
**takror yozilmasligi**.

Jonli hisobda sinash alohida ish, uni token kelganda qilamiz.

    python manage.py test sales.test_writer
"""

from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from catalog.models import Customer, RetailStore

from .models import Payment, PaymentMethod, Register, Sale, SaleItem, Shift
from .writer import SaleWriter, SumMismatch, WriteError, allocate

RETAIL_CUSTOMER = "00000000-0000-0000-0000-0000000000c9"


class FakeClient:
    """MoySklad o'rniga. Nima yuborilganini eslab qoladi."""

    def __init__(self, *, existing=None, sum_override=None):
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[tuple[str, dict]] = []
        self.existing = existing or {}  # syncId → hujjat
        self.sum_override = sum_override

    def get(self, path, **params):
        self.gets.append((path, params))
        sync_id = (params.get("filter") or "").replace("syncId=", "")
        doc = self.existing.get(sync_id)
        return {"rows": [doc] if doc else []}

    def post(self, path, payload):
        self.posts.append((path, payload))
        doc = {
            "id": "11111111-2222-3333-4444-555555555555",
            "syncId": payload.get("syncId"),
        }
        if path in ("entity/demand", "entity/salesreturn"):
            doc["sum"] = (
                self.sum_override
                if self.sum_override is not None
                else sum(
                    round(p["price"] * p["quantity"]) for p in payload["positions"]
                )
            )
        return doc

    def posted(self, entity):
        return [p for path, p in self.posts if path == f"entity/{entity}"]


class AllocateTest(TestCase):
    """Taqsimlashda bir tiyin ham yo'qolmasligi kerak."""

    def test_yigindi_aniq(self):
        for reduction, amounts in [
            (100, [333, 333, 334]),
            (1, [1, 1, 1]),
            (7, [10, 20, 30]),
            (99999, [12345, 67890, 111]),
        ]:
            out = allocate(reduction, amounts)
            self.assertEqual(sum(out), reduction, f"{reduction} / {amounts}")

    def test_nol_va_bosh(self):
        self.assertEqual(allocate(0, [10, 20]), [0, 0])
        self.assertEqual(allocate(50, []), [])
        self.assertEqual(allocate(50, [0, 0]), [0, 0])


@override_settings(MOYSKLAD_RETAIL_CUSTOMER_ID=RETAIL_CUSTOMER)
class WriterTest(TestCase):
    def setUp(self):
        self.store = RetailStore.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000de",
            name="Namuna filiali",
            organization_ms_id="00000000-0000-0000-0000-0000000000a1",
            store_ms_id="00000000-0000-0000-0000-0000000000b2",
        )
        self.register = Register.objects.create(
            code="k1", name="Kassa-1", store=self.store
        )
        self.shift = Shift.objects.create(
            register=self.register, number=7, cashier="Test", opened_at=timezone.now()
        )
        self.cash = PaymentMethod.objects.create(
            code="naqd", name="Naqd", is_cash=True, sort=1
        )
        self.card = PaymentMethod.objects.create(
            code="terminal-1", name="Terminal-1", sort=2,
            ms_account_id="00000000-0000-0000-0000-0000000000f1",
        )

    def make_sale(self, lines, pays, points_spent=0, points_earned=0, customer=None):
        gross = sum(t for _, _, t in lines)
        net = gross - points_spent * 100
        sale = Sale.objects.create(
            shift=self.shift, number=1, created_at=timezone.now(),
            gross_total=gross, net_total=net,
            points_spent=points_spent, points_earned=points_earned,
            customer=customer,
        )
        for pos, (name, qty, total) in enumerate(lines, start=1):
            SaleItem.objects.create(
                sale=sale, position=pos, name=name,
                quantity=Decimal(qty), price=total, total=total,
                ms_product_id=f"00000000-0000-0000-0000-00000000010{pos}",
            )
        for method, amount in pays:
            Payment.objects.create(sale=sale, method=method, amount=amount)
        return sale

    # ---------------------------------------------------------------

    def test_otgruzka_yoziladi(self):
        sale = self.make_sale(
            [("Non", "1.000", 3_000_00)], [(self.cash, 3_000_00)]
        )
        client = FakeClient()
        SaleWriter(client).send(sale)

        demands = client.posted("demand")
        self.assertEqual(len(demands), 1)
        d = demands[0]
        self.assertEqual(d["syncId"], str(sale.local_uuid))
        self.assertEqual(d["name"], "7-1")
        self.assertTrue(d["applicable"])
        self.assertEqual(len(d["positions"]), 1)
        self.assertEqual(d["positions"][0]["price"], 3_000_00)

        sale.refresh_from_db()
        self.assertIsNotNone(sale.ms_demand_id)

    def test_naqd_cashin_karta_paymentin(self):
        sale = self.make_sale(
            [("Non", "1.000", 100_000_00)],
            [(self.cash, 40_000_00), (self.card, 60_000_00)],
        )
        client = FakeClient()
        SaleWriter(client).send(sale)

        self.assertEqual(len(client.posted("cashin")), 1)
        self.assertEqual(len(client.posted("paymentin")), 1)
        self.assertEqual(client.posted("cashin")[0]["sum"], 40_000_00)
        self.assertEqual(client.posted("paymentin")[0]["sum"], 60_000_00)

        # Kartada hisob raqam ko'rsatilishi kerak, naqdda — yo'q
        self.assertIn("organizationAccount", client.posted("paymentin")[0])
        self.assertNotIn("organizationAccount", client.posted("cashin")[0])

    def test_tolov_otgruzkaga_boglanadi(self):
        sale = self.make_sale(
            [("Non", "1.000", 50_000_00)], [(self.cash, 50_000_00)]
        )
        client = FakeClient()
        SaleWriter(client).send(sale)

        op = client.posted("cashin")[0]["operations"][0]
        self.assertEqual(op["meta"]["type"], "demand")
        self.assertEqual(op["linkedSum"], 50_000_00)

    def test_ball_summani_kamaytiradi(self):
        """Ball bilan to'langan qism Отгрузка summasidan chiqishi kerak."""
        sale = self.make_sale(
            [("Non", "1.000", 100_000_00)],
            [(self.cash, 95_000_00)],
            points_spent=5_000,
        )
        client = FakeClient()
        SaleWriter(client).send(sale)

        d = client.posted("demand")[0]
        total = sum(round(p["price"] * p["quantity"]) for p in d["positions"])
        self.assertEqual(total, 95_000_00)
        self.assertEqual(total, sale.net_total)

    def test_ball_bir_nechta_qatorga_taqsimlanadi(self):
        sale = self.make_sale(
            [("A", "1.000", 33_333_00), ("B", "1.000", 33_333_00),
             ("C", "1.000", 33_334_00)],
            [(self.cash, 90_000_00)],
            points_spent=10_000,
        )
        client = FakeClient()
        SaleWriter(client).send(sale)

        d = client.posted("demand")[0]
        total = sum(round(p["price"] * p["quantity"]) for p in d["positions"])
        self.assertEqual(total, sale.net_total)

    def test_ball_yoziladi(self):
        cust = Customer.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000ca", name="Aliyev"
        )
        sale = self.make_sale(
            [("Non", "1.000", 50_000_00)], [(self.cash, 50_000_00)],
            points_earned=500, customer=cust,
        )
        client = FakeClient()
        SaleWriter(client).send(sale)

        tx = client.posted("bonustransaction")
        self.assertEqual(len(tx), 1)
        self.assertEqual(tx[0]["transactionType"], "EARNING")
        self.assertEqual(tx[0]["bonusValue"], 500)

    def test_mijozsiz_savdo_bal_yozmaydi(self):
        sale = self.make_sale(
            [("Non", "1.000", 50_000_00)], [(self.cash, 50_000_00)],
            points_earned=500,
        )
        client = FakeClient()
        SaleWriter(client).send(sale)
        self.assertEqual(client.posted("bonustransaction"), [])

    # ------------------------------------------------- takror yozilmasin

    def test_mavjud_hujjat_qayta_yozilmaydi(self):
        """MoySklad'da shu syncId bilan hujjat bor bo'lsa — yangisi yaratilmaydi."""
        sale = self.make_sale(
            [("Non", "1.000", 50_000_00)], [(self.cash, 50_000_00)]
        )
        client = FakeClient(existing={
            str(sale.local_uuid): {
                "id": "99999999-9999-9999-9999-999999999999",
                "sum": 50_000_00,
            }
        })
        SaleWriter(client).send(sale)

        self.assertEqual(client.posted("demand"), [])
        sale.refresh_from_db()
        self.assertEqual(str(sale.ms_demand_id), "99999999-9999-9999-9999-999999999999")

    def test_ikki_marta_yuborilsa_bitta_hujjat(self):
        sale = self.make_sale(
            [("Non", "1.000", 50_000_00)], [(self.cash, 50_000_00)]
        )
        client = FakeClient()
        writer = SaleWriter(client)
        writer.send(sale)
        writer.send(sale)  # takroriy urinish

        self.assertEqual(len(client.posted("demand")), 1)
        self.assertEqual(len(client.posted("cashin")), 1)

    # ------------------------------------------------------ tekshirishlar

    def test_summa_mos_kelmasa_xato(self):
        sale = self.make_sale(
            [("Non", "1.000", 50_000_00)], [(self.cash, 50_000_00)]
        )
        client = FakeClient(sum_override=49_999_00)
        with self.assertRaises(SumMismatch):
            SaleWriter(client).send(sale)

    def test_tovarsiz_qator_xato(self):
        sale = self.make_sale(
            [("Non", "1.000", 50_000_00)], [(self.cash, 50_000_00)]
        )
        sale.items.update(ms_product_id=None)
        with self.assertRaises(WriteError):
            SaleWriter(FakeClient()).send(sale)

    def test_tashkilotsiz_nuqta_xato(self):
        self.store.organization_ms_id = None
        self.store.save()
        sale = self.make_sale(
            [("Non", "1.000", 50_000_00)], [(self.cash, 50_000_00)]
        )
        with self.assertRaises(WriteError):
            SaleWriter(FakeClient()).send(sale)

    def test_qaytarish_demand_emas_salesreturn_yozadi(self):
        sale = self.make_sale(
            [("Non", "1.000", 50_000_00)], [(self.cash, 50_000_00)]
        )
        sale.kind = Sale.RETURN
        sale.save()
        client = FakeClient()
        SaleWriter(client).send(sale)
        # Qaytarish Отгрузка emas, Возврат yozadi
        self.assertEqual(client.posted("demand"), [])
        self.assertEqual(len(client.posted("salesreturn")), 1)

    def test_dry_run_hech_narsa_yubormaydi(self):
        sale = self.make_sale(
            [("Non", "1.000", 50_000_00)], [(self.cash, 50_000_00)]
        )
        client = FakeClient()
        writer = SaleWriter(client, dry_run=True)
        writer.send(sale)

        self.assertEqual(client.posts, [])
        self.assertEqual(client.gets, [])
        self.assertEqual(len(writer.payloads), 2)  # demand + cashin

        sale.refresh_from_db()
        self.assertIsNone(sale.ms_demand_id)


@override_settings(MOYSKLAD_RETAIL_CUSTOMER_ID=RETAIL_CUSTOMER)
class ReturnTest(TestCase):
    """Qaytarish — Возврат (salesreturn) + pulni qaytarish."""

    def setUp(self):
        self.store = RetailStore.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000de",
            name="Namuna filiali",
            organization_ms_id="00000000-0000-0000-0000-0000000000a1",
            store_ms_id="00000000-0000-0000-0000-0000000000b2",
        )
        self.register = Register.objects.create(
            code="k1", name="Kassa-1", store=self.store
        )
        self.shift = Shift.objects.create(
            register=self.register, number=7, cashier="Test", opened_at=timezone.now()
        )
        self.cash = PaymentMethod.objects.create(
            code="naqd", name="Naqd", is_cash=True, sort=1
        )
        self.card = PaymentMethod.objects.create(
            code="terminal-1", name="Terminal-1", sort=2,
            ms_account_id="00000000-0000-0000-0000-0000000000f1",
        )

    def make_sale(self, kind=Sale.SALE, number=1, origin=None, ms_demand_id=None):
        sale = Sale.objects.create(
            shift=self.shift, kind=kind, number=number,
            created_at=timezone.now(),
            gross_total=50_000_00, net_total=50_000_00,
            origin=origin, ms_demand_id=ms_demand_id,
        )
        SaleItem.objects.create(
            sale=sale, position=1, name="Non",
            quantity=Decimal("1.000"), price=50_000_00, total=50_000_00,
            ms_product_id="00000000-0000-0000-0000-000000000101",
        )
        return sale

    def test_naqd_qaytarish_salesreturn_va_cashout(self):
        ret = self.make_sale(kind=Sale.RETURN)
        Payment.objects.create(sale=ret, method=self.cash, amount=50_000_00)

        client = FakeClient()
        SaleWriter(client).send(ret)

        self.assertEqual(len(client.posted("salesreturn")), 1)
        self.assertEqual(len(client.posted("cashout")), 1)
        # Отгрузка yozilmasligi kerak — bu qaytarish
        self.assertEqual(client.posted("demand"), [])
        self.assertEqual(client.posted("cashin"), [])

    def test_karta_qaytarish_paymentout(self):
        ret = self.make_sale(kind=Sale.RETURN)
        Payment.objects.create(sale=ret, method=self.card, amount=50_000_00)

        client = FakeClient()
        SaleWriter(client).send(ret)

        self.assertEqual(len(client.posted("paymentout")), 1)
        self.assertIn("organizationAccount", client.posted("paymentout")[0])

    def test_pul_qaytarish_salesreturnga_boglanadi(self):
        ret = self.make_sale(kind=Sale.RETURN)
        Payment.objects.create(sale=ret, method=self.cash, amount=50_000_00)

        client = FakeClient()
        SaleWriter(client).send(ret)

        op = client.posted("cashout")[0]["operations"][0]
        self.assertEqual(op["meta"]["type"], "salesreturn")
        self.assertEqual(op["linkedSum"], 50_000_00)

    def test_asl_chekka_boglanadi(self):
        origin = self.make_sale(kind=Sale.SALE, number=1,
                                ms_demand_id="99999999-9999-9999-9999-999999999999")
        ret = self.make_sale(kind=Sale.RETURN, number=1, origin=origin)
        Payment.objects.create(sale=ret, method=self.cash, amount=50_000_00)

        client = FakeClient()
        SaleWriter(client).send(ret)

        sr = client.posted("salesreturn")[0]
        self.assertEqual(sr["demand"]["meta"]["type"], "demand")
        self.assertIn("99999999", sr["demand"]["meta"]["href"])

    def test_asl_chek_yozilmagan_bolsa_boglanmaydi(self):
        origin = self.make_sale(kind=Sale.SALE, number=1)  # ms_demand_id yo'q
        ret = self.make_sale(kind=Sale.RETURN, number=1, origin=origin)
        Payment.objects.create(sale=ret, method=self.cash, amount=50_000_00)

        client = FakeClient()
        SaleWriter(client).send(ret)
        # Bog'lanish yo'q, lekin qaytarish baribir yoziladi
        self.assertNotIn("demand", client.posted("salesreturn")[0])

    def test_takroriy_qaytarish_ikki_marta_yozilmaydi(self):
        ret = self.make_sale(kind=Sale.RETURN)
        Payment.objects.create(sale=ret, method=self.cash, amount=50_000_00)

        client = FakeClient()
        writer = SaleWriter(client)
        writer.send(ret)
        writer.send(ret)

        self.assertEqual(len(client.posted("salesreturn")), 1)
        self.assertEqual(len(client.posted("cashout")), 1)
