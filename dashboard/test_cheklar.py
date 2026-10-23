"""Panel «Cheklar»: barcha cheklar, nakopitel ishlatilgan bo'lsa mijoz ismi
bilan (egasining so'rovi, 2026-10-22). Boshqaruvchi faqat o'z marketini."""

from django.contrib.auth.models import User
from django.test import Client

from dashboard.test_tovarlar import TovarlarBase
from sales.models import PanelManager, Sale


class CheklarTest(TovarlarBase):
    def setUp(self):
        super().setUp()
        self.with_card = self.receipt(lines=[(self.non, 2, 3000_00, "")], customer=self.ali,
                                      earned=60, hour=10)
        self.plain = self.receipt(lines=[(self.sut, 1, 12000_00, "")], hour=11)
        self.foreign = self.receipt(lines=[(self.sut, 1, 5000_00, "")], customer=self.vali,
                                    hour=12, reg=self.reg2)
        self.owner = User.objects.create_user("egasi", password="egasi-parol-1")
        self.client.force_login(self.owner)

    def test_hamma_cheklar_mijoz_ismi_bilan(self):
        r = self.client.get("/cheklar/?davr=bugun")
        self.assertEqual(r.status_code, 200)
        ids = [s.pk for s in r.context["rows"]]
        self.assertEqual(set(ids), {self.with_card.pk, self.plain.pk, self.foreign.pk})
        self.assertContains(r, "Ali Valiyev")
        self.assertContains(r, "karta 7001")
        self.assertContains(r, "+60")
        t = r.context["d"]["totals"]
        self.assertEqual((t["receipts"], t["card"], t["customers"]), (3, 2, 2))
        self.assertContains(r, 'href="/cheklar/"')           # menyuda

    def test_faqat_kartali(self):
        r = self.client.get("/cheklar/?davr=bugun&tur=karta")
        self.assertEqual({s.pk for s in r.context["rows"]}, {self.with_card.pk, self.foreign.pk})

    def test_qidiruv_va_kassa(self):
        r = self.client.get("/cheklar/?davr=bugun&q=Vali Karimov")
        self.assertEqual([s.pk for s in r.context["rows"]], [self.foreign.pk])
        r = self.client.get(f"/cheklar/?davr=bugun&kassa={self.reg1.pk}")
        self.assertEqual({s.pk for s in r.context["rows"]}, {self.with_card.pk, self.plain.pk})

    def test_boshqaruvchi_faqat_oz_marketini_koradi(self):
        user = User.objects.create_user("chilonzor", password="chilonzor-1")
        PanelManager.objects.create(user=user, warehouse_ms_id=self.wh1.ms_id, warehouse_name="Chilonzor")
        c = Client()
        c.force_login(user)
        r = c.get("/cheklar/?davr=bugun")
        self.assertEqual(r.status_code, 200)
        self.assertEqual({s.pk for s in r.context["rows"]}, {self.with_card.pk, self.plain.pk})
        self.assertNotContains(r, "Vali Karimov")
        self.assertNotContains(r, "Kassa-2")
        self.assertContains(r, "Ali Valiyev")
        self.assertNotContains(r, "/bonus/mijoz/")         # mijoz sahifasi faqat egasiga

    def test_qaytarish_belgisi(self):
        ret = self.receipt(lines=[(self.non, 1, 3000_00, "")], customer=self.ali,
                           kind=Sale.RETURN, origin=self.with_card, hour=13)
        r = self.client.get("/cheklar/?davr=bugun&tur=qaytarish")
        self.assertEqual([s.pk for s in r.context["rows"]], [ret.pk])
        self.assertContains(r, "qaytarish")
