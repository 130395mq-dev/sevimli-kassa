"""O'z-o'zini davolash: zaxira yozuvchi, zaxira katalog, tiqilganlarni
sinov o'tganda qaytarish, sinovning tez-tez qaytarilishi.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from catalog.models import Product, RetailStore, SyncState
from sales import healer, sender
from sales.models import MoySkladCheck, Payment, PaymentMethod, Register, Sale, SaleItem, Shift
from sales.selftest import SelfTest, is_due
from sales.test_selftest import FakeMoySklad
from sales.writer import SaleWriter


def _ago(**kw):
    return timezone.now() - timedelta(**kw)


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        SaleWriter._retail_id_cache = None
        SaleWriter._expense_item_cache = None
        self.store = RetailStore.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000de", name="Chilonzor",
            organization_ms_id="00000000-0000-0000-0000-0000000000a1",
            store_ms_id="00000000-0000-0000-0000-0000000000b2",
        )
        self.reg = Register.objects.create(code="k1", name="Kassa-1", store=self.store)
        self.shift = Shift.objects.create(register=self.reg, opened_at=timezone.now(), number=1)
        self.cash = PaymentMethod.objects.create(code="naqd", name="Naqd", is_cash=True, sort=0)
        self.product = Product.objects.create(
            ms_id="00000000-0000-0000-0000-000000000101", name="Non", code="0001", sale_price=3_000_00,
        )

    def sale(self, n=1, status=Sale.NEW, minutes=0):
        s = Sale.objects.create(
            shift=self.shift, number=n, created_at=_ago(minutes=minutes),
            gross_total=3_000_00, net_total=3_000_00, sync_status=status,
        )
        SaleItem.objects.create(
            sale=s, position=1, name="Non", quantity=Decimal("1.000"), price=3_000_00,
            total=3_000_00, ms_product_id=self.product.ms_id,
        )
        Payment.objects.create(sale=s, method=self.cash, amount=3_000_00)
        return s


class WriterHeartbeatTest(_Base):
    def test_hech_qachon_korinmagan_none(self):
        self.assertIsNone(healer.writer_alive())

    def test_yaqinda_korinsa_tirik(self):
        healer.writer_heartbeat()
        self.assertTrue(healer.writer_alive())

    def test_3_daqiqa_jim_bolsa_olgan(self):
        SyncState.objects.create(entity=healer.WRITER_ENTITY, last_run_at=_ago(minutes=4))
        self.assertFalse(healer.writer_alive())


class FallbackWriterTest(_Base):
    """sales-sync jim → panel serveri cheklarni o'zi yozadi."""

    def test_yozuvchi_jim_va_navbat_bor_bolsa_ozi_yozadi(self):
        SyncState.objects.create(entity=healer.WRITER_ENTITY, last_run_at=_ago(minutes=10))
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=1))
        s = self.sale()
        ms = FakeMoySklad()
        with patch("sales.sender.MoySkladClient", lambda token: ms), self.settings(MOYSKLAD_TOKEN="x"):
            done = healer.heal()
        self.assertEqual(done.get("sent"), 1)
        s.refresh_from_db()
        self.assertEqual(s.sync_status, Sale.SENT)
        self.assertEqual(len(ms.posted("demand")), 1)

    def test_yozuvchi_tirik_bolsa_aralashmaydi(self):
        healer.writer_heartbeat()
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=1))
        s = self.sale()
        ms = FakeMoySklad()
        with patch("sales.sender.MoySkladClient", lambda token: ms), self.settings(MOYSKLAD_TOKEN="x"):
            done = healer.heal()
        self.assertNotIn("sent", done)
        s.refresh_from_db()
        self.assertEqual(s.sync_status, Sale.NEW)

    def test_yozuvchi_hali_hech_qachon_korinmagan_bolsa_aralashmaydi(self):
        """Yangi o'rnatma — sales-sync hali ishga tushmagan; bu «o'lgan» emas."""
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=1))
        self.sale()
        ms = FakeMoySklad()
        with patch("sales.sender.MoySkladClient", lambda token: ms), self.settings(MOYSKLAD_TOKEN="x"):
            done = healer.heal()
        self.assertNotIn("sent", done)

    def test_navbat_bosh_bolsa_hech_narsa(self):
        SyncState.objects.create(entity=healer.WRITER_ENTITY, last_run_at=_ago(minutes=10))
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=1))
        with self.settings(MOYSKLAD_TOKEN="x"):
            done = healer.heal()
        self.assertEqual(done, {})

    def test_xato_bolsa_backoff_bilan_belgilanadi(self):
        SyncState.objects.create(entity=healer.WRITER_ENTITY, last_run_at=_ago(minutes=10))
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=1))
        s = self.sale()
        ms = FakeMoySklad(reject={"demand": "ombor yo'q"})
        with patch("sales.sender.MoySkladClient", lambda token: ms), self.settings(MOYSKLAD_TOKEN="x"):
            done = healer.heal()
        self.assertEqual(done.get("failed"), 1)
        s.refresh_from_db()
        self.assertEqual(s.sync_status, Sale.FAILED)
        self.assertIsNotNone(s.next_attempt_at)

    def test_tick_60_soniyada_bir_marta(self):
        with self.settings(MOYSKLAD_TOKEN="x"), patch("sales.healer.heal", return_value={}) as h:
            self.assertTrue(healer.tick(background=False))
            self.assertFalse(healer.tick(background=False))
            self.assertEqual(h.call_count, 1)

    def test_tokensiz_tick_ishlamaydi(self):
        with self.settings(MOYSKLAD_TOKEN=""):
            self.assertFalse(healer.tick(background=False))


class FallbackCatalogTest(_Base):
    def test_katalog_eskirsa_ozi_tortadi(self):
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=40))

        class FakeSync:
            calls = []

            def __init__(self, client):
                pass

            def sync_products(self):
                FakeSync.calls.append("products"); return 5

            def sync_stock(self):
                FakeSync.calls.append("stock"); return 7

        with patch("catalog.sync.CatalogSync", FakeSync), self.settings(MOYSKLAD_TOKEN="x"):
            done = healer.heal()
            done2 = healer.heal()
        self.assertEqual(done.get("catalog"), {"products": 5, "stock": 7})
        self.assertNotIn("catalog", done2, "15 daqiqada bir martadan ko'p emas")

    def test_katalog_yangi_bolsa_tegmaydi(self):
        SyncState.objects.create(entity="assortment", last_success_at=_ago(minutes=2))
        with patch("catalog.sync.CatalogSync") as FakeSync, self.settings(MOYSKLAD_TOKEN="x"):
            done = healer.heal()
        self.assertNotIn("catalog", done)
        FakeSync.assert_not_called()


class SelfTestHealsStuckTest(_Base):
    """Sinov o'tdi → tiqilgan cheklar o'zi navbatga qaytadi."""

    def test_sinov_otsa_tiqilganlar_navbatga_qaytadi(self):
        s = self.sale(status=Sale.STUCK)
        s.sync_attempts = 12
        s.save()
        check = SelfTest(FakeMoySklad()).run()
        self.assertTrue(check.ok)
        s.refresh_from_db()
        self.assertEqual(s.sync_status, Sale.NEW)
        self.assertEqual(s.sync_attempts, 0)
        self.assertTrue(any(st["name"] == "Tiqilgan cheklar" for st in check.steps))

    def test_sinov_otmasa_tiqilganlar_joyida_qoladi(self):
        s = self.sale(status=Sale.STUCK)
        check = SelfTest(FakeMoySklad(reject={"cashout": "expenseItem"})).run()
        self.assertFalse(check.ok)
        s.refresh_from_db()
        self.assertEqual(s.sync_status, Sale.STUCK)

    def test_sinov_otmagan_bolsa_30_daqiqada_qayta(self):
        SelfTest(FakeMoySklad(reject={"cashout": "expenseItem"})).run()
        self.assertFalse(is_due())
        self.assertTrue(is_due(now=timezone.now() + timedelta(minutes=31)))

    def test_sinov_otgan_bolsa_3_soatda_qayta(self):
        SelfTest(FakeMoySklad()).run()
        self.assertFalse(is_due(now=timezone.now() + timedelta(minutes=31)))
        self.assertTrue(is_due(now=timezone.now() + timedelta(hours=3, minutes=1)))


class SenderTest(_Base):
    def test_backoff_va_stuck(self):
        s = self.sale()
        with self.settings(SYNC_MAX_ATTEMPTS=2):
            sender.mark_failed(s, "x")
            self.assertEqual(s.sync_status, Sale.FAILED)
            self.assertEqual(s.sync_attempts, 1)
            sender.mark_failed(s, "x")
            self.assertEqual(s.sync_status, Sale.STUCK)

    def test_due_queue_kutish_muddati(self):
        s = self.sale()
        s.sync_status = Sale.FAILED
        s.next_attempt_at = timezone.now() + timedelta(minutes=5)
        s.save()
        self.assertEqual(sender.due_queue(), [])
        self.assertFalse(sender.due_exists())
        s.next_attempt_at = _ago(minutes=1)
        s.save()
        self.assertEqual(len(sender.due_queue()), 1)

    def test_requeue_stuck(self):
        self.sale(n=1, status=Sale.STUCK)
        self.sale(n=2, status=Sale.SENT)
        self.assertEqual(sender.requeue_stuck(), 1)
        self.assertEqual(Sale.objects.filter(sync_status=Sale.NEW).count(), 1)
