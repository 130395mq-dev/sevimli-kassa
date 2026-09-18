"""Katalog sinxronizatsiyasi testlari."""

from django.test import TestCase


class RetailPriceTest(TestCase):
    """Kassa chakana narxda sotadi — salePrices'dagi birinchisi emas."""

    ROW = {
        "salePrices": [
            {"value": 5200000, "priceType": {"id": "aaaa-ulgurji", "name": "Улугржи нархи"}},
            {"value": 5500000, "priceType": {"id": "bbbb-chakana", "name": "Чакана нарх"}},
        ]
    }

    def test_nuqta_narx_turi_id_boyicha(self):
        from catalog.sync import CatalogSync

        price = CatalogSync._retail_price(self.ROW, ({"bbbb-chakana"}, set()))
        self.assertEqual(price, 5500000)

    def test_nuqta_narx_turi_nomi_boyicha(self):
        from catalog.sync import CatalogSync

        price = CatalogSync._retail_price(self.ROW, (set(), {"чакана нарх"}))
        self.assertEqual(price, 5500000)

    def test_nuqta_malum_bolmasa_chakana_sozi_boyicha(self):
        from catalog.sync import CatalogSync

        self.assertEqual(CatalogSync._retail_price(self.ROW, (set(), set())), 5500000)

    def test_hech_narsa_mos_kelmasa_birinchisi(self):
        from catalog.sync import CatalogSync

        row = {"salePrices": [{"value": 100, "priceType": {"name": "A"}},
                              {"value": 200, "priceType": {"name": "B"}}]}
        self.assertEqual(CatalogSync._retail_price(row, (set(), set())), 100)

    def test_savdo_nuqtasidan_narx_turi_oqiladi(self):
        from unittest.mock import MagicMock

        from catalog.models import RetailStore
        from catalog.sync import CatalogSync

        client = MagicMock()
        client.iter_list.return_value = iter([{
            "id": "00000000-0000-0000-0000-0000000000de",
            "name": "Shaxar 1",
            "priceType": {
                "meta": {"href": "https://api.moysklad.ru/api/remap/1.2/context/companysettings/pricetype/00000000-0000-0000-0000-00000000bbbb"},
                "id": "00000000-0000-0000-0000-00000000bbbb",
                "name": "Чакана нарх",
            },
        }])
        CatalogSync(client).sync_retail_stores()
        st = RetailStore.objects.get()
        self.assertEqual(str(st.price_type_ms_id), "00000000-0000-0000-0000-00000000bbbb")
        self.assertEqual(st.price_type_name, "Чакана нарх")
        ids, names = CatalogSync._preferred_price_types()
        self.assertIn("00000000-0000-0000-0000-00000000bbbb", ids)
        self.assertIn("чакана нарх", names)


class WarehouseTest(TestCase):
    """Filial → ombor bog'lanishi: MoySklad'dan olinadi, qo'lda ustun turadi."""

    MS = "00000000-0000-0000-0000-00000000aaaa"
    QOL = "00000000-0000-0000-0000-00000000bbbb"

    def setUp(self):
        from catalog.models import RetailStore, Warehouse

        Warehouse.objects.create(ms_id=self.MS, name="Shaxar 1 ombori")
        Warehouse.objects.create(ms_id=self.QOL, name="Markaziy ombor")
        self.store = RetailStore.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000de", name="Shaxar 1",
            store_ms_id=self.MS,
            organization_ms_id="00000000-0000-0000-0000-0000000000a1",
        )

    def test_moyskladdagi_ombor_olinadi(self):
        self.assertEqual(str(self.store.warehouse_ms_id), self.MS)
        self.assertEqual(self.store.warehouse_name, "Shaxar 1 ombori")

    def test_qolda_tanlangan_ustun(self):
        self.store.manual_warehouse_ms_id = self.QOL
        self.store.save()
        self.assertEqual(str(self.store.warehouse_ms_id), self.QOL)
        self.assertEqual(self.store.warehouse_name, "Markaziy ombor")

    def test_sinxronizatsiya_qolda_tanlanganni_buzmaydi(self):
        """MoySklad savdo nuqtasini qayta tortsa ham, tanlovimiz qoladi."""
        from unittest.mock import MagicMock

        from catalog.sync import CatalogSync

        self.store.manual_warehouse_ms_id = self.QOL
        self.store.save()

        client = MagicMock()
        client.iter_list.return_value = iter([{
            "id": "00000000-0000-0000-0000-0000000000de",
            "name": "Shaxar 1",
            "store": {"meta": {"href": f"https://x/entity/store/{self.MS}"}},
        }])
        CatalogSync(client).sync_retail_stores()

        self.store.refresh_from_db()
        self.assertEqual(str(self.store.store_ms_id), self.MS)      # MoySklad'niki
        self.assertEqual(str(self.store.warehouse_ms_id), self.QOL)  # bizniki ustun

    def test_ombor_yoq_bolsa_bosh(self):
        self.store.store_ms_id = None
        self.store.save()
        self.assertIsNone(self.store.warehouse_ms_id)
        self.assertEqual(self.store.warehouse_name, "")

    def test_omborlar_moyskladdan_tortiladi(self):
        from unittest.mock import MagicMock

        from catalog.models import Warehouse
        from catalog.sync import CatalogSync

        client = MagicMock()
        client.iter_list.return_value = iter([
            {"id": "00000000-0000-0000-0000-00000000cccc", "name": "Yangi ombor",
             "pathName": "Filiallar"},
        ])
        self.assertEqual(CatalogSync(client).sync_warehouses(), 1)
        wh = Warehouse.objects.get(ms_id="00000000-0000-0000-0000-00000000cccc")
        self.assertEqual(wh.name, "Yangi ombor")
        self.assertEqual(wh.path_name, "Filiallar")


class StockDeltaTest(TestCase):
    """Kirim (приёмка) bo'lganda kassa yangi qoldiqni OLISHI kerak.

    Kassa `catalog?since=` bilan faqat `Product.synced_at` o'zgargan
    tovarlarni tortadi. Qoldiq alohida jadvalda — u o'zgarganda tovarning
    `synced_at` i ham yangilanmasa kassa «omborda yo'q» deb turaveradi
    (2026-09-14 da aynan shu bo'ldi).
    """

    STORE = "00000000-0000-0000-0000-0000000000b2"
    P1 = "00000000-0000-0000-0000-000000000101"
    P2 = "00000000-0000-0000-0000-000000000102"

    def _client(self, stock1, stock2):
        from unittest.mock import MagicMock
        base = "https://api.moysklad.ru/api/remap/1.2/entity"
        client = MagicMock()
        client.iter_list.return_value = iter([
            {"meta": {"href": f"{base}/product/{self.P1}"},
             "stockByStore": [{"meta": {"href": f"{base}/store/{self.STORE}"}, "stock": stock1}]},
            {"meta": {"href": f"{base}/product/{self.P2}"},
             "stockByStore": [{"meta": {"href": f"{base}/store/{self.STORE}"}, "stock": stock2}]},
        ])
        return client

    def setUp(self):
        from catalog.models import Product
        self.p1 = Product.objects.create(ms_id=self.P1, name="Non", sale_price=100)
        self.p2 = Product.objects.create(ms_id=self.P2, name="Sut", sale_price=200)

    def test_qoldiq_ozgargan_tovar_delta_ga_tushadi(self):
        from datetime import timedelta
        from django.utils import timezone
        from catalog.models import Product, Stock
        from catalog.sync import CatalogSync

        # Birinchi sync: ikkalasi 0 (omborda yo'q)
        CatalogSync(self._client(0, 0)).sync_stock()
        self.assertEqual(Stock.objects.get(product=self.p1).quantity, 0)

        # Kassa oxirgi marta shu vaqtda tortgan
        since = timezone.now()
        # synced_at ni ataylab eskiga suramiz (kassa allaqachon olgan)
        old = since - timedelta(minutes=30)
        Product.objects.update(synced_at=old)

        # KIRIM: Non 25 ta bo'ldi, Sut o'zgarmadi
        CatalogSync(self._client(25, 0)).sync_stock()

        self.assertEqual(Stock.objects.get(product=self.p1).quantity, 25)
        # Non — synced_at yangilandi (kassa delta'ga tushadi)
        self.assertGreaterEqual(Product.objects.get(pk=self.p1.pk).synced_at, since)
        # Sut — o'zgarmagan, delta'ga tushmaydi (bekorga yuborilmaydi)
        self.assertEqual(Product.objects.get(pk=self.p2.pk).synced_at, old)

    def test_ozgarmagan_qoldiq_qayta_yozilmaydi(self):
        from catalog.models import Stock
        from catalog.sync import CatalogSync

        CatalogSync(self._client(5, 7)).sync_stock()
        before = Stock.objects.get(product=self.p1).updated_at
        CatalogSync(self._client(5, 7)).sync_stock()  # hech narsa o'zgarmadi
        self.assertEqual(Stock.objects.get(product=self.p1).updated_at, before)


class PackBarcodeTest(TestCase):
    """MoySklad «Упаковка» kodi: kassa uni skanerlasa shuncha dona qo'shsin
    (2026-09-18, egasining talabi — 6 talik upakovka)."""

    def setUp(self):
        from catalog.models import Product
        self.p = Product.objects.create(
            ms_id="00000000-0000-0000-0000-000000000201", name="Sut 1L", sale_price=12_000_00)

    def _sync(self, barcodes, packs):
        from catalog.sync import CatalogSync
        CatalogSync._sync_barcodes(self.p, barcodes, packs)

    def test_upakovka_kodi_miqdori_bilan_saqlanadi(self):
        from decimal import Decimal
        self._sync([{"ean13": "4780001000017"}],
                   [{"quantity": 6.0, "barcodes": [{"ean13": "14780001000014"}]}])
        rows = {b.value: b.pack_quantity for b in self.p.barcodes.all()}
        self.assertEqual(rows["4780001000017"], Decimal("1"))
        self.assertEqual(rows["14780001000014"], Decimal("6"))

    def test_ozgarmagan_bolsa_qayta_yozilmaydi(self):
        self._sync([{"ean13": "4780001000017"}],
                   [{"quantity": 6, "barcodes": [{"ean13": "14780001000014"}]}])
        ids = set(self.p.barcodes.values_list("pk", flat=True))
        self._sync([{"ean13": "4780001000017"}],
                   [{"quantity": 6, "barcodes": [{"ean13": "14780001000014"}]}])
        self.assertEqual(ids, set(self.p.barcodes.values_list("pk", flat=True)))
        # miqdor o'zgarsa — yangilanadi
        self._sync([{"ean13": "4780001000017"}],
                   [{"quantity": 12, "barcodes": [{"ean13": "14780001000014"}]}])
        self.assertEqual(self.p.barcodes.get(value="14780001000014").pack_quantity, 12)

    def test_upakovka_kodi_oddiy_royxatga_tushmaydi(self):
        """Kassa API: upakovka kodi «barcodes» da EMAS (eski kassa 1 dona deb
        sotmasin), «packs» da miqdori bilan."""
        from django.test import Client
        from sales.models import Register
        self._sync([{"ean13": "4780001000017"}],
                   [{"quantity": 6, "barcodes": [{"ean13": "14780001000014"}]}])
        reg = Register.objects.create(code="k1", name="Kassa-1")
        r = Client().get("/api/v1/catalog", HTTP_AUTHORIZATION="Bearer " + reg.api_token)
        self.assertEqual(r.status_code, 200, r.content)
        row = next(x for x in r.json()["products"] if x["id"] == self.p.pk)
        self.assertEqual(row["barcodes"], ["4780001000017"])
        self.assertEqual(row["packs"], [{"barcode": "14780001000014", "quantity": 6.0}])
