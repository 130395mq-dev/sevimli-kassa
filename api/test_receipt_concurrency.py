"""PostgreSQL checks: separate sessions must not spend the same receipt twice."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.db import connection, connections, close_old_connections
from django.test import Client, TransactionTestCase

from api.tests import ApiTestCase
from sales.models import CashOperation, Sale, Shift, Register, PaymentMethod
from catalog.models import RetailStore, Product


@skipUnless(connection.vendor == 'postgresql', 'Row-lock checks require PostgreSQL')
class ReceiptConcurrencyTest(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.store = RetailStore.objects.create(ms_id=uuid.uuid4(), name='Audit',
            organization_ms_id=uuid.uuid4(), store_ms_id=uuid.uuid4())
        self.register = Register.objects.create(code='audit', name='Audit', store=self.store)
        self.cash = PaymentMethod.objects.create(code='naqd', name='Naqd', is_cash=True)
        self.product = Product.objects.create(ms_id=uuid.uuid4(), name='Non', sale_price=300000)

    auth = ApiTestCase.auth
    post = ApiTestCase.post
    manager_token = ApiTestCase.manager_token
    open_shift = ApiTestCase.open_shift
    sale_payload = ApiTestCase.sale_payload

    def race(self, path, payloads, extra=None):
        import json
        barrier = Barrier(2)
        headers = {**self.auth(), **(extra or {})}
        def send(payload):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = Client().post(path, json.dumps(payload), content_type='application/json', **headers)
                return response.status_code
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return sorted(pool.map(send, payloads))

    def test_two_shifts_cannot_refund_same_original_quantity(self):
        self.open_shift()
        origin_id = self.post('/api/v1/sales', self.sale_payload()).json()['id']
        old = Shift.objects.get()
        self.post('/api/v1/shift/close', {})
        self.open_shift()
        new = Shift.objects.get(status=Shift.OPEN)
        settings = self.register.settings
        settings.allow_returns_closed_shift = True
        settings.save()
        payloads = [self.sale_payload(kind='return', origin_id=origin_id, shift_id=s.pk) for s in (old, new)]
        self.assertEqual(self.race('/api/v1/sales', payloads), [201, 400])
        self.assertEqual(Sale.objects.filter(kind=Sale.RETURN).count(), 1)

    def test_simultaneous_cash_retries_create_one_operation(self):
        self.open_shift()
        token = self.manager_token()
        payload = {'kind': 'out', 'amount': 1000000, 'local_uuid': str(uuid.uuid4())}
        self.assertEqual(self.race('/api/v1/cash', [payload, payload], {'HTTP_X_SESSION': token}), [200, 201])
        self.assertEqual(CashOperation.objects.count(), 1)


@skipUnless(connection.vendor == 'postgresql', 'Row-lock checks require PostgreSQL')
class BonusConcurrencyTest(TransactionTestCase):
    """V05 (audit 2026-09-28): bir mijozning balli ikki kassada BIR VAQTDA sarflanmasin."""

    def setUp(self):
        super().setUp()
        self.store = RetailStore.objects.create(ms_id=uuid.uuid4(), name='Audit',
            organization_ms_id=uuid.uuid4(), store_ms_id=uuid.uuid4())
        self.register = Register.objects.create(code='audit', name='Audit', store=self.store)
        self.cash = PaymentMethod.objects.create(code='naqd', name='Naqd', is_cash=True)
        self.product = Product.objects.create(ms_id=uuid.uuid4(), name='Non', sale_price=300000)

    auth = ApiTestCase.auth
    post = ApiTestCase.post
    open_shift = ApiTestCase.open_shift
    sale_payload = ApiTestCase.sale_payload
    race = ReceiptConcurrencyTest.race

    def _customer(self, points):
        from catalog.models import Customer
        from sales.models import BonusProgram
        prog = BonusProgram.get()
        prog.active = True
        prog.redeem_enabled = True
        prog.max_redeem_percent = 100
        prog.save()
        return Customer.objects.create(ms_id=uuid.uuid4(), name='Parallel', bonus_points=points)

    def _points_sale(self, customer, points):
        p = self.sale_payload(customer_id=customer.pk, points_spent=points)
        p['payments'] = [{'method': 'naqd', 'amount': 300000 - points * 100}]
        return p

    def test_parallel_ball_sarfi_balansdan_oshmaydi(self):
        self.open_shift()
        cust = self._customer(1000)
        # Ikkalasi 700 tadan: birinchisi o'tadi, ikkinchisi «yetarli ball yo'q»
        codes = self.race('/api/v1/sales', [self._points_sale(cust, 700), self._points_sale(cust, 700)])
        self.assertEqual(codes, [201, 400])
        cust.refresh_from_db()
        self.assertEqual(cust.bonus_points, 1000 - 700 + Sale.objects.get(kind=Sale.SALE).points_earned)
        self.assertEqual(Sale.objects.count(), 1)

    def test_parallel_ikki_kichik_sarf_ikkalasi_otadi_balans_togri(self):
        self.open_shift()
        cust = self._customer(1000)
        codes = self.race('/api/v1/sales', [self._points_sale(cust, 400), self._points_sale(cust, 400)])
        self.assertEqual(codes, [201, 201])
        cust.refresh_from_db()
        earned = sum(Sale.objects.values_list('points_earned', flat=True))
        self.assertEqual(cust.bonus_points, 1000 - 800 + earned)
