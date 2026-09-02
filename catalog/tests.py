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
