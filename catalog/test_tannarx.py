"""Tannarx (MoySklad buyPrice) — KPI «narx nazorati» uchun katalogda saqlanadi.

Muhim: tannarx kassaga kerak emas — u o'zgarganda `synced_at` yangilanmasligi
kerak, aks holda 22 ming tovar hamma kassaga qayta tushardi."""

from unittest.mock import MagicMock

from django.test import Client, TestCase

from catalog.models import Product
from catalog.sync import CatalogSync
from sales.models import Register

UZS = {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/currency/uzs"}}
USD = {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/currency/usd"}}
P = "00000000-0000-0000-0000-000000000401"
P2 = "00000000-0000-0000-0000-000000000402"


def row(pid=P, sale=12_000_00, buy=9_000_00, buy_currency=None, sale_currency=None):
    r = {
        "id": pid, "meta": {"type": "product"}, "name": "Sut 1L", "code": "S77",
        "salePrices": [{"value": sale, "priceType": {"id": "pt1", "name": "Чакана нарх"}}],
        "barcodes": [{"ean13": "4780001000017"}], "packs": [],
        "updated": "2026-11-10 10:00:00.000",
    }
    if buy is not None:
        r["buyPrice"] = {"value": buy}
        if buy_currency:
            r["buyPrice"]["currency"] = buy_currency
    if sale_currency:
        r["salePrices"][0]["currency"] = sale_currency
    return r


def sync_products(*rows):
    client = MagicMock()
    client.iter_list.return_value = iter(rows)
    CatalogSync(client).sync_products(full=True)


class TannarxSyncTest(TestCase):
    def test_yangi_tovarda_tannarx_yoziladi(self):
        sync_products(row())
        p = Product.objects.get(ms_id=P)
        self.assertEqual((p.sale_price, p.buy_price), (12_000_00, 9_000_00))

    def test_faqat_tannarx_ozgarsa_kassaga_qayta_tushmaydi(self):
        sync_products(row())
        before = Product.objects.get(ms_id=P).synced_at
        sync_products(row(buy=9_500_00))
        p = Product.objects.get(ms_id=P)
        self.assertEqual(p.buy_price, 9_500_00)
        self.assertEqual(p.synced_at, before)          # synced_at o'zgarmadi

    def test_narx_bilan_birga_ozgarsa_ikkalasi_yoziladi(self):
        sync_products(row())
        sync_products(row(sale=13_000_00, buy=10_000_00))
        p = Product.objects.get(ms_id=P)
        self.assertEqual((p.sale_price, p.buy_price), (13_000_00, 10_000_00))

    def test_tannarx_yoq_yoki_xato_bolsa_nol(self):
        sync_products(row(buy=None))
        self.assertEqual(Product.objects.get(ms_id=P).buy_price, 0)
        self.assertEqual(CatalogSync._buy_price({"buyPrice": {"value": "abc"}}), 0)
        self.assertEqual(CatalogSync._buy_price({"buyPrice": {"value": -5}}), 0)
        self.assertEqual(CatalogSync._buy_price({"buyPrice": {"value": 1234.6}}), 1235)

    def test_boshqa_valyutadagi_tannarx_solishtirilmaydi(self):
        # Tannarx dollarda, chakana narx so'mda — solishtirib bo'lmaydi → 0
        sync_products(row(buy=120, buy_currency=USD, sale_currency=UZS))
        self.assertEqual(Product.objects.get(ms_id=P).buy_price, 0)
        # Bir xil valyuta — yoziladi; valyuta ko'rsatilmagan bo'lsa ham
        sync_products(row(buy=9_000_00, buy_currency=UZS, sale_currency=UZS))
        self.assertEqual(Product.objects.get(ms_id=P).buy_price, 9_000_00)
        sync_products(row(buy=8_000_00, buy_currency=UZS))
        self.assertEqual(Product.objects.get(ms_id=P).buy_price, 8_000_00)

    def test_kassa_katalogida_tannarx_yoq(self):
        sync_products(row())
        reg = Register.objects.create(code="k1", name="Kassa-1")
        r = Client().get("/api/v1/catalog", HTTP_AUTHORIZATION="Bearer " + reg.api_token)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertNotIn("buy_price", r.content.decode())
        self.assertNotIn("900000", r.content.decode())


class TannarxReconcileTest(TestCase):
    """To'liq ro'yxat (reconcile) hamma tovarning tannarxini yangilaydi —
    delta sync faqat o'zgargan tovarni ko'radi."""

    def _reconcile(self, *rows):
        client = MagicMock()
        client.iter_list.return_value = iter(rows)
        return CatalogSync(client).reconcile_deleted()

    def test_hamma_tovar_tannarxi_yangilanadi_synced_at_tegilmaydi(self):
        a = Product.objects.create(ms_id=P, name="A", sale_price=12_000_00)
        b = Product.objects.create(ms_id=P2, name="B", sale_price=5_000_00, buy_price=4_000_00)
        sa, sb = a.synced_at, b.synced_at
        archived = self._reconcile(row(P, buy=9_000_00), row(P2, sale=5_000_00, buy=4_000_00))
        self.assertEqual(archived, 0)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.buy_price, b.buy_price), (9_000_00, 4_000_00))
        self.assertEqual((a.synced_at, b.synced_at), (sa, sb))      # kassalarga qayta tushmaydi
        self.assertFalse(a.archived or b.archived)

    def test_ozgarmagan_bolsa_yozilmaydi(self):
        Product.objects.create(ms_id=P, name="A", buy_price=9_000_00)
        self.assertEqual(CatalogSync._refresh_buy_prices({P: 9_000_00}), 0)
        self.assertEqual(CatalogSync._refresh_buy_prices({P: 9_100_00}), 1)
        self.assertEqual(CatalogSync._refresh_buy_prices({}), 0)
        self.assertEqual(Product.objects.get(ms_id=P).buy_price, 9_100_00)

    def test_ochirilgan_tovar_arxivlanadi_qolgani_yangilanadi(self):
        a = Product.objects.create(ms_id=P, name="A")
        b = Product.objects.create(ms_id=P2, name="B")
        self.assertEqual(self._reconcile(row(P, buy=7_000_00)), 1)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.buy_price, 7_000_00)
        self.assertTrue(b.archived)
