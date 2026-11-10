"""Mijozning tug'ilgan kuni (SEVIMLI BONUS, 2026-11-08): sana o'qish,
«bugunmi» tekshiruvi, kassa API va panel."""

from __future__ import annotations

import json
import uuid
from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from catalog import tugilgan_kun
from catalog.models import Customer, RetailStore
from sales.models import Register


class ParseTest(TestCase):
    def test_kun_oy(self):
        self.assertEqual(tugilgan_kun.parse("12.05"), (12, 5, None))
        self.assertEqual(tugilgan_kun.parse(" 1/3 "), (1, 3, None))
        self.assertEqual(tugilgan_kun.parse("29.02"), (29, 2, None))     # yilsiz — ruxsat

    def test_yil_bilan(self):
        self.assertEqual(tugilgan_kun.parse("12.05.1990"), (12, 5, 1990))
        self.assertEqual(tugilgan_kun.parse("12-05-90", today=date(2026, 11, 8)), (12, 5, 1990))
        self.assertEqual(tugilgan_kun.parse("12.05.26", today=date(2026, 11, 8)), (12, 5, 2026))
        self.assertEqual(tugilgan_kun.parse("12.05.27", today=date(2026, 11, 8)), (12, 5, 1927))

    def test_notogri(self):
        for bad in ("", "abc", "32.01", "12.13", "29.02.2025", "12.05.1800", "12.05.2030", "2026.05.12"):
            with self.assertRaises(tugilgan_kun.BirthdayError, msg=bad):
                tugilgan_kun.parse(bad, today=date(2026, 11, 8))

    def test_matn(self):
        self.assertEqual(tugilgan_kun.text(12, 5, None), "12.05")
        self.assertEqual(tugilgan_kun.text(3, 11, 1985), "03.11.1985")
        self.assertEqual(tugilgan_kun.text(None, None, None), "")

    def test_bugunmi(self):
        self.assertTrue(tugilgan_kun.is_today(8, 11, date(2026, 11, 8)))
        self.assertFalse(tugilgan_kun.is_today(9, 11, date(2026, 11, 8)))
        self.assertFalse(tugilgan_kun.is_today(None, None, date(2026, 11, 8)))
        # 29-fevral: kabisa bo'lmagan yilda 1-martda
        self.assertTrue(tugilgan_kun.is_today(29, 2, date(2027, 3, 1)))
        self.assertFalse(tugilgan_kun.is_today(29, 2, date(2028, 3, 1)))
        self.assertTrue(tugilgan_kun.is_today(29, 2, date(2028, 2, 29)))


class ApiTest(TestCase):
    def setUp(self):
        store = RetailStore.objects.create(ms_id=uuid.uuid4(), name="Sinov")
        self.reg = Register.objects.create(code="k1", name="Kassa-1", store=store)
        self.reg.set_password("1111")
        self.reg.save()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.reg.api_token}"}

    def _post(self, url, payload):
        return self.client.post(url, data=json.dumps(payload), content_type="application/json", **self.auth)

    def test_yangi_mijoz_tugilgan_kun_bilan(self):
        r = self._post("/api/v1/customers/create",
                       {"name": "Aliyeva Dilnoza", "phone": "998901112233", "card": "7700011", "birthday": "12.05.1990"})
        self.assertEqual(r.status_code, 200, r.content)
        c = r.json()["customer"]
        self.assertEqual(c["birthday"], "12.05.1990")
        self.assertIn("is_birthday", c)
        db = Customer.objects.get(discount_card="7700011")
        self.assertEqual((db.birth_day, db.birth_month, db.birth_year), (12, 5, 1990))

    def test_notogri_sana_400(self):
        r = self._post("/api/v1/customers/create",
                       {"name": "Aliyeva Dilnoza", "phone": "998901112233", "card": "7700011", "birthday": "32.13"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Customer.objects.filter(discount_card="7700011").exists())

    def test_sanasiz_ham_boladi(self):
        r = self._post("/api/v1/customers/create", {"name": "Aliyeva Dilnoza", "phone": "", "card": "7700012"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["customer"]["birthday"], "")
        self.assertFalse(r.json()["customer"]["is_birthday"])

    def test_karta_bilan_qidirganda_bugun_belgisi(self):
        today = date(2026, 11, 8)
        Customer.objects.create(ms_id=uuid.uuid4(), name="Bugungi", discount_card="7700013",
                                birth_day=8, birth_month=11, birth_year=None)
        Customer.objects.create(ms_id=uuid.uuid4(), name="Ertangi", discount_card="7700014",
                                birth_day=9, birth_month=11)
        with patch("django.utils.timezone.localdate", return_value=today):
            a = self.client.get("/api/v1/customers?card=7700013", **self.auth).json()["customers"][0]
            b = self.client.get("/api/v1/customers?card=7700014", **self.auth).json()["customers"][0]
        self.assertEqual((a["birthday"], a["is_birthday"]), ("08.11", True))
        self.assertEqual((b["birthday"], b["is_birthday"]), ("09.11", False))

    def test_mavjud_mijozga_kiritish(self):
        c = Customer.objects.create(ms_id=uuid.uuid4(), name="Eski", discount_card="7700015")
        r = self._post("/api/v1/customers/birthday", {"id": c.pk, "birthday": "3.11"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["customer"]["birthday"], "03.11")
        c.refresh_from_db()
        self.assertEqual((c.birth_day, c.birth_month, c.birth_year), (3, 11, None))
        # noto'g'ri — o'zgarmaydi
        r = self._post("/api/v1/customers/birthday", {"id": c.pk, "birthday": "40.11"})
        self.assertEqual(r.status_code, 400)
        c.refresh_from_db()
        self.assertEqual(c.birth_day, 3)
        # bo'sh — o'chiradi
        r = self._post("/api/v1/customers/birthday", {"id": c.pk, "birthday": ""})
        self.assertEqual(r.status_code, 200)
        c.refresh_from_db()
        self.assertIsNone(c.birth_day)
        # yo'q mijoz
        self.assertEqual(self._post("/api/v1/customers/birthday", {"id": 999999, "birthday": "1.1"}).status_code, 404)

    def test_tokensiz_kirib_bolmaydi(self):
        c = Customer.objects.create(ms_id=uuid.uuid4(), name="Eski", discount_card="7700016")
        r = self.client.post("/api/v1/customers/birthday", data=json.dumps({"id": c.pk, "birthday": "3.11"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 401)

    # --- kim kiritgani (egasi, 2026-11-10)

    def _kassir_sessiya(self, name="Dilnoza"):
        from sales.models import Cashier
        cashier = Cashier.objects.create(login=name.lower(), name=name)
        cashier.set_password("1234")
        cashier.save()
        r = self._post("/api/v1/login", {"login": cashier.login, "password": "1234"})
        self.assertEqual(r.status_code, 200, r.content)
        return {"HTTP_X_SESSION": r.json()["session"]}

    def test_kim_kiritgani_kassa_va_kassir(self):
        c = Customer.objects.create(ms_id=uuid.uuid4(), name="Eski", discount_card="7700018")
        sess = self._kassir_sessiya("Dilnoza")
        r = self.client.post("/api/v1/customers/birthday", data=json.dumps({"id": c.pk, "birthday": "3.11"}),
                             content_type="application/json", **self.auth, **sess)
        self.assertEqual(r.status_code, 200, r.content)
        c.refresh_from_db()
        self.assertEqual(c.birth_set_source, Customer.BIRTH_KASSA)
        self.assertEqual(c.birth_set_register, self.reg)
        self.assertEqual(c.birth_set_cashier, "Dilnoza")
        self.assertIsNotNone(c.birth_set_at)
        self.assertEqual(c.birth_set_label, "Kassa-1 · Dilnoza")
        # O'chirilsa — kim kiritgani ham o'chadi
        self._post("/api/v1/customers/birthday", {"id": c.pk, "birthday": ""})
        c.refresh_from_db()
        self.assertIsNone(c.birth_set_at)
        self.assertEqual((c.birth_set_cashier, c.birth_set_source, c.birth_set_label), ("", "", ""))

    def test_kassaning_oz_logini_bilan(self):
        """Sessiya tokeni yo'q (kassa o'z logini bilan) — kassir «», faqat kassa."""
        c = Customer.objects.create(ms_id=uuid.uuid4(), name="Eski", discount_card="7700019")
        self._post("/api/v1/customers/birthday", {"id": c.pk, "birthday": "3.11"})
        c.refresh_from_db()
        self.assertEqual((c.birth_set_register, c.birth_set_cashier), (self.reg, ""))
        self.assertEqual(c.birth_set_label, "Kassa-1")

    def test_royxatdan_otkazishda_kim(self):
        sess = self._kassir_sessiya("Nodira")
        r = self.client.post("/api/v1/customers/create",
                             data=json.dumps({"name": "Yangi Mijoz", "phone": "", "card": "7700020", "birthday": "12.05"}),
                             content_type="application/json", **self.auth, **sess)
        self.assertEqual(r.status_code, 200, r.content)
        c = Customer.objects.get(discount_card="7700020")
        self.assertEqual((c.birth_set_source, c.birth_set_cashier), (Customer.BIRTH_ROYXAT, "Nodira"))
        self.assertEqual(c.birth_set_register, self.reg)
        # Sanasiz ro'yxat — kim kiritgani ham bo'sh
        r = self._post("/api/v1/customers/create", {"name": "Sanasiz Mijoz", "phone": "", "card": "7700021"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIsNone(Customer.objects.get(discount_card="7700021").birth_set_at)


class PanelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("egasi", password="x")
        self.client.force_login(self.user)
        self.cust = Customer.objects.create(ms_id=uuid.uuid4(), name="Aliyeva Dilnoza", discount_card="7700017")

    def test_sahifada_kiritish(self):
        url = f"/bonus/mijoz/{self.cust.pk}/"
        r = self.client.get(url)
        self.assertContains(r, "Tug'ilgan kuni")
        r = self.client.post(url, {"action": "birthday", "birthday": "12.05.1990"}, follow=True)
        self.assertContains(r, "12.05.1990")
        self.cust.refresh_from_db()
        self.assertEqual(self.cust.birth_year, 1990)
        r = self.client.post(url, {"action": "birthday", "birthday": "99.99"}, follow=True)
        self.assertContains(r, "Oy 1 dan 12 gacha")
        self.cust.refresh_from_db()
        self.assertEqual(self.cust.birth_year, 1990)          # o'zgarmadi
        # Paneldan kiritilgani yoziladi va sahifada ko'rinadi
        self.assertEqual((self.cust.birth_set_source, self.cust.birth_set_user), (Customer.BIRTH_PANEL, "egasi"))
        self.assertContains(r, "Panel · egasi")

    def test_bonus_sahifasida_tugilgan_kunlar(self):
        from django.utils import timezone

        from sales.models import Register
        store = RetailStore.objects.create(ms_id=uuid.uuid4(), name="Sinov")
        reg = Register.objects.create(code="k2", name="Shaxar kassa 2", store=store)
        now = timezone.now()
        for i, (name, who) in enumerate([("A Mijoz", "Dilnoza"), ("B Mijoz", "Dilnoza"), ("C Mijoz", "")]):
            c = Customer.objects.create(ms_id=uuid.uuid4(), name=name, discount_card=f"770010{i}")
            c.set_birthday("12.05", source=Customer.BIRTH_KASSA, register=reg, cashier=who, now=now)
            c.save()
        Customer.objects.create(ms_id=uuid.uuid4(), name="Eski Yozuv", discount_card="7700110",
                                birth_day=1, birth_month=1)                     # kim kiritgani yo'q
        Customer.objects.create(ms_id=uuid.uuid4(), name="Kunsiz Mijoz", discount_card="7700111")
        Customer.objects.create(ms_id=uuid.uuid4(), name="Kartasiz", discount_card="")  # hisobga kirmaydi
        r = self.client.get("/bonus/")
        self.assertContains(r, "Tug'ilgan kunlar")
        # kartali: self.cust + 3 + Eski + Kunsiz = 6; kiritilgan: 3 + Eski = 4
        self.assertContains(r, "4 <span class=\"sec\" style=\"font-weight:400;\">/ 6 (67%)", html=False)
        self.assertContains(r, "Kiritilmaganlar: 2")
        self.assertContains(r, "Shaxar kassa 2 · Dilnoza")
        self.assertContains(r, "Kim kiritgani saqlanmagan")
        # Kiritilmaganlar ro'yxati
        # («A Mijoz» «Oxirgi kiritilganlar» jadvalida ham bor — ro'yxatda qo'shimcha marta chiqadi)
        r = self.client.get("/bonus/?tk=yoq")
        self.assertContains(r, "Kunsiz Mijoz")
        self.assertContains(r, "Aliyeva Dilnoza")
        self.assertEqual(r.content.decode().count("A Mijoz"), 1)
        r = self.client.get("/bonus/?tk=bor")
        self.assertEqual(r.content.decode().count("A Mijoz"), 2)
        self.assertNotContains(r, "Kunsiz Mijoz")
