"""Tug'ilgan kuni yaqin mijozlar (egasi, 2026-11-14): panelda «yaqin» belgisi
va 7 kunlik ro'yxat — SMS tabrik kimga ketishini oldindan ko'rish uchun."""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from catalog import tugilgan_kun
from catalog.models import Customer
from dashboard.views import _tugilgan_yaqin

BUGUN = date(2026, 11, 14)


def _mijoz(name, kun, oy, rozi=True, phone="+998 90 123-45-67", card="", **kw):
    return Customer.objects.create(ms_id=uuid.uuid4(), name=name, discount_card=card, phone=phone,
                                   birth_day=kun, birth_month=oy, sms_rozi=rozi, **kw)


class SanaTest(SimpleTestCase):
    def test_qolgan_kun(self):
        q = tugilgan_kun.qolgan_kun
        self.assertEqual(q(14, 11, BUGUN), 0)
        self.assertEqual(q(15, 11, BUGUN), 1)
        self.assertEqual(q(21, 11, BUGUN), 7)
        self.assertEqual(q(13, 11, BUGUN), 364)            # kecha edi — keyingisi kelasi yil
        self.assertEqual(q(2, 1, date(2026, 12, 28)), 5)   # yil oxiridan o'tadi
        self.assertIsNone(q(None, None, BUGUN))

    def test_29_fevral(self):
        q = tugilgan_kun.qolgan_kun
        self.assertEqual(q(29, 2, date(2027, 2, 27)), 2)   # kabisa emas — 1-martda
        self.assertEqual(q(29, 2, date(2027, 3, 1)), 0)
        self.assertEqual(q(29, 2, date(2028, 2, 27)), 2)   # kabisa — 29-fevralda
        self.assertEqual(tugilgan_kun.kun_juftlari(date(2027, 3, 1)), [(1, 3), (29, 2)])
        self.assertEqual(tugilgan_kun.kun_juftlari(date(2028, 3, 1)), [(1, 3)])
        # «bugunmi» bilan bir xil qoida
        for d in (date(2027, 3, 1), date(2028, 2, 29), date(2028, 3, 1)):
            self.assertEqual(q(29, 2, d) == 0, tugilgan_kun.is_today(29, 2, d), d)


class RoyxatTest(TestCase):
    def test_tartib_va_sms_holati(self):
        _mijoz("Bugungi", 14, 11)
        _mijoz("Ertangi", 15, 11, rozi=None)
        _mijoz("Yettinchi", 21, 11, rozi=False)
        _mijoz("Telefonsiz", 18, 11, phone="")
        _mijoz("Sakkizinchi", 22, 11)                      # 8 kun — kirmaydi
        _mijoz("Kechagi", 13, 11)                          # o'tib ketgan — kirmaydi
        _mijoz("Arxivdagi", 16, 11, archived=True)
        Customer.objects.create(ms_id=uuid.uuid4(), name="Kunsiz")
        y = _tugilgan_yaqin(BUGUN)
        self.assertEqual([(r["mijoz"].name, r["qolgan"], r["sms"]) for r in y["rows"]], [
            ("Bugungi", 0, "ok"), ("Ertangi", 1, "soralmagan"),
            ("Telefonsiz", 4, "telefon"), ("Yettinchi", 7, "rad")])
        self.assertEqual((y["kun"], y["jami"], y["sms"]), (7, 4, 1))

    def test_rozilik_talabi_ochirilsa_soralmagan_mijoz_ham_oladi(self):
        from sms.models import SmsSozlama

        _mijoz("Bugungi", 14, 11)
        _mijoz("Ertangi", 15, 11, rozi=None)
        _mijoz("Yettinchi", 21, 11, rozi=False)
        _mijoz("Telefonsiz", 18, 11, phone="", rozi=None)
        y = _tugilgan_yaqin(BUGUN)
        self.assertEqual((y["sms"], y["rozilik_kerak"]), (1, True))
        self.assertEqual(SmsSozlama.objects.update(rozilik_kerak=False), 1)   # egasi belgini o'chirdi
        y = _tugilgan_yaqin(BUGUN)
        self.assertEqual([(r["mijoz"].name, r["sms"]) for r in y["rows"]], [
            ("Bugungi", "ok"), ("Ertangi", "ok"), ("Telefonsiz", "telefon"), ("Yettinchi", "rad")])
        self.assertEqual((y["sms"], y["rozilik_kerak"]), (2, False))

    def test_yil_oxiri(self):
        _mijoz("Yangi yil", 2, 1)
        _mijoz("Dekabr", 31, 12)
        _mijoz("Uzoq", 6, 1)                               # 9 kun — kirmaydi
        y = _tugilgan_yaqin(date(2026, 12, 28))
        self.assertEqual([(r["mijoz"].name, r["qolgan"]) for r in y["rows"]], [("Dekabr", 3), ("Yangi yil", 5)])

    def test_29_fevral_kabisa_bolmagan_yilda(self):
        _mijoz("Kabisa", 29, 2)
        self.assertEqual([(r["mijoz"].name, r["qolgan"]) for r in _tugilgan_yaqin(date(2027, 2, 25))["rows"]],
                         [("Kabisa", 4)])
        self.assertEqual(_tugilgan_yaqin(date(2027, 3, 2))["rows"], [])

    def test_bugun_yuborilgan_sms(self):
        from sms.models import SmsXabar

        c = _mijoz("Bugungi", 14, 11)
        _mijoz("Ertangi", 15, 11)
        SmsXabar.objects.create(customer=c, tur=SmsXabar.TUGILGAN_KUN, yil=2026, sana=BUGUN,
                                telefon="998901234567", matn="Tabrik", holat=SmsXabar.YUBORILDI)
        rows = _tugilgan_yaqin(BUGUN)["rows"]
        self.assertEqual(rows[0]["xabar"].holat, SmsXabar.YUBORILDI)
        self.assertIsNone(rows[1]["xabar"])


@patch("dashboard.views._bugun", return_value=BUGUN)
class SahifaTest(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user("egasi", password="x"))
        self.a = _mijoz("Aliyeva Bugun", 14, 11, card="7700001")
        self.b = _mijoz("Botirov Ertaga", 15, 11, rozi=None, card="7700002")
        self.c = _mijoz("Usmonova Yaqin", 18, 11, phone="", card="7700003")
        self.d = _mijoz("Uzoq Mijoz", 1, 1, card="7700004")

    @staticmethod
    def _qism(html: str, boshi: str, oxiri: str) -> str:
        html = html[html.index(boshi):]
        return html[:html.index(oxiri)]

    def test_bonus_sahifasida_yaqin_royxat(self, _):
        html = self.client.get("/bonus/").content.decode()
        self.assertIn("Tug'ilgan kuni yaqin: 3", html)
        jadval = self._qism(html, 'id="tk-yaqin"', "</table>")
        # Eng yaqini tepada
        self.assertLess(jadval.index("Aliyeva Bugun"), jadval.index("Botirov Ertaga"))
        self.assertLess(jadval.index("Botirov Ertaga"), jadval.index("Usmonova Yaqin"))
        self.assertNotIn("Uzoq Mijoz", jadval)
        for matn in ("bugun!", "yaqin · ertaga", "yaqin · 4 kun", "SMS oladi", "telefon raqami"):
            self.assertIn(matn, jadval)
        # Rozilik so'ralmaganda — mijoz sahifasidagi rozilik joyiga havola
        self.assertIn(f'/bonus/mijoz/{self.b.pk}/#sms-rozilik">rozilik so\'ralmagan', jadval)

    def test_rozilik_talabi_ochirilgan_bolsa_yozuv_ozgaradi(self, _):
        from sms.models import SmsSozlama

        html = self.client.get("/bonus/").content.decode()
        self.assertIn("mijozdan kassada so'rab qoling", html)
        self.assertNotIn("Rozilik talabi o'chirilgan", html)
        self.assertEqual(SmsSozlama.objects.update(rozilik_kerak=False), 1)
        html = self.client.get("/bonus/").content.decode()
        self.assertIn("Rozilik talabi o'chirilgan — tabrik «rad etgan»lardan boshqa har bir mijozga ketadi", html)
        self.assertNotIn("mijozdan kassada so'rab qoling", html)
        jadval = self._qism(html, 'id="tk-yaqin"', "</table>")
        # rozilik so'ralmagan mijoz endi «SMS oladi»; rozilik joyiga havola yo'q
        self.assertEqual(jadval.count("SMS oladi"), 2)
        self.assertNotIn("rozilik so'ralmagan", jadval)
        self.assertIn("telefon raqami", jadval)

    def test_mijozlar_royxatida_belgi(self, _):
        html = self.client.get("/bonus/?tk=bor").content.decode()
        royxat = self._qism(html, 'id="mijozlar"', "</table>")
        self.assertIn("yaqin · ertaga", royxat)
        self.assertIn("yaqin · 4 kun", royxat)
        self.assertIn("bugun!", royxat)
        qator = self._qism(royxat, "Uzoq Mijoz", "</tr>")
        self.assertNotIn("yaqin", qator)

    def test_mijoz_sahifasida_belgi(self, _):
        self.assertContains(self.client.get(f"/bonus/mijoz/{self.b.pk}/"), "yaqin · ertaga")
        self.assertNotContains(self.client.get(f"/bonus/mijoz/{self.d.pk}/"), "yaqin ·")

    def test_sms_sahifasidan_royxatga_havola(self, _):
        self.assertContains(self.client.get("/sms/"), 'href="/bonus/#yaqin"')

    def test_yaqin_yoq(self, _):
        Customer.objects.filter(pk__in=[self.a.pk, self.b.pk, self.c.pk]).delete()
        self.assertContains(self.client.get("/bonus/"), "Bugundan 7 kun ichida tug'ilgan kuni bor mijoz yo'q")
