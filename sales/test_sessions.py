"""
Bir login — bir vaqtda bitta kompyuter (KassaSession).

Tekshiriladi: kirish loginni kompyuterga biriktiradi; ikkinchi kompyuter
o'sha login bilan kira olmaydi (409, tushunarli xabar); «Chiqish» yoki
3 daqiqa jimlik loginni bo'shatadi; parolsiz davom etish (resume) ham
shu qoidaga bo'ysunadi; hello sessiyani tirik tutadi va tokenni uzaytiradi;
panelda kim kirgani ko'rinadi va «Bo'shatish» ishlaydi.

2026-09-19 dan beri ustida yana bir qatlam bor: kassa TOKENI birinchi
ulangan kompyuterga biriktiriladi (`Register.device`). Shuning uchun
ikkinchi kompyuter bu testlarda avval 401 oladi — sessiya qatlamiga
yetib ham bormaydi. Sessiya qatlamini sinash uchun testlar `unbind()`
bilan biriktirishni bo'shatadi (panelda «Kompyuterni bo'shatish» shuni
qiladi).
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

    def unbind(self):
        """Panelda «Kompyuterni bo'shatish» bosilgandek — kassa endi
        istalgan kompyuterda ochiladi."""
        Register.objects.filter(pk=self.reg.pk).update(device="", device_name="")


class LoginExclusiveTest(SessionBase):
    def test_kirish_loginni_kompyuterga_biriktiradi(self):
        r = self.login(PC1)
        self.assertEqual(r.status_code, 200)
        row = KassaSession.objects.get(login="kassa1")
        self.assertEqual((row.device, row.device_name), ("dev-1111", "KASSA-PC"))
        self.assertEqual(row.register, self.reg)
        self.assertEqual(row.cashier_id, 0)

    def test_ikkinchi_kompyuter_kira_olmaydi(self):
        """Kassa PC1 ga biriktirilgan — PC2 eshikdan ham o'tmaydi."""
        self.login(PC1)
        r = self.login(PC2)
        self.assertEqual(r.status_code, 401)
        self.assertIn("biriktirilgan", r.json()["error"])
        self.assertEqual(KassaSession.objects.get(login="kassa1").device, "dev-1111")

    def test_boshatilgan_kassada_login_band_bolsa_409(self):
        """Biriktirish bo'shatilgan bo'lsa ham login bandligi tekshiriladi."""
        self.login(PC1)
        self.unbind()
        r = self.login(PC2)
        self.assertEqual(r.status_code, 409)
        msg = r.json()["error"]
        self.assertIn("Kassa-1 · KASSA-PC", msg)
        self.assertIn("«Chiqish»", msg)
        self.assertEqual(r.json()["holder"], "Kassa-1 · KASSA-PC")
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
        self.unbind()     # kassa boshqa monoblokka ko'chirildi
        self.assertEqual(self.login(PC2).status_code, 200)
        self.assertEqual(KassaSession.objects.get(login="kassa1").device, "dev-2222")

    def test_boshqa_kompyuter_chiqish_bosa_olmaydi(self):
        """PC2 «logout» yuborsa PC1 sessiyasi o'chmaydi."""
        self.login(PC1)
        self.assertEqual(self.call("/api/v1/logout", {}, PC2).status_code, 401)
        self.assertEqual(KassaSession.objects.get(login="kassa1").device, "dev-1111")
        self.unbind()
        self.assertEqual(self.call("/api/v1/logout", {}, PC2).json()["released"], 0)
        self.assertEqual(KassaSession.objects.get(login="kassa1").device, "dev-1111")

    def test_jim_qolgan_kompyuter_bloklamaydi(self):
        self.login(PC1)
        self.unbind()
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
        self.unbind()
        r = self.login(PC2, "nilufar", "2222")
        self.assertEqual(r.status_code, 409)
        self.assertIn("(Nilufar)", r.json()["error"])


class ResumeTest(SessionBase):
    def test_parolsiz_davom_etish(self):
        token = self.login(PC1).json()["session"]
        r = self.call("/api/v1/session/resume", {"cashier_id": 0}, PC1, token)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["cashier"]["login"], "kassa1")
        self.assertFalse(r.json()["cashier"]["is_manager"])

    def test_bosh_kompyuter_davom_eta_olmaydi(self):
        r = self.call("/api/v1/session/resume", {"cashier_id": 0}, PC1)
        self.assertEqual(r.status_code, 401)
        self.assertFalse(KassaSession.objects.exists())

    def test_boshqa_qurilmada_token_ishlamaydi(self):
        token = self.login(PC1).json()["session"]
        r = self.call("/api/v1/session/resume", {"cashier_id": 0}, PC2, token)
        self.assertEqual(r.status_code, 401)

    def test_logout_tokenni_bekor_qiladi(self):
        token = self.login(PC1).json()["session"]
        self.call("/api/v1/logout", {}, PC1)
        self.assertEqual(self.call("/api/v1/session/resume",
            {"cashier_id": 0}, PC1, token).status_code, 401)

    def test_kassir_id_almashtirish_rad(self):
        token = self.login(PC1).json()["session"]
        self.assertEqual(self.call("/api/v1/session/resume",
            {"cashier_id": 999}, PC1, token).status_code, 401)


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
        self.assertEqual(self.call("/api/v1/session/resume",
            {"cashier_id": 0}, PC1, ls["session"]).status_code, 200)
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
        # PC1 jim qoldi, kassa bo'shatilib PC2 ga o'tdi
        KassaSession.objects.filter(login="kassa1").update(
            seen_at=timezone.now() - timedelta(seconds=KassaSession.ALIVE_SECONDS + 5)
        )
        self.unbind()
        self.login(PC2)
        # Endi kassa PC2 niki — PC1 umuman kira olmaydi (401), shuning
        # uchun «mine: false» ekranini ko'rishga ham ulgurmaydi.
        r = self.call("/api/v1/hello", headers=PC1, session=token)
        self.assertEqual(r.status_code, 401)
        self.assertIn("OMBOR-PC", r.json()["error"])

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
        # Login bo'shadi, lekin kassa hali PC1 ga biriktirilgan
        self.assertEqual(self.login(PC2).status_code, 401)
        r = self.web.post("/kassalar/", {"action": "unbind", "id": self.reg.pk})
        self.assertEqual(r.status_code, 302)
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.device, "")
        self.assertEqual(self.login(PC2).status_code, 200)

    def test_jim_qolgan_sessiya_korinmaydi(self):
        self.login(PC1)
        KassaSession.objects.filter(login="kassa1").update(
            seen_at=timezone.now() - timedelta(seconds=KassaSession.ALIVE_SECONDS + 5)
        )
        html = unescape(self.web.get("/kassalar/").content.decode())
        # Kompyuter nomi sahifada baribir bor — kassa o'sha kompyuterga
        # BIRIKTIRILGAN («Kompyuter: KASSA-PC»). Bu yerda tekshirilayotgani
        # boshqa narsa: jim qolgan sessiya «kim kirgan» ustunida
        # ko'rsatilmasligi kerak («· KASSA-PC · 08:00 dan» qatori).
        self.assertNotIn("\u00b7 KASSA-PC \u00b7", html)


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
