"""Regression scenarios for money, shift ownership and session boundaries."""
import json
from datetime import timedelta
from django.utils import timezone
from django.test import RequestFactory
from api.tests import ApiTestCase
from api import pricing
from sales.models import CashOperation, Sale, Shift, Register, Cashier
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


class PushSaleNowSingleWriterTest(ApiTestCase):
    """Bitta chekni bir vaqtda ikki so'rov MoySklad'ga yozmasin (2026-09-16).

    Kassa chekni ikki marta ketma-ket yuborganda ikkinchi so'rov POST
    qilmasdan birinchisining natijasini kutadi. Cron ham band qilingan
    chekka 60 soniya tegmaydi."""

    def setUp(self):
        super().setUp()
        # _push_sale_now oxirida connection.close() qiladi (gunicorn uchun
        # to'g'ri). Test tranzaksiyasi ichida bu ulanishni uzib qo'yadi
        # (Postgres: «the connection is closed») — testda o'chiramiz.
        from unittest.mock import patch
        p = patch("django.db.connection.close", lambda: None)
        p.start(); self.addCleanup(p.stop)

    def _new_sale(self):
        self.open_shift()
        from unittest.mock import patch
        with patch("api.views._push_sale_now", return_value=None):
            r = self.post("/api/v1/sales", self.sale_payload())
        self.assertEqual(r.status_code, 201, r.content)
        return Sale.objects.get(pk=r.json()["id"])

    def test_yozadi_va_raqamni_qaytaradi(self):
        from unittest.mock import patch
        from django.test import override_settings
        from api.views import _push_sale_now

        sale = self._new_sale()
        calls = []

        def fake_send(self_, s):
            calls.append(s.pk)
            Sale.objects.filter(pk=s.pk).update(receipt_number="1163")
            return {}

        with override_settings(MOYSKLAD_TOKEN="t"), \
                patch("sales.writer.SaleWriter.send", fake_send), \
                patch("moysklad.client.MoySkladClient.__init__", return_value=None):
            self.assertEqual(_push_sale_now(sale.pk, wait=0), "1163")
        sale.refresh_from_db()
        self.assertEqual(calls, [sale.pk])
        self.assertEqual(sale.sync_status, Sale.SENT)
        self.assertIsNone(sale.next_attempt_at)

    def test_band_qilingan_chek_ikkinchi_marta_yozilmaydi(self):
        from unittest.mock import patch
        from django.test import override_settings
        from api.views import _push_sale_now

        sale = self._new_sale()
        # Boshqa so'rov hozir yozyapti: band (next_attempt_at kelajakda)
        Sale.objects.filter(pk=sale.pk).update(
            next_attempt_at=timezone.now() + timedelta(seconds=60))
        with override_settings(MOYSKLAD_TOKEN="t"), \
                patch("sales.writer.SaleWriter.send") as send:
            self.assertIsNone(_push_sale_now(sale.pk, wait=0))
            send.assert_not_called()

        # Birinchisi tugagan bo'lsa — POST qilmasdan tayyor raqam qaytadi
        Sale.objects.filter(pk=sale.pk).update(
            sync_status=Sale.SENT, receipt_number="1164", next_attempt_at=None)
        with override_settings(MOYSKLAD_TOKEN="t"), \
                patch("sales.writer.SaleWriter.send") as send:
            self.assertEqual(_push_sale_now(sale.pk, wait=0), "1164")
            send.assert_not_called()

    def test_xato_bolsa_cron_60s_dan_keyin_oladi(self):
        from unittest.mock import patch
        from django.test import override_settings
        from api.views import _push_sale_now
        from sales.sender import due_exists

        sale = self._new_sale()
        with override_settings(MOYSKLAD_TOKEN="t"), \
                patch("sales.writer.SaleWriter.send", side_effect=RuntimeError("MoySklad yo'q")), \
                patch("moysklad.client.MoySkladClient.__init__", return_value=None):
            self.assertIsNone(_push_sale_now(sale.pk, wait=0))
        sale.refresh_from_db()
        self.assertEqual(sale.sync_status, Sale.NEW)          # yo'qolmadi
        self.assertFalse(due_exists())                         # hozir cron tegmaydi
        self.assertTrue(due_exists(timezone.now() + timedelta(seconds=61)))  # keyin oladi


class ReceiptAfterShiftClosedTest(ApiTestCase):
    """Yopilgan smenadan KEYIN urilgan chek o'sha smenaga tushmasligi kerak.

    Haqiqiy holat (18.09.2026, kasssa2): bitta login ikkita kompyuterda
    ishlagan. Ikkinchi kompyuter smenani yopib o'zinikini ochgan, birinchisi
    esa eski smena id si bilan sotishda davom etgan — 125 ta chek,
    9 288 008 so'm yopilgan smenaga tushib, kechki smena cheki shuncha kam
    ko'rsatgan.
    """

    def setUp(self):
        super().setUp()
        self.open_shift()
        self.old = Shift.objects.get()

    def _close_old(self, hours_ago=3):
        self.old.opened_at = timezone.now() - timedelta(hours=hours_ago + 5)
        self.old.closed_at = timezone.now() - timedelta(hours=hours_ago)
        self.old.status = Shift.CLOSED
        self.old.save(update_fields=["opened_at", "closed_at", "status"])

    def _post(self, hours_ago):
        payload = self.sale_payload(
            shift_id=self.old.pk,
            created_at=(timezone.now() - timedelta(hours=hours_ago)).isoformat(),
        )
        response = self.post("/api/v1/sales", payload)
        self.assertEqual(response.status_code, 201, response.content)
        return Sale.objects.get()

    def test_sale_made_after_close_lands_in_the_open_shift(self):
        self._close_old(hours_ago=3)
        self.open_shift()
        new = Shift.objects.get(status=Shift.OPEN)
        sale = self._post(hours_ago=1)
        self.assertEqual(sale.shift_id, new.pk)
        self.assertFalse(sale.late)

    def test_receipt_made_during_the_shift_still_belongs_to_it(self):
        self._close_old(hours_ago=3)
        self.open_shift()
        sale = self._post(hours_ago=4)
        self.assertEqual(sale.shift_id, self.old.pk)
        self.assertTrue(sale.late)

    def test_without_an_open_shift_the_receipt_is_not_lost(self):
        self._close_old(hours_ago=3)
        sale = self._post(hours_ago=1)
        self.assertEqual(sale.shift_id, self.old.pk)
        self.assertTrue(sale.late)

    def test_clock_skew_does_not_move_a_receipt(self):
        """Kassa soati bir-ikki daqiqaga oldinda bo'lsa ham ko'chirilmaydi."""
        self.old.closed_at = timezone.now()
        self.old.status = Shift.CLOSED
        self.old.save(update_fields=["closed_at", "status"])
        self.open_shift()
        payload = self.sale_payload(
            shift_id=self.old.pk,
            created_at=(timezone.now() + timedelta(minutes=2)).isoformat(),
        )
        self.assertEqual(self.post("/api/v1/sales", payload).status_code, 201)
        self.assertEqual(Sale.objects.get().shift_id, self.old.pk)


class OneRegisterOneComputerTest(ApiTestCase):
    """Bitta kassa — bitta kompyuter.

    Kassa tokeni birinchi ulangan kompyuterga biriktiriladi. Xuddi shu
    tokenni ikkinchi kompyuterga ko'chirib ishlatib bo'lmaydi: 18.09.2026
    da aynan shu tufayli bitta login ikkita kompyuterda ishlagan va
    smenalar aralashib ketgan.
    """

    def hello(self, device="", name=""):
        extra = {}
        if device:
            extra["HTTP_X_DEVICE"] = device
        if name:
            extra["HTTP_X_DEVICE_NAME"] = name
        return self.client.get("/api/v1/hello", **{**self.auth(), **extra})

    def test_first_computer_is_bound_and_keeps_working(self):
        self.assertEqual(self.hello("pos-1", "POS-1").status_code, 200)
        self.register.refresh_from_db()
        self.assertEqual(self.register.device, "pos-1")
        self.assertEqual(self.register.device_name, "POS-1")
        self.assertIsNotNone(self.register.device_bound_at)
        self.assertEqual(self.hello("pos-1").status_code, 200)

    def test_second_computer_with_the_same_token_is_refused(self):
        self.hello("pos-1", "POS-1")
        response = self.hello("pos-2", "POS-2")
        self.assertEqual(response.status_code, 401)
        self.assertIn("POS-1", response.json()["error"])
        self.register.refresh_from_db()
        self.assertEqual(self.register.device, "pos-1")

    def test_second_computer_cannot_write_a_sale(self):
        self.hello("pos-1")
        self.open_shift()
        payload = self.sale_payload()
        refused = self.post("/api/v1/sales", payload, HTTP_X_DEVICE="pos-2")
        self.assertEqual(refused.status_code, 401)
        self.assertFalse(Sale.objects.exists())
        self.assertEqual(
            self.post("/api/v1/sales", payload, HTTP_X_DEVICE="pos-1").status_code, 201
        )

    def test_login_and_password_move_the_register_to_a_new_computer(self):
        self.hello("pos-1", "POS-1")
        self.register.set_password("maxfiy")
        self.register.login = "kassa-1"
        self.register.save(update_fields=["password_hash", "login"])
        moved = self.client.post(
            "/api/v1/connect",
            data=json.dumps({"login": "kassa-1", "password": "maxfiy"}),
            content_type="application/json",
            HTTP_X_DEVICE="pos-2", HTTP_X_DEVICE_NAME="POS-2",
        )
        self.assertEqual(moved.status_code, 200)
        self.register.refresh_from_db()
        self.assertEqual(self.register.device, "pos-2")
        self.assertEqual(self.hello("pos-2").status_code, 200)
        self.assertEqual(self.hello("pos-1").status_code, 401)

    def test_old_app_without_the_header_is_not_locked_out(self):
        self.assertEqual(self.hello().status_code, 200)
        self.register.refresh_from_db()
        self.assertEqual(self.register.device, "")
        self.hello("pos-1")
        self.assertEqual(self.hello().status_code, 200)

    def test_panel_can_release_the_computer(self):
        self.hello("pos-1", "POS-1")
        self.register.device = ""
        self.register.device_name = ""
        self.register.save(update_fields=["device", "device_name"])
        self.assertEqual(self.hello("pos-2", "POS-2").status_code, 200)
        self.register.refresh_from_db()
        self.assertEqual(self.register.device, "pos-2")


class OfflineShiftCloseTest(ApiTestCase):
    """Internetsiz yopilgan smena keyin serverga kelganda.

    Kassada internet kun bo'yi bo'lmasa kassir smenani o'zida yopadi va
    pulni topshiradi. Ulanish tiklanganda smena shu yerga keladi — HAQIQIY
    yopilish vaqti bilan va aynan o'sha smena yopilishi kerak.
    """

    def setUp(self):
        super().setUp()
        self.open_shift()
        self.shift = Shift.objects.get()
        self.shift.local_uuid = "11111111-1111-1111-1111-111111111111"
        self.shift.opened_at = timezone.now() - timedelta(hours=12)
        self.shift.save(update_fields=["local_uuid", "opened_at"])

    def test_real_close_time_is_kept(self):
        closed = timezone.now() - timedelta(hours=3)
        r = self.post("/api/v1/shift/close", {
            "closed_at": closed.isoformat(),
            "local_uuid": self.shift.local_uuid,
        })
        self.assertEqual(r.status_code, 200, r.content)
        self.shift.refresh_from_db()
        self.assertEqual(self.shift.status, Shift.CLOSED)
        self.assertEqual(int(self.shift.closed_at.timestamp()),
                         int(closed.timestamp()))

    def test_repeated_close_is_not_an_error(self):
        """Javob yo'lda yo'qolsa kassa qayta yuboradi — xato bo'lmasin."""
        body = {"closed_at": timezone.now().isoformat(),
                "local_uuid": self.shift.local_uuid}
        self.assertEqual(self.post("/api/v1/shift/close", body).status_code, 200)
        again = self.post("/api/v1/shift/close", body)
        self.assertEqual(again.status_code, 200, again.content)
        self.assertIn("receipt_text", json.loads(again.content))

    def test_a_newer_shift_is_not_closed_by_mistake(self):
        """Eski smenaning yopilishi yangi ochilganini yopib qo'ymasin."""
        self.post("/api/v1/shift/close",
                  {"local_uuid": self.shift.local_uuid,
                   "closed_at": timezone.now().isoformat()})
        self.open_shift()
        new = Shift.objects.get(status=Shift.OPEN)
        # Kassa eski smenani qayta yuborib yubordi
        r = self.post("/api/v1/shift/close",
                      {"local_uuid": self.shift.local_uuid,
                       "closed_at": timezone.now().isoformat()})
        self.assertEqual(r.status_code, 200)
        new.refresh_from_db()
        self.assertEqual(new.status, Shift.OPEN)

    def test_unknown_shift_is_refused(self):
        r = self.post("/api/v1/shift/close",
                      {"local_uuid": "22222222-2222-2222-2222-222222222222"})
        self.assertEqual(r.status_code, 409)

    def test_broken_clock_falls_back_to_now(self):
        """Kassa soati adashsa hisobot vaqti buzilmasin."""
        r = self.post("/api/v1/shift/close", {
            "closed_at": (timezone.now() + timedelta(days=2)).isoformat(),
            "local_uuid": self.shift.local_uuid,
        })
        self.assertEqual(r.status_code, 200)
        self.shift.refresh_from_db()
        self.assertLess(self.shift.closed_at, timezone.now() + timedelta(minutes=1))

    def test_cash_operation_keeps_the_time_it_was_made(self):
        made = timezone.now() - timedelta(hours=6)
        r = self.post("/api/v1/cash", {
            "kind": "out", "amount": 1000000,
            "local_uuid": "33333333-3333-3333-3333-333333333333",
            "created_at": made.isoformat(),
        }, HTTP_X_SESSION=self.manager_token())
        self.assertEqual(r.status_code, 201, r.content)
        op = CashOperation.objects.get()
        self.assertEqual(int(op.created_at.timestamp()), int(made.timestamp()))
