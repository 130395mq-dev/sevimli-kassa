"""Regression coverage for the eight receipt audit failures."""
import uuid
from decimal import Decimal

from api import pricing
from api.tests import ApiTestCase
from catalog.models import Customer, Product
from sales.models import BonusProgram, CashOperation, Sale, Shift


class ReceiptIntegrityTest(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.open_shift()

    def sell(self, total=300000, points=0, quantity='1', customer=None):
        settings = self.register.settings
        settings.allow_discount = True
        settings.max_discount = 100
        settings.save()
        p = self.sale_payload(customer_id=customer.pk if customer else None, points_spent=points)
        p['items'][0].update(total=total, quantity=quantity)
        cash = total - points * 100
        p['payments'] = [{'method': 'naqd', 'amount': cash}] if cash else []
        response = self.post('/api/v1/sales', p)
        self.assertEqual(response.status_code, 201, response.content)
        return Sale.objects.get(pk=response.json()['id'])

    def refund(self, origin, amount=300000, quantity='1', **item_changes):
        p = self.sale_payload(kind='return', origin_id=origin.pk)
        p['items'][0].update(total=amount, quantity=quantity, **item_changes)
        p['payments'] = [{'method': 'naqd', 'amount': amount}] if amount else []
        return self.post('/api/v1/sales', p)

    def test_discounted_return_quote_and_acceptance(self):
        origin = self.sell(total=270000)
        item = self.client.get('/api/v1/sales/returnable', **self.auth()).json()['sales'][0]['items'][0]
        self.assertEqual(item['refund_total'], 270000)
        self.assertEqual(self.refund(origin, 300000).status_code, 400)
        self.assertEqual(self.refund(origin, 270000, origin_item_id=item['origin_item_id']).status_code, 201)
        self.assertEqual(Sale.objects.get(kind='return').items.get().origin_item_id, origin.items.get().pk)

    def test_excess_quantity_rejected_even_if_amount_is_equal(self):
        origin = self.sell()
        self.assertEqual(self.refund(origin, quantity='2', price=150000).status_code, 400)
        self.assertFalse(Sale.objects.filter(kind='return').exists())

    def test_unrelated_product_rejected(self):
        origin = self.sell()
        other = Product.objects.create(ms_id=uuid.uuid4(), name='Other', sale_price=300000)
        self.assertEqual(self.refund(origin, product_id=other.pk, ms_product_id=str(other.ms_id)).status_code, 400)

    def test_originless_return_setting_enforced(self):
        payload = self.sale_payload(kind='return')
        self.assertEqual(self.post('/api/v1/sales', payload).status_code, 400)
        settings = self.register.settings
        settings.allow_returns_no_reason = True
        settings.save()
        self.assertEqual(self.post('/api/v1/sales', payload).status_code, 201)

    def test_closed_shift_return_setting_enforced(self):
        origin = self.sell()
        self.post('/api/v1/shift/close', {})
        self.open_shift()
        self.assertEqual(self.refund(origin).status_code, 400)
        settings = self.register.settings
        settings.allow_returns_closed_shift = True
        settings.save()
        self.assertEqual(self.refund(origin).status_code, 201)

    def test_late_sale_rejects_forged_price(self):
        p = self.sale_payload(shift_id=Shift.objects.get().pk)
        p['items'][0].update(price=100, total=100)
        p['payments'] = [{'method': 'naqd', 'amount': 100}]
        self.post('/api/v1/shift/close', {})
        self.assertEqual(self.post('/api/v1/sales', p).status_code, 400)

    def test_late_sale_accepts_signed_historical_price(self):
        p = self.sale_payload(shift_id=Shift.objects.get().pk)
        p['items'][0]['price_quote'] = pricing.quote(self.product, self.register)
        self.post('/api/v1/shift/close', {})
        self.product.sale_price += 10000
        self.product.save()
        self.assertEqual(self.post('/api/v1/sales', p).status_code, 201)

    def test_cash_retry_after_shift_close_does_not_duplicate(self):
        auth = {'HTTP_X_SESSION': self.manager_token()}
        p = {'kind': 'out', 'amount': 1000000, 'local_uuid': str(uuid.uuid4())}
        first = self.post('/api/v1/cash', p, **auth)
        self.assertEqual(first.status_code, 201)
        self.post('/api/v1/shift/close', {})
        second = self.post('/api/v1/cash', p, **auth)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()['id'], second.json()['id'])
        self.assertEqual(CashOperation.objects.count(), 1)
        self.assertEqual(self.post('/api/v1/cash', {**p, 'amount': 2000000}, **auth).status_code, 409)

    def customer_with_bonus(self):
        program = BonusProgram.get()
        program.active = program.redeem_enabled = True
        program.max_redeem_percent = 100
        program.save()
        return Customer.objects.create(ms_id=uuid.uuid4(), name='Bonus', bonus_points=3000)

    def test_full_bonus_sale_and_return_restore_balance(self):
        customer = self.customer_with_bonus()
        origin = self.sell(points=3000, customer=customer)
        self.assertEqual(origin.net_total, 0)
        self.assertFalse(origin.payments.exists())
        customer.refresh_from_db()
        self.assertEqual(customer.bonus_points, 0)
        self.assertEqual(self.refund(origin, amount=0).status_code, 201)
        customer.refresh_from_db()
        self.assertEqual(customer.bonus_points, 3000)
        self.assertEqual(self.refund(origin, amount=0).status_code, 400)

    def test_partial_refund_rounding_preserves_exact_cash_and_points(self):
        customer = self.customer_with_bonus()
        origin = self.sell(total=800000, points=1, quantity='3', customer=customer)
        item = origin.items.get()
        for amount in (266633, 266634, 266633):
            result = self.refund(origin, amount=amount, origin_item_id=item.pk)
            self.assertEqual(result.status_code, 201, result.content)
        self.assertEqual(sum(Sale.objects.filter(kind='return').values_list('net_total', flat=True)), 799900)
        customer.refresh_from_db()
        self.assertEqual(customer.bonus_points, 3000)
        self.assertEqual(self.refund(origin, amount=266633, origin_item_id=item.pk).status_code, 400)

    def test_multiple_rows_cannot_return_same_unit_twice(self):
        origin = self.sell()
        p = self.sale_payload(kind='return', origin_id=origin.pk)
        p['items'] = [dict(p['items'][0]), dict(p['items'][0])]
        p['payments'] = [{'method': 'naqd', 'amount': 600000}]
        self.assertEqual(self.post('/api/v1/sales', p).status_code, 400)

    def test_search_finds_receipt_older_than_first_page(self):
        original = self.sell()
        for _ in range(41):
            self.sell()
        first = self.client.get('/api/v1/sales/returnable', **self.auth()).json()
        self.assertEqual(len(first['sales']), 40)
        self.assertEqual(first['next_offset'], 40)
        found = self.client.get('/api/v1/sales/returnable', {'q': str(original.pk)}, **self.auth()).json()
        self.assertIn(original.pk, [s['id'] for s in found['sales']])

    def test_receipt_number_matches_moysklad_document_and_retries(self):
        from unittest.mock import patch
        from shared.identity import receipt_number
        from sales.writer import SaleWriter
        from sales.test_writer import FakeClient
        payload = self.sale_payload()
        payload['receipt_number'] = receipt_number(payload['local_uuid'])
        response = self.post('/api/v1/sales', payload)
        self.assertEqual(response.status_code, 201, response.content)
        sale = Sale.objects.get(pk=response.json()['id'])
        client = FakeClient()
        with patch.object(SaleWriter, '_agent', return_value={'meta': {'href': 'test'}}):
            writer = SaleWriter(client)
            writer.send(sale)
            writer.send(sale)
        self.assertEqual(len(client.posted('demand')), 1)
        self.assertEqual(client.posted('demand')[0]['name'], payload['receipt_number'])
        self.assertEqual(self.post('/api/v1/sales', payload).json()['receipt_number'], payload['receipt_number'])

    def test_mismatched_receipt_number_is_rejected(self):
        response = self.post('/api/v1/sales', self.sale_payload(receipt_number='SK-FORGED'))
        self.assertEqual(response.status_code, 400)

    def test_local_queue_telemetry_is_distinct_from_server_queue(self):
        response = self.client.get('/api/v1/hello', {'local_pending': 1, 'local_stuck': 1,
                                   'local_error': 'Return rejected'}, **self.auth())
        self.assertEqual(response.status_code, 200)
        self.register.refresh_from_db()
        self.assertEqual(self.register.local_pending, 1)
        self.assertEqual(self.register.local_queue_error, 'Return rejected')
        self.assertEqual(response.json()['links']['moysklad']['pending'], 0)

    def test_invalid_local_queue_telemetry_does_not_change_counts(self):
        self.assertEqual(self.client.get('/api/v1/hello', {'local_pending': -1}, **self.auth()).status_code, 400)
        self.register.refresh_from_db()
        self.assertIsNone(self.register.local_pending)
