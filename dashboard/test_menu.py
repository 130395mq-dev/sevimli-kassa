"""Tepa menyu (dashboard/menu.py): bo'limlar, tanlangan band va rollar.

Eng muhimi — menyu hech qachon foydalanuvchiga yopiq (403) sahifaga havola bermasligi:
ruxsatlar dashboard/access.py da, menyu ularni o'sha yerdan oladi."""

from __future__ import annotations

import re
import uuid

from django.contrib.auth.models import User
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from dashboard import access, menu
from kpi.models import Tovaroved
from sales.models import PanelManager


def _nav(html: str) -> tuple[list[str], list[str], list[str], list[str]]:
    """(bo'limlar, tanlangan bo'lim, ikkinchi qator, ikkinchi qatorda tanlangan)."""
    main = re.search(r'<nav class="mainnav".*?</nav>', html, flags=re.S).group(0)
    items = re.findall(r'<a class="item( on)?" href="([^"]+)"[^>]*>.*?<span class="txt">(.*?)</span>', main, flags=re.S)
    sub = re.search(r'<nav class="subnav".*?</nav>', html, flags=re.S)
    subs = re.findall(r'<a( class="on")?[^>]* href="[^"]+">(.*?)</a>', sub.group(0), flags=re.S) if sub else []
    return ([t for _, _, t in items], [t for on, _, t in items if on],
            [t for _, t in subs], [t for on, t in subs if on])


def _links(html: str) -> list[str]:
    out = []
    for nav in re.findall(r'<nav class="(?:mainnav|subnav)".*?</nav>', html, flags=re.S):
        out += re.findall(r'href="([^"#]+)"', nav)
    return sorted(set(out))


class TuzilmaTest(SimpleTestCase):
    def test_hamma_band_haqiqiy_sahifa(self):
        for sec in (*menu.MENU, *menu.TOVAROVED_MENU):
            for it in sec.items:
                with self.subTest(band=it.title):
                    self.assertTrue(reverse(it.name).startswith("/"))
                    for extra in it.also:
                        self.assertIn(":", extra)

    def test_royxatlar_ruxsatlarga_mos(self):
        # Tovaroved menyusidagi hamma band unga ochiq sahifa
        for sec in menu.TOVAROVED_MENU:
            for it in sec.items:
                self.assertIn(it.name, access.TOVAROVED_PAGES)
        # Boshqaruvchiga ochiq sahifalarning hech biri menyusiz qolmagan (yordamchilaridan tashqari)
        nomlar = {n for sec in menu.MENU for it in sec.items for n in (it.name, *it.also)}
        yordamchi = {"login", "logout", "dashboard:aloqa-json", "dashboard:health"}
        self.assertEqual(access.MANAGER_PAGES - nomlar - yordamchi, set())

    def test_aniqlanmagan_manzil_yiqitmaydi(self):
        request = RequestFactory().get("/bunday-sahifa-yoq/")
        m = menu.build(request)
        self.assertEqual([s["on"] for s in m["sections"]], [False] * len(m["sections"]))
        self.assertEqual(m["sub"], [])


class EgasiTest(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_superuser("egasi", password="x"))

    def test_bolimlar_va_tanlangan_band(self):
        kutilgan = [
            ("/", "Bosh sahifa", [], []),
            ("/smenalar/", "Savdo", ["Smenalar", "Cheklar"], ["Smenalar"]),
            ("/cheklar/", "Savdo", ["Smenalar", "Cheklar"], ["Cheklar"]),
            ("/tovarlar/", "Tovarlar", None, ["Top tovarlar"]),
            ("/tovarlar/tugayotgan/", "Tovarlar", None, ["Tugayotganlar"]),
            ("/tovarlar/tushgan/", "Tovarlar", None, ["Tushgan tovarlar"]),
            ("/tovarlar/kochirish/", "Tovarlar", None, ["Ko'chirish tavsiyasi"]),
            ("/narxlar/", "Tovarlar", None, ["Sotuv narxi"]),
            ("/bonus/", "Mijozlar", ["SEVIMLI BONUS", "Mijozlarga SMS"], ["SEVIMLI BONUS"]),
            ("/bonus/cheklar/", "Mijozlar", None, ["SEVIMLI BONUS"]),
            ("/sms/", "Mijozlar", None, ["Mijozlarga SMS"]),
            ("/kpi/", "Nazorat", [], []),
            ("/kpi/minuslar/", "Nazorat", [], []),
            ("/kassalar/", "Sozlash", ["Kassalar", "To'lov turlari", "Boshqaruvchilar", "Versiyalar"], ["Kassalar"]),
            ("/tolov-turlari/", "Sozlash", None, ["To'lov turlari"]),
            ("/boshqaruvchilar/", "Sozlash", None, ["Boshqaruvchilar"]),
            ("/versiyalar/", "Sozlash", None, ["Versiyalar"]),
        ]
        for url, bolim, sub, sub_on in kutilgan:
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 200)
                names, on, subs, subs_on = _nav(r.content.decode())
                self.assertEqual(names, ["Bosh sahifa", "Savdo", "Tovarlar", "Mijozlar", "Nazorat", "Sozlash"])
                self.assertEqual(on, [bolim])
                if sub is not None:
                    self.assertEqual([s.replace("&#x27;", "'") for s in subs], sub)
                self.assertEqual([s.replace("&#x27;", "'") for s in subs_on], sub_on)

    def test_hamma_havola_ochiladi(self):
        for url in _links(self.client.get("/kassalar/").content.decode()) + ["/tovarlar/", "/bonus/"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class BoshqaruvchiTest(TestCase):
    def setUp(self):
        u = User.objects.create_user("market", password="x")
        PanelManager.objects.create(user=u, warehouse_ms_id=uuid.uuid4(), warehouse_name="Sevimli Shahar")
        self.client.force_login(u)

    def test_faqat_oz_bolimlari(self):
        html = self.client.get("/").content.decode()
        names, on, subs, _ = _nav(html)
        self.assertEqual(names, ["Bosh sahifa", "Savdo", "Tovarlar"])
        self.assertEqual(on, ["Bosh sahifa"])
        self.assertIn("Sevimli Shahar", html)                   # market nomi logotip ostida
        for yopiq in ("/kassalar/", "/bonus/", "/sms/", "/kpi/", "/narxlar/", "/tovarlar/kochirish/"):
            self.assertNotIn(f'href="{yopiq}"', re.search(r"<header.*?</header>", html, flags=re.S).group(0))

    def test_menyudagi_har_bir_havola_ochiq(self):
        ochilgan = set()
        for start in ("/", "/smenalar/", "/tovarlar/"):
            for url in _links(self.client.get(start).content.decode()):
                if url == "/chiqish/" or url in ochilgan:
                    continue
                ochilgan.add(url)
                with self.subTest(url=url):
                    self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(ochilgan, {"/", "/smenalar/", "/cheklar/", "/tovarlar/", "/tovarlar/tugayotgan/", "/tovarlar/tushgan/"})

    def test_yopiq_sahifa_403_va_menyu_bilan(self):
        r = self.client.get("/kassalar/")
        self.assertEqual(r.status_code, 403)
        names, on, _, _ = _nav(r.content.decode())
        self.assertEqual(names, ["Bosh sahifa", "Savdo", "Tovarlar"])
        self.assertEqual(on, [])


class TovarovedTest(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("tov", password="x")
        self.tov = Tovaroved.objects.create(name="Sardor", user=self.u)
        self.boshqa = Tovaroved.objects.create(name="Jasur")
        self.client.force_login(self.u)

    def test_ikki_band(self):
        for url, tanlangan in (("/kpi/", ["Reyting"]), ("/kpi/men/", ["Mening ishim"]),
                               (f"/kpi/xodim/{self.tov.pk}/", ["Mening ishim"]),
                               (f"/kpi/xodim/{self.boshqa.pk}/", []), ("/kpi/minuslar/", ["Reyting"])):
            with self.subTest(url=url):
                r = self.client.get(url, follow=True)
                self.assertEqual(r.status_code, 200)
                names, on, subs, _ = _nav(r.content.decode())
                self.assertEqual(names, ["Reyting", "Mening ishim"])
                self.assertEqual(on, tanlangan)
                self.assertEqual(subs, [])

    def test_havolalar_ochiq(self):
        for url in _links(self.client.get("/kpi/").content.decode()):
            if url != "/chiqish/":
                with self.subTest(url=url):
                    self.assertEqual(self.client.get(url, follow=True).status_code, 200)
