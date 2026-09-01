"""
Smena yakuni testlari.

Bu testlar pulni tekshiradi. Shuning uchun raqamlar qo'lda hisoblab
yozilgan — dastur nima chiqarsa, shu emas, balki nima chiqishi kerak
bo'lsa, shu.

Ishga tushirish:  python manage.py test sales
"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from catalog.models import RetailStore
from shared.receipt import render

from .models import CashOperation, Payment, PaymentMethod, Register, Sale, SaleItem, Shift
from .services import ShiftError, build_receipt, close_shift, pending_count


class ShiftReceiptTest(TestCase):
    def setUp(self):
        self.store = RetailStore.objects.create(
            ms_id="11111111-1111-1111-1111-111111111111", name="Chilonzor filiali"
        )
        self.register = Register.objects.create(
            code="kassa-2", name="Kassa-2", store=self.store
        )
        self.cash = PaymentMethod.objects.create(
            code="naqd", name="Naqd", is_cash=True, sort=1
        )
        self.term = PaymentMethod.objects.create(code="terminal-1", name="Terminal-1", sort=2)
        self.click = PaymentMethod.objects.create(code="click", name="Click", sort=3)

        now = timezone.now()
        self.shift = Shift.objects.create(
            register=self.register,
            number=142,
            cashier="Rahimova Nilufar",
            opened_at=now - timedelta(hours=14),
            opening_cash=30_000_00,  # 30 000 so'm
        )

    def add_sale(self, number, gross, discount=0, points_spent=0, points_earned=0,
                 pays=(), kind=Sale.SALE):
        net = gross - discount - points_spent * 100
        sale = Sale.objects.create(
            shift=self.shift,
            kind=kind,
            number=number,
            created_at=timezone.now(),
            gross_total=gross,
            discount_total=discount,
            points_spent=points_spent,
            points_earned=points_earned,
            net_total=net,
        )
        SaleItem.objects.create(
            sale=sale, position=1, name="Buhanka S",
            quantity=Decimal("1.000"), price=gross, total=net,
        )
        for method, amount in pays:
            Payment.objects.create(sale=sale, method=method, amount=amount)
        return sale

    # ---------------------------------------------------------------

    def test_yigindi_togri(self):
        """Uchta chek: summalar va to'lov turlari to'g'ri yig'ilishi kerak."""
        self.add_sale(1, gross=300_000_00, pays=[(self.cash, 300_000_00)])
        self.add_sale(2, gross=200_000_00, discount=20_000_00,
                      pays=[(self.term, 180_000_00)])
        self.add_sale(3, gross=100_000_00, points_spent=5_000,
                      pays=[(self.click, 95_000_00)])

        r = build_receipt(self.shift)

        self.assertEqual(r.receipts_count, 3)
        self.assertEqual(r.gross_total, 600_000_00)
        self.assertEqual(r.discount_total, 20_000_00)
        self.assertEqual(r.paid_by_points, 5_000_00)
        # 600 000 - 20 000 - 5 000
        self.assertEqual(r.net_total, 575_000_00)

        self.assertEqual(r.cash_total, 300_000_00)
        self.assertEqual(r.cashless_total, 275_000_00)
        self.assertEqual(r.payments_total, r.net_total)
        self.assertTrue(r.is_balanced)

    def test_aralash_tolov(self):
        """Bitta chek ikki xil to'lov bilan — ikkalasi ham hisobga kirsin."""
        self.add_sale(1, gross=100_000_00,
                      pays=[(self.cash, 40_000_00), (self.term, 60_000_00)])

        r = build_receipt(self.shift)
        self.assertEqual(r.cash_total, 40_000_00)
        self.assertEqual(r.cashless_total, 60_000_00)
        self.assertTrue(r.is_balanced)

    def test_qaytarish_savdodan_ayriladi(self):
        """Qaytarish savdo yig'indisiga qo'shilmasligi, naqdni kamaytirishi kerak."""
        self.add_sale(1, gross=500_000_00, pays=[(self.cash, 500_000_00)])
        self.add_sale(1, gross=84_000_00, kind=Sale.RETURN,
                      pays=[(self.cash, 84_000_00)])

        r = build_receipt(self.shift)

        # Qaytarish sof savdoga kirmaydi
        self.assertEqual(r.receipts_count, 1)
        self.assertEqual(r.net_total, 500_000_00)
        self.assertEqual(r.cash_total, 500_000_00)
        # Lekin kassadan chiqadi
        self.assertEqual(r.returns_count, 1)
        self.assertEqual(r.returns_cash, 84_000_00)
        # 30 000 + 500 000 - 84 000
        self.assertEqual(r.expected_cash, 446_000_00)

    def test_kassa_operatsiyalari(self):
        self.add_sale(1, gross=800_000_00, pays=[(self.cash, 800_000_00)])
        CashOperation.objects.create(
            shift=self.shift, kind=CashOperation.IN, amount=20_000_00
        )
        CashOperation.objects.create(
            shift=self.shift, kind=CashOperation.OUT, amount=700_000_00,
            comment="Inkassatsiya",
        )

        r = build_receipt(self.shift)
        # 30 000 + 800 000 + 20 000 - 700 000
        self.assertEqual(r.expected_cash, 150_000_00)

    def test_kam_sanalgan_pul_chekda_korinadi(self):
        self.add_sale(1, gross=100_000_00, pays=[(self.cash, 100_000_00)])
        r = close_shift(self.shift, counted_cash=125_000_00)

        # 30 000 + 100 000 = 130 000, sanalgani 125 000
        self.assertEqual(r.expected_cash, 130_000_00)
        self.assertEqual(r.cash_diff, -5_000_00)

        text = render(r)
        self.assertIn("FARQ", text)
        self.assertIn("-5 000", text)
        self.assertIn("<<<", text)

    def test_smena_yopiladi_va_ikki_marta_yopilmaydi(self):
        self.add_sale(1, gross=100_000_00, pays=[(self.cash, 100_000_00)])
        close_shift(self.shift, counted_cash=130_000_00)

        self.shift.refresh_from_db()
        self.assertEqual(self.shift.status, Shift.CLOSED)
        self.assertIsNotNone(self.shift.closed_at)

        with self.assertRaises(ShiftError):
            close_shift(self.shift)

    def test_yuborilmagan_chek_yopishga_tosqinlik_qilmaydi(self):
        """Internet yo'q bo'lsa ham kassir smenani yopa olishi kerak."""
        self.add_sale(1, gross=100_000_00, pays=[(self.cash, 100_000_00)])
        self.assertEqual(pending_count(self.shift), 1)

        r = close_shift(self.shift, counted_cash=130_000_00)
        self.assertEqual(r.net_total, 100_000_00)
        self.assertEqual(pending_count(self.shift), 1)

    def test_bosh_smena_chekni_buzmaydi(self):
        """Bitta ham savdo bo'lmasa — chek baribir chiqishi kerak."""
        r = close_shift(self.shift, counted_cash=30_000_00)
        self.assertEqual(r.receipts_count, 0)
        self.assertEqual(r.net_total, 0)
        self.assertEqual(r.cash_diff, 0)

        text = render(r)
        self.assertIn("BUGUNGI SAVDO", text)
        self.assertIn("0 so'm", text)

    def test_chek_kengligi(self):
        self.add_sale(1, gross=100_000_00, pays=[(self.cash, 100_000_00)])
        r = build_receipt(self.shift)
        for width in (48, 32):
            for line in render(r, width).split("\n"):
                self.assertLessEqual(len(line), width)


class SaleBalanceTest(TestCase):
    """Chekning o'zi muvozanatda ekanini bilishi kerak."""

    def setUp(self):
        store = RetailStore.objects.create(
            ms_id="22222222-2222-2222-2222-222222222222", name="Test"
        )
        reg = Register.objects.create(code="k1", name="K1", store=store)
        self.shift = Shift.objects.create(
            register=reg, number=1, cashier="Test", opened_at=timezone.now()
        )
        self.cash = PaymentMethod.objects.create(code="n", name="Naqd", is_cash=True)

    def test_notogri_tolov_aniqlanadi(self):
        sale = Sale.objects.create(
            shift=self.shift, number=1, created_at=timezone.now(),
            gross_total=100_00, net_total=100_00,
        )
        Payment.objects.create(sale=sale, method=self.cash, amount=90_00)
        self.assertFalse(sale.is_balanced)

        Payment.objects.create(sale=sale, method=self.cash, amount=10_00)
        self.assertTrue(sale.is_balanced)
