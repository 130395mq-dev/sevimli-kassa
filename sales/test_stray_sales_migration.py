"""0024 migratsiyasi: yopilgan smenaga tushib qolgan cheklarni qaytarish.

Migratsiyaning o'zi bir marta ishlaydi, lekin uning mantig'i pul hisobiga
tegadi — shuning uchun shu yerda sinaladi.
"""
from datetime import timedelta
from importlib import import_module

from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from catalog.models import RetailStore
from sales.models import Register, Sale, Shift

move_back = import_module(
    "sales.migrations.0024_stray_sales_to_their_shift"
).move_back


class StraySalesTest(TestCase):
    def setUp(self):
        store = RetailStore.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000de", name="Chilonzor"
        )
        self.reg = Register.objects.create(
            code="kassa-1", name="Kassa-1", store=store, login="kassa1"
        )
        day = timezone.now().replace(hour=8, minute=0, second=0, microsecond=0)
        self.day = day
        self.morning = Shift.objects.create(
            register=self.reg, number=1, cashier="Ertalabki",
            opened_at=day, closed_at=day + timedelta(hours=8),   # 16:00
            status=Shift.CLOSED, opening_cash=100_00,
        )
        self.evening = Shift.objects.create(
            register=self.reg, number=2, cashier="Kechki",
            opened_at=day + timedelta(hours=8, minutes=2),       # 16:02
            closed_at=day + timedelta(hours=15, minutes=30),     # 23:30
            status=Shift.CLOSED, opening_cash=100_00,
        )

    def sale(self, shift, hours, number, kind=Sale.SALE):
        return Sale.objects.create(
            shift=shift, kind=kind, number=number,
            created_at=self.day + timedelta(hours=hours),
            gross_total=10_000, discount_total=0, net_total=10_000,
            late=True, sync_status=Sale.SENT,
        )

    def run_migration(self):
        move_back(apps, None)

    def test_sale_made_after_close_moves_to_the_open_shift(self):
        stray = self.sale(self.morning, 11.5, 405)      # 19:30
        self.run_migration()
        stray.refresh_from_db()
        self.assertEqual(stray.shift_id, self.evening.pk)
        self.assertFalse(stray.late)
        self.assertEqual(stray.number, 405)

    def test_sale_made_during_the_shift_is_left_alone(self):
        kept = self.sale(self.morning, 3, 10)           # 11:00
        self.run_migration()
        kept.refresh_from_db()
        self.assertEqual(kept.shift_id, self.morning.pk)
        self.assertTrue(kept.late)

    def test_receipt_number_collision_is_renumbered(self):
        self.sale(self.evening, 9, 405)                 # 17:00, raqam band
        stray = self.sale(self.morning, 11.5, 405)
        self.run_migration()
        stray.refresh_from_db()
        self.assertEqual(stray.shift_id, self.evening.pk)
        self.assertEqual(stray.number, 406)

    def test_nothing_to_move_into_leaves_the_sale_in_place(self):
        late_night = self.sale(self.evening, 16, 300)   # 00:00 — ochiq smena yo'q
        self.run_migration()
        late_night.refresh_from_db()
        self.assertEqual(late_night.shift_id, self.evening.pk)

    def test_running_twice_changes_nothing(self):
        stray = self.sale(self.morning, 11.5, 405)
        self.run_migration()
        self.run_migration()
        stray.refresh_from_db()
        self.assertEqual(stray.shift_id, self.evening.pk)
        self.assertEqual(Sale.objects.count(), 1)
