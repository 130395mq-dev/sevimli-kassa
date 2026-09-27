"""0008 migratsiyasi: upakovka kodi bor tovarlar kassalarga qayta yuboriladi.

2026-09-27: 4780026392308 (16 talik) boshqa kassalarda 16 × 9 000 bo'lib
chiqadi, Kassa 4 da chiqmaydi — kassa upakovka kodini hech qachon saqlamagan.
"""
from datetime import timedelta
from decimal import Decimal
from importlib import import_module

from django.apps import apps
from django.test import Client, TestCase
from django.utils import timezone

from catalog.models import Barcode, Product
from sales.models import Register

resend = import_module("catalog.migrations.0008_resend_pack_products").resend_pack_products


class ResendPackProductsTest(TestCase):
    def setUp(self):
        self.old = timezone.now() - timedelta(days=9)

        def make(n, name, pack=None, archived=False):
            p = Product.objects.create(
                ms_id=f"00000000-0000-0000-0000-0000000008{n:02d}", name=name,
                sale_price=9_000_00, archived=archived)
            Barcode.objects.create(product=p, value=f"20000028697{n:02d}")
            if pack:
                Barcode.objects.create(product=p, value=f"47800263923{n:02d}",
                                       pack_quantity=Decimal(pack))
            return p

        self.kippers = make(1, "подгузники KIPPERS №3 4 шт", pack=16)
        self.plain = make(2, "Sut 1L")
        self.gone = make(3, "Arxivdagi", pack=6, archived=True)
        Product.objects.update(synced_at=self.old)

    def _synced(self, p):
        return Product.objects.get(pk=p.pk).synced_at

    def test_faqat_upakovkali_tirik_tovar_qayta_yuboriladi(self):
        resend(apps, None)
        self.assertGreater(self._synced(self.kippers), self.old)
        self.assertEqual(self._synced(self.plain), self.old)
        self.assertEqual(self._synced(self.gone), self.old)

    def test_kassa_deltasida_upakovka_kodi_keladi(self):
        reg = Register.objects.create(code="k4", name="Kassa 4")
        since = (self.old + timedelta(hours=1)).isoformat()
        auth = {"HTTP_AUTHORIZATION": "Bearer " + reg.api_token}
        before = Client().get("/api/v1/catalog", {"since": since}, **auth).json()["products"]
        self.assertEqual(before, [])            # o'zgarmagan — kassaga kelmasdi

        resend(apps, None)
        rows = Client().get("/api/v1/catalog", {"since": since}, **auth).json()["products"]
        self.assertEqual([r["id"] for r in rows], [self.kippers.pk])
        self.assertEqual(rows[0]["packs"], [{"barcode": "4780026392301", "quantity": 16.0}])
