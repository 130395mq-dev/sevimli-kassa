"""SEVIMLI BONUS mijozlari MoySklad'ga qaram emas (2026-10-22).

Xato: kassada ro'yxatdan o'tgan mijoz qayta kelib kartasini urganda
topilmasdi — karta MoySklad'ga yozilmas, sinxron esa bo'sh kartani bazaga
yozib, uni o'chirib yuborardi.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

import requests
from django.test import TestCase, override_settings

from catalog import mijozlar
from catalog.models import Customer, RetailStore
from catalog.sync import CatalogSync
from sales.models import Register


def _ms_row(ms_id, name="Aliyev Bek", phone="", card="", **extra):
    row = {"id": str(ms_id), "name": name, "phone": phone, "discountCardNumber": card,
           "salesAmount": 0, "archived": False, "updated": "2026-10-22 10:00:00.000"}
    row.update(extra)
    return row


class KassaBase(TestCase):
    def setUp(self):
        store = RetailStore.objects.create(ms_id=uuid.uuid4(), name="Sinov")
        self.reg = Register.objects.create(code="k1", name="Kassa-1", store=store)
        self.reg.set_password("1111")
        self.reg.save()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.reg.api_token}"}

    def create(self, name="Aliyev Bek", phone="+998901112233", card="55009999"):
        return self.client.post("/api/v1/customers/create",
                                data=json.dumps({"name": name, "phone": phone, "card": card}),
                                content_type="application/json", **self.auth)

    def find(self, card):
        r = self.client.get(f"/api/v1/customers?card={card}", **self.auth)
        return r.json()["customers"]

    def sync(self, rows):
        client = MagicMock()
        client.iter_list.return_value = iter(rows)
        with patch.object(CatalogSync, "_delta_filter", return_value={}):
            CatalogSync(client)._sync_customers(MagicMock(), False)


class RoyxatdanOtishTest(KassaBase):
    def test_moysklad_yoq_bolsa_ham_mijoz_yaratiladi_va_karta_bilan_topiladi(self):
        r = self.create()
        self.assertEqual(r.status_code, 200)
        c = Customer.objects.get(pk=r.json()["customer"]["id"])
        self.assertTrue(c.ms_local)                         # MoySklad'da hali yo'q
        self.assertEqual(c.discount_card, "55009999")
        self.assertEqual([x["name"] for x in self.find("55009999")], ["Aliyev Bek"])

    @override_settings(MOYSKLAD_TOKEN="t")
    def test_moysklad_ga_karta_bilan_yoziladi(self):
        fake = MagicMock()
        new_id = str(uuid.uuid4())
        fake.post.return_value = {"id": new_id}
        with patch("catalog.mijozlar._client", return_value=fake):
            r = self.create()
        self.assertEqual(r.status_code, 200)
        fake.post.assert_called_once_with("entity/counterparty", {
            "name": "Aliyev Bek", "phone": "+998901112233", "discountCardNumber": "55009999"})
        c = Customer.objects.get(pk=r.json()["customer"]["id"])
        self.assertEqual(str(c.ms_id), new_id)
        self.assertFalse(c.ms_local)
        self.assertEqual(r.json()["customer"]["ms_id"], new_id)

    @override_settings(MOYSKLAD_TOKEN="t")
    def test_moysklad_ishlamasa_kassa_kutmaydi_keyin_fon_ishi_yozadi(self):
        fake = MagicMock()
        fake.post.side_effect = requests.ConnectionError("Read timed out")
        with patch("catalog.mijozlar._client", return_value=fake):
            r = self.create()
        self.assertEqual(r.status_code, 200)                # ro'yxatdan o'tdi
        c = Customer.objects.get(pk=r.json()["customer"]["id"])
        self.assertTrue(c.ms_local)
        self.assertEqual(self.find("55009999")[0]["id"], c.pk)
        # Fon ishi (healer) keyin yozadi
        ok = MagicMock()
        ok.post.return_value = {"id": str(uuid.uuid4())}
        done = mijozlar.push_pending(client=ok)
        self.assertEqual(done["created"], 1)
        c.refresh_from_db()
        self.assertFalse(c.ms_local)

    def test_sinxron_kartani_ochirmaydi(self):
        # Asl xato: MoySklad'da karta bo'sh -> sinxron bizdagini o'chirardi
        ms_id = uuid.uuid4()
        c = Customer.objects.create(ms_id=ms_id, name="Aliyev Bek", phone="+998901112233",
                                    discount_card="55009999", bonus_points=120)
        self.sync([_ms_row(ms_id, phone="", card="")])
        c.refresh_from_db()
        self.assertEqual(c.discount_card, "55009999")
        self.assertEqual(c.phone, "+998901112233")
        self.assertTrue(c.ms_card_pending)                  # MoySklad'ga yoziladi
        self.assertEqual(c.bonus_points, 120)               # ball tegilmaydi
        self.assertEqual(self.find("55009999")[0]["id"], c.pk)

    def test_moysklad_da_karta_almashtirilsa_yangisi_olinadi(self):
        ms_id = uuid.uuid4()
        c = Customer.objects.create(ms_id=ms_id, name="Aliyev Bek", discount_card="111")
        self.sync([_ms_row(ms_id, card="222")])
        c.refresh_from_db()
        self.assertEqual(c.discount_card, "222")
        self.assertFalse(c.ms_card_pending)

    def test_yozilmagan_karta_moysklad_eskisi_bilan_almashmaydi(self):
        ms_id = uuid.uuid4()
        c = Customer.objects.create(ms_id=ms_id, name="Aliyev Bek", discount_card="333",
                                    ms_card_pending=True)
        self.sync([_ms_row(ms_id, card="222")])
        c.refresh_from_db()
        self.assertEqual(c.discount_card, "333")

    def test_karta_moysklad_ga_put_bilan_yoziladi(self):
        ms_id = uuid.uuid4()
        c = Customer.objects.create(ms_id=ms_id, name="Aliyev Bek", phone="+998901112233",
                                    discount_card="55009999", ms_card_pending=True)
        fake = MagicMock()
        done = mijozlar.push_pending(client=fake)
        self.assertEqual(done["updated"], 1)
        fake.put.assert_called_once_with(f"entity/counterparty/{ms_id}", {
            "discountCardNumber": "55009999", "phone": "+998901112233"})
        c.refresh_from_db()
        self.assertFalse(c.ms_card_pending)


class KartasiOchganMijozTest(KassaBase):
    def setUp(self):
        super().setUp()
        # Xato tufayli kartasi o'chib ketgan, ballari bor mijoz
        self.old = Customer.objects.create(ms_id=uuid.uuid4(), name="Aliyev Bek",
                                           phone="998 90 111-22-33", discount_card="",
                                           bonus_points=4500)

    def test_shu_telefon_bilan_qayta_royxat_eskisini_tiklaydi(self):
        before = Customer.objects.count()
        r = self.create(name="Aliyev Bek", phone="+998901112233", card="55009999")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["restored"])
        self.assertEqual(r.json()["customer"]["id"], self.old.pk)
        self.assertEqual(r.json()["customer"]["bonus_points"], 4500)   # ballar saqlandi
        self.assertEqual(Customer.objects.count(), before)              # yangi yozuv yo'q
        self.old.refresh_from_db()
        self.assertEqual(self.old.discount_card, "55009999")
        self.assertTrue(self.old.ms_card_pending)
        self.assertEqual(self.find("55009999")[0]["id"], self.old.pk)

    def test_telefon_boshqa_kartaga_biriktirilgan_bolsa_rad(self):
        self.old.discount_card = "7001"
        self.old.save()
        r = self.create(phone="+998901112233", card="55009999")
        self.assertEqual(r.status_code, 409)
        self.assertIn("boshqa kartaga", r.json()["error"])
        self.assertFalse(Customer.objects.filter(discount_card="55009999").exists())

    def test_karta_allaqachon_band(self):
        self.old.discount_card = "55009999"
        self.old.save()
        r = self.create(name="Boshqa Odam", phone="+998935554433", card="55009999")
        self.assertEqual(r.status_code, 409)
        self.assertIn("allaqachon", r.json()["error"])

    def test_kartasiz_royxat_avvalgidek_yangi_mijoz(self):
        r = self.create(phone="+998901112233", card="")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["restored"])
        self.assertNotEqual(r.json()["customer"]["id"], self.old.pk)


class TelefonKalitiTest(TestCase):
    def test_turli_yozuvlar_bir_xil(self):
        for p in ("+998 90 111-22-33", "998901112233", "901112233", "(90) 111 22 33"):
            self.assertEqual(mijozlar.phone_key(p), "901112233", p)
        self.assertEqual(mijozlar.phone_key("12345"), "")


class ChekKontragentiTest(TestCase):
    def test_moysklad_da_yoq_mijoz_cheki_roznichniy_ga(self):
        from sales.writer import SaleWriter

        local = Customer.objects.create(ms_id=uuid.uuid4(), name="Yangi", ms_local=True)
        real = Customer.objects.create(ms_id=uuid.uuid4(), name="Eski")
        w = SaleWriter(MagicMock())
        with patch.object(SaleWriter, "_retail_agent_id", return_value="00000000-0000-0000-0000-000000000777"):
            self.assertIn("000000000777", w._agent(MagicMock(customer_id=1, customer=local))["meta"]["href"])
            self.assertIn(str(real.ms_id), w._agent(MagicMock(customer_id=2, customer=real))["meta"]["href"])
