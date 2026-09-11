"""
Bir login — bir vaqtda bitta kompyuter (KassaSession).

Tekshiriladi: kirish loginni kompyuterga biriktiradi; ikkinchi kompyuter
o'sha login bilan kira olmaydi (409, tushunarli xabar); «Chiqish» yoki
3 daqiqa jimlik loginni bo'shatadi; parolsiz davom etish (resume) ham
shu qoidaga bo'ysunadi; hello sessiyani tirik tutadi va tokenni uzaytiradi;
panelda kim kirgani ko'rinadi va «Bo'shatish» ishlaydi.
"""

from __future__ import annotations

import json
from datetime import timedelta
from html import unescape

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from api.auth import verify_session_token
from catalog.models import RetailStore
from sales import sessions
from sales.models import Cashier, KassaSession, Register

PC1 = {"HTTP_X_DEVICE": "dev-1111", "HTTP_X_DEVICE_NAME": "KASSA-PC"}
PC2 = {"HTTP_X_DEVICE": "dev-2222", "HTTP_X_DEVICE_NAME": "OMBOR-PC"}


class SessionBase(TestCase):
    def setUp(self):
        self.store = RetailStore.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000de", name="Chilonzor",
        )
        self.reg = Register.objects.create(
            code="kassa-1", name="Kassa-1", store=self.store, login="kassa1",
        )
        self.reg.set_password("1111")
        self.reg.save()
        self.client = Client()

    def call(self, path, payload=None, headers=None, session=""):
        hdr = {"HTTP_AUTHORIZATION": f"Bearer {self.reg.api_token}"}
        hdr.update(headers or {})
        if session:
            hdr["HTTP_X_SESSION"] = session
        if payload is None:
            return self.client.get(path, **hdr)
        return self.client.post(
            path, data=json.dumps(payload), content_type="application/json", **hdr
        )

    def login(self, pc, login="kassa1", password="1111"):
        return self.call("/api/v1/login", {"login": login, "password": password}, pc)


class LoginExclusiveTest(SessionBase):
    def test_kirish_loginni_kompyuterga_biriktiradi(self):
        r = self.login(PC1)
        self.assertEqual(r.status_code, 200)
        row = KassaSession.objects.get(login="kassa1")
        self.assertEqual((row.device, row.device_name), ("dev-1111", "KASSA-PC"))
        self.assertEqual(row.register, self.reg)
        self.assertEqual(row.cashier_id, 0)

    def test_ikkinchi_kompyuter_kira_olmaydi(self):
        self.login(PC1)
        r = self.login(PC2)
        self.assertEqual(r.status_code, 409)
        msg = r.json()["error"]
        self.assertIn("Kassa-1 · KASSA-PC", msg)
        self.assertIn("«Chiqish»", msg)
        self.assertEqual(r.json()["holder"], "Kassa-1 · KASSA-PC")
        # Egasi o'zgarmadi
        self.assertEqual(KassaSession.objects.get(login="kassa1").device, "dev-1111")

    def test_osha_kompyuter_qayta_kira_oladi(self):
        self.login(PC1)
        self.assertEqual(self.login(PC1).status_code, 200)
        self.assertEqual(KassaSession.objects.count(), 1)

    def test_chiqish_bosilsa_boshqa_kompyuter_kiradi(self):
        self.login(PC1)
        r = self.call("/api/v1/logout", {}, PC1)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["released"], 1)
        self.assertEqual(self.login(PC2).status_code, 200)
        self.assertEqual(KassaSession.objects.get(login="kassa1").device, "dev-2222")

    def test_boshqa_kompyuter_chiqish_bosa_olmaydi(self):
        """PC2 «logout» yuborsa PC1 sessiyasi o'chmaydi."""
        self.login(PC1)
        self.assertEqual(self.call("/api/v1/logout", {}, PC2).json()["released"], 0)
        self.assertEqual(self.login(PC2).status_code, 409)

    def test_jim_qolgan_kompyuter_bloklamaydi(self):
        self.login(PC1)
        KassaSession.objects.filter(login="kassa1").update(
            seen_at=timezone.now() - timedelta(seconds=KassaSession.ALIVE_SECONDS + 5)
        )
        self.assertEqual(self.login(PC2).status_code, 200)
        self.assertEqual(KassaSession.objects.get(login="kassa1").device, "dev-2222")

    def test_notogri_parol_sessiya_yaratmaydi(self):
        self.assertEqual(self.login(PC1, password="0000").status_code, 401)
        self.assertEqual(KassaSession.objects.count(), 0)

    def test_eski_ilova_qurilmasiz_ishlaydi(self):
        """1.15 va eski kassalar X-Device yubormaydi — tekshiruv yo'q."""
        r = self.call("/api/v1/login", {"login": "kassa1", "password": "1111"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(KassaSession.objects.count(), 0)

    def test_kassir_hisobi_ham_bir_joyda(self):
        c = Cashier(name="Nilufar", login="nilufar")
        c.set_pin("2222")
        c.save()
        self.assertEqual(self.login(PC1, "nilufar", "2222").status_code, 200)
        row = KassaSession.objects.get(login="nilufar")
        self.assertEqual((row.cashier_id, row.cashier_name), (c.pk, "Nilufar"))
        r = self.login(PC2, "nilufar", "2222")
        self.assertEqual(r.status_code, 409)
        self.assertIn("(Nilufar)", r.json()["error"])


class ResumeTest(SessionBase):
    def test_parolsiz_davom_etish(self):
        self.login(PC1)
        r = self.call("/api/v1/session/resume", {"cashier_id": 0}, PC1)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["cashier"]["login"], "kassa1")
        self.assertTrue(verify_session_token(r.json()["session"])["is_manager"])

    def test_bosh_kompyuter_ham_davom_eta_oladi(self):
        """Server qayta o'rnatilib sessiya jadvali bo'sh bo'lsa ham kassa
        (tokeni bor) parolsiz davom etadi — kassir qayta login qilmaydi."""
        r = self.call("/api/v1/session/resume", {"cashier_id": 0}, PC1)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(KassaSession.objects.get(login="kassa1").device, "dev-1111")

    def test_boshqa_kompyuter_olib_qoygan_bolsa_409(self):
        self.login(PC2)
        r = self.call("/api/v1/session/resume", {"cashier_id": 0}, PC1)
        self.assertEqual(r.status_code, 409)
        self.assertIn("OMBOR-PC", r.json()["error"])

    def test_ochirilgan_kassir_davom_eta_olmaydi(self):
        c = Cashier(name="N", login="n", active=False)
        c.set_pin("1")
        c.save()
        r = self.call("/api/v1/session/resume", {"cashier_id": c.pk}, PC1)
        self.assertEqual(r.status_code, 401)


class HelloSessionTest(SessionBase):
    def test_hello_sessiyani_tirik_tutadi_va_tokenni_uzaytiradi(self):
        token = self.login(PC1).json()["session"]
        KassaSession.objects.filter(login="kassa1").update(
            seen_at=timezone.now() - timedelta(seconds=100)
        )
        r = self.call("/api/v1/hello", headers=PC1, session=token)
        self.assertEqual(r.status_code, 200)
        ls = r.json()["login_session"]
        self.assertTrue(ls["mine"])
        self.assertTrue(verify_session_token(ls["session"]))
        row = KassaSession.objects.get(login="kassa1")
        self.assertLess((timezone.now() - row.seen_at).total_seconds(), 5)

    def test_hello_sessiyasiz_tirik_tutmaydi(self):
        """«Chiqish»dan keyin kassa X-Session yubormaydi — seen_at o'zgarmaydi,
        3 daqiqadan keyin login o'zi bo'shaydi."""
        self.login(PC1)
        old = timezone.now() - timedelta(seconds=100)
        KassaSession.objects.filter(login="kassa1").update(seen_at=old)
        r = self.call("/api/v1/hello", headers=PC1)
        self.assertIsNone(r.json()["login_session"]["mine"])
        self.assertEqual(KassaSession.objects.get(login="kassa1").seen_at, old)

    def test_boshqa_kompyuter_olib_qoysa_mine_false(self):
        token = self.login(PC1).json()["session"]
        # PC1 jim qoldi, PC2 kirdi
        KassaSession.objects.filter(login="kassa1").update(
            seen_at=timezone.now() - timedelta(seconds=KassaSession.ALIVE_SECONDS + 5)
        )
        self.login(PC2)
        r = self.call("/api/v1/hello", headers=PC1, session=token)
        ls = r.json()["login_session"]
        self.assertFalse(ls["mine"])
        self.assertEqual(ls["holder"], "Kassa-1 · OMBOR-PC")
        self.assertIn("OMBOR-PC", ls["message"])
        self.assertNotIn("session", ls)

    def test_eski_ilova_hello_mine_none(self):
        r = self.call("/api/v1/hello")
        self.assertEqual(r.json()["login_session"], {"mine": None, "holder": ""})


class PanelSessionTest(SessionBase):
    def setUp(self):
        super().setUp()
        User.objects.create_superuser("t_admin", "t@a.uz", "pw12345")
        self.web = Client()
        self.web.login(username="t_admin", password="pw12345")

    def test_kassalar_sahifasida_kim_kirgani(self):
        self.login(PC1)
        html = unescape(self.web.get("/kassalar/").content.decode())
        self.assertIn("Kim kirgan", html)
        self.assertIn("KASSA-PC", html)
        self.assertIn("Bo'shatish", html)

    def test_boshatish_tugmasi(self):
        self.login(PC1)
        r = self.web.post("/kassalar/", {"action": "release", "id": self.reg.pk})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(KassaSession.objects.count(), 0)
        self.assertEqual(self.login(PC2).status_code, 200)

    def test_jim_qolgan_sessiya_korinmaydi(self):
        self.login(PC1)
        KassaSession.objects.filter(login="kassa1").update(
            seen_at=timezone.now() - timedelta(seconds=KassaSession.ALIVE_SECONDS + 5)
        )
        html = unescape(self.web.get("/kassalar/").content.decode())
        self.assertNotIn("KASSA-PC", html)


class SessionsModuleTest(SessionBase):
    def test_state_for(self):
        now = timezone.now()
        self.assertEqual(sessions.state_for(self.reg, "dev-1111")["mine"], None)
        sessions.acquire("kassa1", self.reg, "dev-1111", "PC")
        self.assertTrue(sessions.state_for(self.reg, "dev-1111", now=now)["mine"])
        self.assertFalse(sessions.state_for(self.reg, "dev-2222", now=now)["mine"])
        late = now + timedelta(seconds=KassaSession.ALIVE_SECONDS + 1)
        self.assertIsNone(sessions.state_for(self.reg, "dev-2222", now=late)["mine"])

    def test_touch_30_soniyada_bir(self):
        sessions.acquire("kassa1", self.reg, "dev-1111", "PC")
        row = KassaSession.objects.get()
        first = row.seen_at
        sessions.touch(self.reg, "dev-1111")  # hali erta — yozilmaydi
        self.assertEqual(KassaSession.objects.get().seen_at, first)
        KassaSession.objects.update(seen_at=first - timedelta(seconds=31))
        sessions.touch(self.reg, "dev-1111")
        self.assertGreater(KassaSession.objects.get().seen_at, first - timedelta(seconds=31))

    def test_qurilmasiz_hech_narsa(self):
        self.assertIsNone(sessions.acquire("kassa1", self.reg, "", ""))
        self.assertIsNone(sessions.touch(self.reg, ""))
        self.assertEqual(sessions.release(self.reg, ""), 0)
