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

    def test_hozir_yuborish_kutishni_bekor_qiladi(self):
        from datetime import timedelta
        from django.utils import timezone
        Sale.objects.filter(pk=self.plain.pk).update(
            sync_status=Sale.FAILED, sync_attempts=9, sync_error="MoySklad 412: price",
            next_attempt_at=timezone.now() + timedelta(minutes=60))
        r = self.client.get(f"/chek/{self.plain.pk}/")
        self.assertContains(r, "Hozir yuborish")
        self.assertContains(r, "MoySklad 412: price")
        r = self.client.post(f"/chek/{self.plain.pk}/", {"action": "send_now"}, follow=True)
        from html import unescape
        self.assertIn("navbatga qo'yildi", unescape(r.content.decode()))
        s = Sale.objects.get(pk=self.plain.pk)
        self.assertEqual((s.sync_status, s.sync_attempts, s.next_attempt_at), (Sale.NEW, 0, None))
        from sales import sender
        self.assertIn(s, sender.due_queue())

    def test_yozilgan_chekka_tegilmaydi(self):
        r = self.client.get(f"/chek/{self.with_card.pk}/")
        self.assertNotContains(r, "Hozir yuborish")
        self.client.post(f"/chek/{self.with_card.pk}/", {"action": "send_now"})
        self.assertEqual(Sale.objects.get(pk=self.with_card.pk).sync_status, Sale.SENT)

    def test_boshqaruvchi_hozir_yubora_olmaydi(self):
        user = User.objects.create_user("chilonzor2", password="chilonzor-2")
        PanelManager.objects.create(user=user, warehouse_ms_id=self.wh1.ms_id, warehouse_name="Chilonzor")
        Sale.objects.filter(pk=self.plain.pk).update(sync_status=Sale.STUCK, sync_attempts=12)
        c = Client()
        c.force_login(user)
        r = c.get(f"/chek/{self.plain.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Hozir yuborish")
        c.post(f"/chek/{self.plain.pk}/", {"action": "send_now"})
        self.assertEqual(Sale.objects.get(pk=self.plain.pk).sync_status, Sale.STUCK)

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
