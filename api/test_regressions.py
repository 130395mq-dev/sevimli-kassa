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

    def test_stale_quote_with_current_central_price_is_accepted(self):
        p = self.sale_payload()
        p["items"][0]["price_quote"] = pricing.quote(self.product, self.register) + "broken"
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 201)

    def test_stale_quote_cannot_authorize_forged_price(self):
        p = self.sale_payload()
        p["items"][0].update(price=100, total=100,
                             price_quote="old-or-tampered-signature")
        p["payments"] = [{"method": "naqd", "amount": 100}]
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 400)

    def test_receipt_cannot_move_to_new_shift(self):
        old = Shift.objects.get()
        p = self.sale_payload()
        p["shift_id"] = old.pk
        self.post("/api/v1/shift/close", {})
        self.open_shift()
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 201)
        sale = Sale.objects.get()
        self.assertEqual(sale.shift_id, old.pk)
        self.assertTrue(sale.late)

    def test_legacy_receipt_cannot_move_to_new_shift(self):
        old = Shift.objects.get()
        p = self.sale_payload()
        self.post("/api/v1/shift/close", {})
        self.open_shift()
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 201)
        sale = Sale.objects.get()
        self.assertEqual(sale.shift_id, old.pk)
        self.assertTrue(sale.late)

    def test_foreign_shift_rejected(self):
        other = Register.objects.create(code="other", name="Other", store=self.store)
        sh = Shift.objects.create(register=other, number=1, cashier="Other",
                                  opened_at=timezone.now(), opening_cash=0)
        p = self.sale_payload(shift_id=sh.pk)
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 409)
        self.assertFalse(Sale.objects.exists())

    def test_unknown_explicit_shift_cannot_fall_back_to_current(self):
        from uuid import uuid4
        for binding in ({"shift_id": 999999}, {"shift_local_uuid": str(uuid4())}):
            with self.subTest(binding=binding):
                p = self.sale_payload(**binding)
                self.assertEqual(self.post("/api/v1/sales", p).status_code, 409)
                self.assertFalse(Sale.objects.exists())

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


class ReturnableOnlyOpenShiftTest(ApiTestCase):
    """Qaytarish ro'yxatida FAQAT ochiq smena cheklari (egasining talabi)."""

    def _returnable_ids(self):
        r = self.client.get("/api/v1/sales/returnable",
                            HTTP_AUTHORIZATION="Bearer " + self.register.api_token)
        self.assertEqual(r.status_code, 200, r.content)
        return [s["id"] for s in r.json()["sales"]]

    def test_yopiq_smena_cheki_royxatda_korinmaydi(self):
        self.open_shift()
        r = self.post("/api/v1/sales", self.sale_payload())
        self.assertEqual(r.status_code, 201, r.content)
        old_sale = Sale.objects.get()
        self.assertIn(old_sale.pk, self._returnable_ids())   # ochiq smena — ko'rinadi

        self.post("/api/v1/shift/close", {})
        self.open_shift()
        r = self.post("/api/v1/sales", self.sale_payload())
        self.assertEqual(r.status_code, 201, r.content)
        new_sale = Sale.objects.exclude(pk=old_sale.pk).get()

        ids = self._returnable_ids()
        self.assertIn(new_sale.pk, ids)       # hozirgi smena — ko'rinadi
        self.assertNotIn(old_sale.pk, ids)    # yopiq smena — KO'RINMAYDI

    def test_sozlama_yoqilsa_yopiq_smena_ham_korinadi(self):
        st = self.register.settings
        st.allow_returns_closed_shift = True
        st.save()
        self.open_shift()
        self.post("/api/v1/sales", self.sale_payload())
        old_sale = Sale.objects.get()
        self.post("/api/v1/shift/close", {})
        self.open_shift()
        self.assertIn(old_sale.pk, self._returnable_ids())
