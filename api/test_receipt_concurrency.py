"""PostgreSQL checks: separate sessions must not spend the same receipt twice."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.db import connection, close_old_connections
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
                close_old_connections()
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
