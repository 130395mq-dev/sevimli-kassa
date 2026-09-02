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
