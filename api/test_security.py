"""
Server-tomon moliyaviy yaxlitlik va idempotentlik testlari.

Bu testlar aynan yangi himoyalarni isbotlaydi:
  - mijoz yuborgan `total` narx×miqdordan (brutto) osha olmaydi;
  - manfiy narx/summa rad etiladi;
  - ball chek summasidan oshsa (net_total < 0) rad etiladi;
  - qonuniy chegirmali chek O'TADI (yolg'on-rad yo'q);
  - bir xil local_uuid ikki marta yuborilsa — bitta sale.
"""

from __future__ import annotations

import uuid

from sales.models import Sale

from .tests import ApiTestCase


class FinancialIntegrityTest(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.open_shift()

    def test_total_narx_miqdordan_katta_bolsa_rad(self):
        # price 3000.00 × 1 = 3000.00 brutto; total 4000.00 — imkonsiz
        p = self.sale_payload()
        p["items"][0]["total"] = 4_000_00
        p["payments"] = [{"method": "naqd", "amount": 4_000_00}]
        r = self.post("/api/v1/sales", p)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Sale.objects.count(), 0)

    def test_manfiy_narx_rad(self):
        p = self.sale_payload()
        p["items"][0]["price"] = -100
        r = self.post("/api/v1/sales", p)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Sale.objects.count(), 0)

    def test_manfiy_total_rad(self):
        p = self.sale_payload()
        p["items"][0]["total"] = -1
        r = self.post("/api/v1/sales", p)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Sale.objects.count(), 0)

    def test_ball_summadan_katta_bolsa_rad(self):
        # 4000 ball = 400 000 tiyin > 300 000 tiyin (3000.00) -> net_total < 0
        p = self.sale_payload()
        p["points_spent"] = 4000
        r = self.post("/api/v1/sales", p)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Sale.objects.count(), 0)

    def test_qonuniy_chegirmali_chek_otadi(self):
        # total (2900.00) < brutto (3000.00) — chegara ichidagi chegirma
        # (3.33% > 1% default, shuning uchun sozlamada 20% ruxsat beramiz).
        st = self.register.settings
        st.max_discount = 20
        st.save()
        p = self.sale_payload()
        p["items"][0]["total"] = 2_500_00
        p["discount_total"] = 500_00
        p["payments"] = [{"method": "naqd", "amount": 2_500_00}]
        r = self.post("/api/v1/sales", p)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(Sale.objects.get().net_total, 2_500_00)

    def test_chegaradan_oshgan_chegirma_rad(self):
        # Default chegara 1% — 99% chegirma yuborsa server RAD etadi.
        p = self.sale_payload()
        p["items"][0]["total"] = 30_00  # 3000.00 tovar 30.00 ga — 99%
        p["payments"] = [{"method": "naqd", "amount": 30_00}]
        r = self.post("/api/v1/sales", p)
        self.assertEqual(r.status_code, 400)
        self.assertIn("chegirma", r.json()["error"].lower())
        self.assertEqual(Sale.objects.count(), 0)

    def test_manager_tokeni_chegirmani_otkazadi(self):
        # Menejer imzolangan tokeni bo'lsa — chegaradan oshsa ham o'tadi.
        from api.auth import make_session_token
        p = self.sale_payload()
        p["items"][0]["total"] = 30_00
        p["payments"] = [{"method": "naqd", "amount": 30_00}]
        token = self.manager_token()
        r = self.post("/api/v1/sales", p, HTTP_X_SESSION=token)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Sale.objects.get().net_total, 30_00)

    def test_takroriy_local_uuid_bitta_sale(self):
        p = self.sale_payload()
        r1 = self.post("/api/v1/sales", p)
        r2 = self.post("/api/v1/sales", p)  # aynan o'sha local_uuid
        self.assertEqual(r1.status_code, 201)
        self.assertIn(r2.status_code, (200, 201))
        self.assertTrue(r2.json().get("duplicate"))
        self.assertEqual(Sale.objects.filter(local_uuid=p["local_uuid"]).count(), 1)

    def test_sale_raqamlari_ketma_ket(self):
        # Ketma-ket cheklar 1,2,3 bo'lib borishi (raqam boshqaruvi to'g'ri).
        nums = []
        for _ in range(3):
            r = self.post("/api/v1/sales", self.sale_payload())
            self.assertEqual(r.status_code, 201)
            nums.append(r.json()["number"])
        self.assertEqual(nums, [1, 2, 3])


from django.test import override_settings

from api.auth import make_session_token, verify_session_token


class ManagerAuthTest(ApiTestCase):
    """Manager-only amal (kassaga pul kiritish) server-side tekshiriladi.
    Mijozdagi `is_manager`'ga ISHONILMAYDI — imzolangan token kerak."""

    def setUp(self):
        super().setUp()
        self.open_shift()

    def cash_op(self, token=None):
        kw = {"HTTP_X_SESSION": token} if token is not None else {}
        return self.post("/api/v1/cash", {"kind": "in", "amount": 100_00}, **kw)

    def test_login_sessiya_token_beradi(self):
        r = self.post("/api/v1/login",
                      {"login": self.register.login, "password": "0000"})
        # parol noto'g'ri bo'lishi mumkin — faqat token maydoni borligini emas,
        # to'g'ri login bilan tekshiramiz:
        self.register.set_password("1234"); self.register.save()
        r = self.post("/api/v1/login",
                      {"login": self.register.login, "password": "1234"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("session", r.json())
        self.assertEqual(self.cash_op(r.json()["session"]).status_code, 403)

    def test_manager_token_bilan_otadi(self):
        r = self.cash_op(self.manager_token())
        self.assertEqual(r.status_code, 201)

    def test_kassir_token_bilan_rad(self):
        r = self.cash_op(make_session_token(7, False))  # is_manager=False
        self.assertEqual(r.status_code, 403)

    def test_soxta_token_rad(self):
        r = self.cash_op("qalbaki.token")
        self.assertEqual(r.status_code, 403)

    def test_tokensiz_har_doim_rad(self):
        r = self.cash_op()
        self.assertEqual(r.status_code, 403)

    @override_settings(REQUIRE_MANAGER_TOKEN=True)
    def test_qatiy_rejimda_tokensiz_rad(self):
        r = self.cash_op()
        self.assertEqual(r.status_code, 403)
