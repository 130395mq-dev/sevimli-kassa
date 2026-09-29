"""Codex (28.09.2026) mustaqil regressiya testlari — pul qoidasi.

3-nashr (2026-09-28): kassa3 chek nusxasi uzun kasrli miqdor faqat noto'g'ri
o'qilgan 21… zavod kodidan chiqishini ko'rsatdi (Sevimli'da narxli yorliq
yo'q). Shuning uchun server uzun kasrni YANA rad etadi. Codex'ning uchta
«legacy» testi (uzun kasr qabul qilinadi deb kutgan) shu dalil sabab o'zgardi:
endi uzun kasr rad etilishi va qaytarish testlari 3 xonali asl chek bilan.
Pulni himoya qiluvchi 5 ta test (chegirma, oshirilgan summa, ortiqcha
qaytarish, narx, asl cheksiz qaytarish) o'zgarishsiz qoldi."""
from decimal import Decimal
from api.tests import ApiTestCase
from sales.models import Sale


class PriceNormalizationRegression(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.open_shift()

    def payload(self, quantity, total):
        data = self.sale_payload()
        data['items'][0].update(quantity=quantity, total=total)
        data['gross_total'] = total
        data['payments'] = [{'method': 'naqd', 'amount': total}]
        return data

    def test_extra_precision_cannot_hide_unauthorized_discount(self):
        # The actual normalization difference is below a tiyin; the imported
        # patch nevertheless waived half a gram (151 tiyin) of discount.
        response = self.post('/api/v1/sales', self.payload('0.00100001', 149))
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Sale.objects.exists())

    def test_normalization_cannot_authorize_an_inflated_line_total(self):
        response = self.post('/api/v1/sales', self.payload('0.33333333333333333333', 100_050))
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Sale.objects.exists())

    def test_legacy_long_fraction_is_rejected_not_rounded(self):
        response = self.post('/api/v1/sales', self.payload('0.33333333333333333333', 100_000))
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Sale.objects.exists())

    def test_gram_sale_gross_equals_net(self):
        response = self.post('/api/v1/sales', self.payload('0.333', 99_900))
        self.assertEqual(response.status_code, 201, response.content)
        sale = Sale.objects.get()
        self.assertEqual((sale.gross_total, sale.discount_total, sale.net_total), (99_900, 0, 99_900))
        self.assertEqual(sale.items.get().quantity, Decimal('0.333'))

    def make_legacy_sale(self):
        response = self.post('/api/v1/sales', self.payload('0.333', 99_900))
        self.assertEqual(response.status_code, 201, response.content)
        return Sale.objects.get(pk=response.json()['id'])

    def refund(self, origin, quantity, total, **changes):
        data = self.payload(quantity, total)
        data.update(kind='return', origin_id=origin.pk)
        data['items'][0].update(origin_item_id=origin.items.get().pk, **changes)
        return self.post('/api/v1/sales', data)

    def test_full_refund_of_gram_line_preserves_cash(self):
        origin = self.make_legacy_sale()
        response = self.refund(origin, '0.333', 99_900)
        self.assertEqual(response.status_code, 201, response.content)
        returned = Sale.objects.get(kind='return')
        self.assertEqual(returned.net_total, 99_900)
        self.assertGreaterEqual(returned.gross_total, returned.net_total)

    def test_partial_refunds_of_gram_line_sum_to_original_cash(self):
        origin = self.make_legacy_sale()
        first = self.refund(origin, '0.100', 30_000)
        self.assertEqual(first.status_code, 201, first.content)
        second = self.refund(origin, '0.233', 69_900)
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(sum(Sale.objects.filter(kind='return').values_list('net_total', flat=True)), 99_900)
        self.assertEqual(self.refund(origin, '0.001', 300).status_code, 400)

    def test_original_cash_limit_still_rejects_excess_refund(self):
        origin = self.make_legacy_sale()
        self.assertEqual(self.refund(origin, '0.333', 99_901).status_code, 400)
        self.assertFalse(Sale.objects.filter(kind='return').exists())

    def test_original_price_still_required_for_refund(self):
        origin = self.make_legacy_sale()
        self.assertEqual(self.refund(origin, '0.333', 99_900, price=299_999).status_code, 400)

    def test_unlinked_return_cannot_exceed_quantity_times_price(self):
        config = self.register.settings
        config.allow_returns_no_reason = True
        config.save()
        data = self.payload('0.333', 100_000)
        data['kind'] = 'return'
        self.assertEqual(self.post('/api/v1/sales', data).status_code, 400)
