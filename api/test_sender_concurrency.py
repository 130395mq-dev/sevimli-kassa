"""Real PostgreSQL sessions; outbound MoySklad calls are always fake."""
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.db import connection, connections
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from api.tests import ApiTestCase
from api.views import _push_sale_now, _push_sale_now_sync
from catalog.sync import CatalogSync
from sales.models import Sale
from sales.sender import send_one


@skipUnless(connection.vendor == "postgresql", "Requires separate PostgreSQL sessions")
class SenderConcurrencyTest(TransactionTestCase):
    setUp = ApiTestCase.setUp
    auth = ApiTestCase.auth
    post = ApiTestCase.post
    open_shift = ApiTestCase.open_shift
    sale_payload = ApiTestCase.sale_payload

    def sale(self):
        self.open_shift()
        with patch("api.views._push_sale_now", return_value=None):
            response = self.post("/api/v1/sales", self.sale_payload())
        self.assertEqual(response.status_code, 201, response.content)
        return Sale.objects.get(pk=response.json()["id"])

    @staticmethod
    def session(fn):
        try:
            return fn()
        finally:
            connections.close_all()

    def test_api_and_queue_never_write_together_even_after_lease_expires(self):
        sale = self.sale()
        entered, release = Event(), Event()
        def write(obj):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Test barrier timeout")
            Sale.objects.filter(pk=obj.pk).update(receipt_number="OT-test")
        writer = Mock()
        writer.send.side_effect = write
        with ThreadPoolExecutor(max_workers=1) as pool:
            with override_settings(MOYSKLAD_TOKEN="test"), \
                    patch("sales.writer.SaleWriter", return_value=writer):
                first = pool.submit(self.session, lambda: _push_sale_now_sync(sale.pk, 0))
                try:
                    self.assertTrue(entered.wait(5))
                    Sale.objects.filter(pk=sale.pk).update(next_attempt_at=timezone.now())
                    other = Mock()
                    self.assertEqual(send_one(other, sale, only_due=True), ("skipped", ""))
                    other.send.assert_not_called()
                finally:
                    release.set()
                self.assertEqual(first.result(10), "OT-test")
        stale = Sale.objects.get(pk=sale.pk)
        self.assertEqual(send_one(writer, stale, only_due=True), ("sent", ""))
        self.assertEqual(writer.send.call_count, 1)

    def test_queue_blocks_immediate_writer_and_releases_after_error(self):
        sale = self.sale()
        entered, release = Event(), Event()
        def fail(obj):
            entered.set()
            release.wait(10)
            raise RuntimeError("simulated network failure")
        writer = Mock()
        writer.send.side_effect = fail
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.session, lambda: send_one(writer, Sale.objects.get(pk=sale.pk)))
            try:
                self.assertTrue(entered.wait(5))
                with override_settings(MOYSKLAD_TOKEN="test"), patch("sales.writer.SaleWriter.send") as other:
                    self.assertIsNone(_push_sale_now_sync(sale.pk, 0))
                    other.assert_not_called()
            finally:
                release.set()
            self.assertEqual(first.result(10)[0], "failed")
        retry = Mock()
        self.assertEqual(send_one(retry, sale), ("sent", ""))
        retry.send.assert_called_once()
        sale.refresh_from_db()
        self.assertEqual(sale.sync_status, Sale.SENT)

    def test_catalog_parallel_worker_skips_without_advancing_cursor(self):
        entered, release = Event(), Event()
        def worker(state, full):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Test barrier timeout")
            return 7
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.session, lambda: CatalogSync(Mock())._run("assortment", False, worker))
            try:
                self.assertTrue(entered.wait(5))
                other = Mock()
                self.assertEqual(CatalogSync(Mock())._run("assortment", False, other), 0)
                other.assert_not_called()
            finally:
                release.set()
            self.assertEqual(first.result(10), 7)


@override_settings(MOYSKLAD_TOKEN="test")
class BoundedPushTest(SimpleTestCase):
    def test_slow_remote_does_not_hold_request_or_create_unbounded_workers(self):
        entered, release, done = Event(), Event(), Event()
        from threading import BoundedSemaphore
        slots = BoundedSemaphore(1)
        def slow(*args, **kw):
            entered.set()
            release.wait(10)
            return "OT-later"
        with patch("api.views._push_slots", slots), \
                patch("api.views._push_sale_now_sync", side_effect=slow) as write, \
                patch("django.db.connections.close_all", side_effect=done.set):
            try:
                started = time.monotonic()
                self.assertIsNone(_push_sale_now(1, wait=0.02))
                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(entered.wait(2))
                for _ in range(100):
                    self.assertIsNone(_push_sale_now(1, wait=0.02))
                self.assertEqual(write.call_count, 1)
            finally:
                release.set()
                self.assertTrue(done.wait(5))
                # The slot is released after connections are closed.
                self.assertTrue(slots.acquire(timeout=5))
                slots.release()

    def test_thread_start_failure_leaves_slot_available_for_next_receipt(self):
        from threading import BoundedSemaphore
        slots = BoundedSemaphore(1)
        with patch("api.views._push_slots", slots), \
                patch("api.views.threading.Thread.start", side_effect=RuntimeError("no thread")):
            self.assertIsNone(_push_sale_now(1))
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()
