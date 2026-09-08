"""Kassadan yangi mijoz qo'shish va uni qidirib topish."""

from __future__ import annotations

import json

from django.test import TestCase

import uuid

from catalog.models import Customer, RetailStore
from sales.models import Register


class CreateCustomerTests(TestCase):
    def setUp(self):
        store = RetailStore.objects.create(ms_id=uuid.uuid4(), name="Sinov")
        self.reg = Register.objects.create(code="k1", name="Kassa-1", store=store)
        self.reg.set_password("1111")
        self.reg.save()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.reg.api_token}"}

    def _post(self, payload):
        return self.client.post(
            "/api/v1/customers/create",
            data=json.dumps(payload),
            content_type="application/json",
            **self.auth,
        )

    def test_creates_and_is_searchable(self):
        r = self._post({"name": "Aliyev Bek", "phone": "998901112233",
                        "card": "55009999"})
        self.assertEqual(r.status_code, 200)
        c = r.json()["customer"]
        self.assertEqual(c["name"], "Aliyev Bek")
        self.assertEqual(c["card"], "55009999")
        self.assertTrue(Customer.objects.filter(name="Aliyev Bek").exists())

        # ism, karta va telefon bo'yicha topiladi
        for q in ("Aliyev", "5500", "99890"):
            res = self.client.get(f"/api/v1/customers?q={q}", **self.auth)
            names = [x["name"] for x in res.json()["customers"]]
            self.assertIn("Aliyev Bek", names, f"'{q}' bo'yicha topilmadi")

    def test_short_name_rejected(self):
        r = self._post({"name": "A", "phone": "", "card": ""})
        self.assertEqual(r.status_code, 400)
