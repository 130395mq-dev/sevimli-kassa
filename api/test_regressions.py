"""Regression scenarios for money, shift ownership and session boundaries."""
from datetime import timedelta
from django.utils import timezone
from django.test import RequestFactory
from api.tests import ApiTestCase
from api import pricing
from sales.models import Sale, Shift, Register, Cashier
from api.session_security import verify

class ReceiptSafetyTest(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.open_shift()

    def test_low_price_cannot_bypass_discount_limit(self):
        p = self.sale_payload()
        p["items"][0].update(price=100, total=100)
        p["payments"] = [{"method": "naqd", "amount": 100}]
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)
        self.assertFalse(Sale.objects.exists())

    def test_signed_offline_price_survives_catalog_change(self):
        p = self.sale_payload()
        p["items"][0]["price_quote"] = pricing.quote(self.product, self.register)
        self.product.sale_price += 10000
        self.product.save()
        r = self.post("/api/v1/sales", p)
        self.assertEqual(r.status_code, 201, r.content)

    def test_tampered_quote_is_rejected(self):
        p = self.sale_payload()
        p["items"][0]["price_quote"] = pricing.quote(self.product, self.register) + "broken"
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)

    def test_receipt_cannot_move_to_new_shift(self):
        old = Shift.objects.get()
        p = self.sale_payload()
        p["shift_id"] = old.pk
        self.post("/api/v1/shift/close", {})
        self.open_shift()
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 409)
        self.assertFalse(Sale.objects.exists())

    def test_legacy_receipt_cannot_move_to_new_shift(self):
        p = self.sale_payload()
        self.post("/api/v1/shift/close", {})
        self.open_shift()
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 409)

    def test_foreign_shift_rejected(self):
        other = Register.objects.create(code="other", name="Other", store=self.store)
        sh = Shift.objects.create(register=other, number=1, cashier="Other",
                                  opened_at=timezone.now(), opening_cash=0)
        p = self.sale_payload(shift_id=sh.pk)
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 409)

    def test_duplicate_uuid_does_not_leak_other_register(self):
        p = self.sale_payload()
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 201)
        other = Register.objects.create(code="other", name="Other", store=self.store)
        self.assertEqual(self.post("/api/v1/sales", p,
            HTTP_AUTHORIZATION="Bearer " + other.api_token).status_code, 409)

class ManagerBoundaryTest(ApiTestCase):
    def test_token_cannot_be_used_on_another_register(self):
        token = self.manager_token()
        other = Register.objects.create(code="other", name="Other", store=self.store)
        req = RequestFactory().get("/", HTTP_X_SESSION=token)
        req.register = other
        self.assertIsNone(verify(req))

    def test_demoted_manager_loses_permission_immediately(self):
        token = self.manager_token()
        Cashier.objects.filter(login="test-manager").update(is_manager=False)
        self.open_shift()
        self.assertEqual(self.post("/api/v1/cash", {"kind":"out","amount":100},
            HTTP_X_SESSION=token).status_code, 403)

    def test_resume_requires_session_proof(self):
        self.register.login = "cash"
        self.register.save()
        self.assertEqual(self.post("/api/v1/session/resume", {"cashier_id":0},
            HTTP_X_DEVICE="untrusted").status_code, 401)
