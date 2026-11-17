"""Design system tekshiruvlari (docs/DESIGN_SYSTEM.md).

* matn va belgilar kontrasti ikkala rejimda WCAG AA dan past emas;
* sahifalar faqat base.html da aniqlangan tokenlarni ishlatadi (aniqlanmagan token —
  jimgina «rangsiz» qolgan joy: kassa sozlamasida qora fonda oq maydonlar shundan edi);
* shablonlarda ko'p qatorli `{# … #}` yo'q (Django uni matn qilib chiqaradi);
* bosh sahifa yorliqlari va karkas o'z joyida."""

from __future__ import annotations

import re
from pathlib import Path

from unittest import mock

from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.template import Context, Template
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from catalog.models import RetailStore
from dashboard.templatetags.aloqa_tags import summary
from sales.models import KassaSession, Register

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "dashboard/templates/dashboard/base.html"
#: base.html ga tayanmaydigan mustaqil sahifalar (o'z uslubi bor)
MUSTAQIL = {"login.html", "installer.html"}


def _tokens() -> tuple[dict, dict]:
    css = BASE.read_text(encoding="utf-8")
    light = re.search(r":root \{(.*?)\n    \}", css, flags=re.S).group(1)
    dark = re.search(r'html\[data-theme="dark"\] \{(.*?)\n    \}', css, flags=re.S).group(1)
    pick = lambda block: dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block))
    return pick(light), pick(dark)


def _rgb(value: str, under=(255, 255, 255)):
    value = value.strip()
    if value.startswith("#"):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
    m = re.match(r"rgba\((\d+),(\d+),(\d+),([\d.]+)\)", value.replace(" ", ""))
    r, g, b, a = int(m[1]), int(m[2]), int(m[3]), float(m[4])
    return tuple(round(c * a + u * (1 - a)) for c, u in zip((r, g, b), under))


def _lum(c):
    f = lambda x: x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    r, g, b = (f(x / 255) for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _kontrast(a, b) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


#: (ustidagi, ostidagi, eng kam nisbat) — matn 4.5, grafik belgi va chegara 3.0
MATN = [("--ink", "--surface"), ("--ink", "--bg"), ("--ink-soft", "--surface"), ("--ink-soft", "--surface-2"),
        ("--ink-soft", "--surface-3"), ("--muted", "--surface"), ("--muted", "--surface-2"), ("--muted", "--bg"),
        ("--faint", "--surface"), ("--faint", "--surface-2"), ("--accent-text", "--surface"),
        ("--accent-text", "--surface-2"), ("--accent-text", "--accent-soft"), ("--accent-ink", "--accent"),
        ("--accent-ink", "--accent-deep"), ("--ok", "--surface"), ("--ok", "--ok-soft"),
        ("--ok-solid-ink", "--ok-solid"), ("--warn", "--surface"), ("--warn", "--warn-soft"),
        ("--gold-ink", "--gold-bg"), ("--danger", "--surface"), ("--danger", "--danger-soft"),
        ("--info", "--surface"), ("--info", "--info-soft"), ("--ink", "--warn-soft"), ("--ink", "--ok-soft"),
        ("--ink", "--danger-soft"), ("--nav-ink", "--nav-bg")]
BELGI = [("--series", "--surface"), ("--series-2", "--surface"), ("--series-ctx", "--surface"),
         ("--ok-dot", "--surface"), ("--warn-dot", "--surface"), ("--danger-dot", "--surface"),
         ("--ctl-line", "--surface"), ("--ctl-line", "--bg"), ("--nav-line", "--nav-bg")]


class KontrastTest(SimpleTestCase):
    def test_ikkala_rejimda_AA(self):
        light, dark = _tokens()
        for nom, tok in (("kunduzgi", light), ("tungi", {**light, **dark})):
            surf = _rgb(tok["--surface"])
            for pairs, kam in ((MATN, 4.5), (BELGI, 3.0)):
                for ust, ost in pairs:
                    with self.subTest(rejim=nom, juft=f"{ust} / {ost}"):
                        k = _kontrast(_rgb(tok[ust], surf), _rgb(tok[ost], surf))
                        self.assertGreaterEqual(round(k, 2), kam, f"{nom}: {ust} / {ost} = {k:.2f}")
            # xato belgisi (oq «✕») va tepa menyudagi oq yozuv
            self.assertGreaterEqual(_kontrast((255, 255, 255), _rgb(tok["--danger-solid"])), 4.5)
            self.assertGreaterEqual(_kontrast((255, 255, 255), _rgb(tok["--nav-bg"])), 4.5)

    def test_tungi_rejim_hamma_rangni_almashtiradi(self):
        light, dark = _tokens()
        rang = {k for k, v in light.items() if v.strip().startswith(("#", "rgba")) and k != "--brand-red"
                and k not in ("--shadow", "--gold-bg-hover")}
        # tungi rejimda qiymati ataylab bir xil qoladiganlar
        bir_xil = {"--accent-ink", "--danger-solid", "--nav-line"}
        self.assertEqual(rang - set(dark) - bir_xil, set())

    def test_shrift_pogonalari(self):
        light, _ = _tokens()
        self.assertEqual([light[f"--fs-{i}"] for i in range(1, 8)],
                         ["11px", "12px", "13px", "14px", "16px", "20px", "24px"])


class ShablonTest(SimpleTestCase):
    def _shablonlar(self):
        for app in ("dashboard", "kpi", "sms"):
            for p in sorted((ROOT / app / "templates" / app).glob("*.html")):
                if p.name not in MUSTAQIL:
                    yield p

    def test_faqat_aniqlangan_tokenlar(self):
        light, _ = _tokens()
        for p in self._shablonlar():
            ishlatilgan = set(re.findall(r"var\((--[\w-]+)", p.read_text(encoding="utf-8")))
            # SVG ichida skript qo'yadigan o'zgaruvchilar
            ishlatilgan -= {"--k", "--cols"}
            with self.subTest(shablon=p.name):
                self.assertEqual(ishlatilgan - set(light), set())

    def test_kop_qatorli_izoh_yoq(self):
        for p in self._shablonlar():
            with self.subTest(shablon=p.name):
                self.assertIsNone(re.search(r"\{#[^#\n]*\n", p.read_text(encoding="utf-8")),
                                  "{# … #} bir qatorda bo'lishi shart — ko'p qatorlisi sahifada matn bo'lib chiqadi")


class AloqaYigindisiTest(SimpleTestCase):
    def test_qoida(self):
        r = lambda *states: [{"state": s} for s in states]
        self.assertEqual(summary(r("ok", "ok")), {"ok": 2, "total": 2, "state": "ok"})
        self.assertEqual(summary(r("ok", "warn")), {"ok": 1, "total": 2, "state": "warn"})
        self.assertEqual(summary(r("ok", "warn", "bad")), {"ok": 1, "total": 3, "state": "bad"})
        self.assertEqual(summary(r("unknown")), {"ok": 0, "total": 1, "state": "warn"})
        self.assertEqual(summary([]), {"ok": 0, "total": 0, "state": "ok"})


class KarkasTest(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_superuser("egasi", password="x"))

    def test_bosh_sahifa_yorliqlari(self):
        html = self.client.get("/").content.decode()
        self.assertEqual(re.findall(r'data-tab="(\w+)"', html), ["umumiy", "kunlar", "tizim"])
        self.assertEqual(re.findall(r'class="tabpanel" data-panel="(\w+)"', html), ["umumiy", "kunlar", "tizim"])
        # bloklar o'z yorlig'ida
        umumiy, kunlar, tizim = (html.index(f'data-panel="{n}"') for n in ("umumiy", "kunlar", "tizim"))
        joy = lambda matn: html.index(matn)
        self.assertTrue(umumiy < joy('class="tiles strip"') < joy("Nuqtalar bo'yicha savdo") < kunlar)
        self.assertTrue(kunlar < joy("Kunlik ko'rsatkichlar") < tizim)
        self.assertTrue(tizim < joy("Kassalardagi yuborish holati") < joy("MoySklad tekshiruvi"))
        # davr shakli va ogohlantirishlar yorliqlardan tashqarida — har doim ko'rinadi
        self.assertLess(joy('id="davr"'), joy('class="tabs hometabs"'))

    def test_karkas(self):
        html = self.client.get("/smenalar/").content.decode()
        self.assertIn('<header class="topnav">', html)
        self.assertIn('data-theme="light"', html)
        self.assertIn('id="aloqa-sum"', html)
        self.assertIn('href="/chiqish/"', html)
        self.assertNotIn("aside", re.sub(r"<style>.*?</style>", "", html, flags=re.S).split("<main")[0])


def _media(css: str, selector: str) -> list[tuple[int | None, int | None]]:
    """`selector` qaysi @media bloklari ichida turgani: [(min-width, max-width), …]."""
    out = []
    for m in re.finditer(r"@media ([^{]+)\{", css):
        depth, i = 1, m.end()
        while depth and i < len(css):
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        if selector in css[m.end():i]:
            lo = re.search(r"min-width: (\d+)px", m.group(1))
            hi = re.search(r"max-width: (\d+)px", m.group(1))
            out.append((int(lo[1]) if lo else None, int(hi[1]) if hi else None))
    return out


class TorEkranTest(TestCase):
    """16.11.2026 — chiqarilgandan keyin haqiqiy panelda topilgan nuqsonlar qaytmasin:
    1280 px va planshetda tepa menyuning oxirgi bo'limlari («Nazorat», «Sozlash») kesilib
    qolgan, Kassalar jadvalida «O'chirish» tugmasi 1500 px dan tor ekranda jadval chetida
    yashirinib turgan edi.

    Kenglikning o'zi brauzerda o'lchanadi; bu yerda — qoidalar joyida ekani. Chegaralar eng
    keng holatning (egasi, 6 bo'lim, «10/11») o'lchangan ehtiyojidan kelib chiqqan: to'liq
    ko'rinishga 1356 px, sana/soatsiz 1195, kassa yozuvisiz 1005, belgisiz 850 px kerak
    (Inter shrifti; brauzerning aylantirish chizig'i yana ~17 px oladi). Chegarani pasaytirsangiz
    — avval qayta o'lchang (docs/DESIGN_SYSTEM.md, 4-bo'lim)."""

    def setUp(self):
        self.client.force_login(User.objects.create_superuser("egasi", password="x"))
        self.css = BASE.read_text(encoding="utf-8")

    def test_tepa_qator_bosqichma_bosqich_siqiladi(self):
        (_, sana), = _media(self.css, ".topnav .hide-md { display: none; }")
        (_, kassa), = _media(self.css, ".topnav .aloqa .hide-sm { display: none; }")
        (belgi_min, belgi), = _media(self.css, "nav.mainnav a.item .icon { display: none; }")
        ikki_qator = max(hi for _, hi in _media(self.css, "order: 3;"))
        self.assertGreaterEqual(sana, 1356 + 17)
        self.assertGreaterEqual(kassa, 1195 + 17)
        self.assertGreaterEqual(belgi, 1005 + 17)
        self.assertGreaterEqual(ikki_qator, 850 + 17)
        self.assertEqual(belgi_min, ikki_qator + 1, "ikki qatorli ko'rinishda belgilar qaytadi — joy bor")
        self.assertTrue(sana > kassa > belgi > ikki_qator > 760)
        # ikki qatorli ko'rinishda menyu yopishmaydi va yopishqoq qismlar 0 dan boshlanadi
        blok = self.css[self.css.index(f"@media (max-width: {ikki_qator}px)"):]
        blok = blok[:blok.index("@media", 10)]
        self.assertIn("--head-h: 0px", blok)
        self.assertIn("position: static", blok)

    def test_sigmasa_skript_keyingi_bosqichni_yoqadi(self):
        """Shrift kengroq chiqsa yoki bo'lim qo'shilsa ham menyu kesilmaydi."""
        self.assertIn(".topnav.fit1 .hide-md, .topnav.fit2 .aloqa .hide-sm, "
                      ".topnav.fit3 nav.mainnav a.item .icon { display: none; }", self.css)
        html = self.client.get("/smenalar/").content.decode()
        self.assertIn('hdr.classList.add("fit" + i)', html)
        self.assertIn("nav.scrollWidth > nav.clientWidth + 1", html)
        self.assertIn("document.fonts.ready.then(fit)", html)
        # menyu baribir sig'masa ham sahifani buzmaydi — o'z ichida suriladi
        nav = re.search(r"nav\.mainnav \{(.*?)\}", self.css, flags=re.S).group(1)
        self.assertIn("min-width: 0", nav)
        self.assertIn("overflow-x: auto", nav)

    def test_kassalar_amallari_ikki_qatorda(self):
        store = RetailStore.objects.create(ms_id="00000000-0000-0000-0000-0000000000f7", name="Nuqta", active=True)
        reg = Register.objects.create(code="k1", name="shaxar parfyumerya", store=store, login="parfyumerya",
                                      device="dev-1", device_name="WIN-52C6KCQ7Q2K")
        now = timezone.now()
        KassaSession.objects.create(register=reg, login="parfyumerya", device="dev-1", device_name="POS 1",
                                    cashier_name="shaxar parfyumerya", started_at=now, seen_at=now)
        html = self.client.get("/kassalar/").content.decode()
        # tugmalar ikki ustunli to'rda — ustun tor, hech biri jadval chetida qolmaydi
        self.assertEqual(html.count('<td class="actions"><div class="acts">'), 1)
        self.assertIn("grid-template-columns: auto auto", html)
        self.assertNotIn("table.kassalar td.actions { white-space: nowrap", html)
        amallar = html[html.index('<div class="acts">'):]
        amallar = amallar[:amallar.index("</div></td>")]
        for matn in ("Sozlash", 'placeholder="yangi parol"', "Almashtirish", "Aloqani uzish", "O'chirish"):
            self.assertIn(matn, amallar.replace("&#x27;", "'"))
        # «Kim kirgan»: joy yetmasa butun bo'lak bo'lib pastga tushadi (so'z o'rtasidan bo'linmaydi)
        self.assertIn('<span class="sec nb">· POS 1 ·', html)
        self.assertIn('<span class="nb">Kompyuter: WIN-52C6KCQ7Q2K</span>', html)
        self.assertIn("table.kassalar td.who { white-space: normal;", html)

    def test_juda_tor_telefonda_ikki_ustunli_bloklar_sigadi(self):
        """320 px telefonda `minmax(320px, 1fr)` sahifani 12 px ga toshirardi."""
        for p in (BASE, ROOT / "dashboard/templates/dashboard/bonus.html"):
            with self.subTest(shablon=p.name):
                self.assertIsNone(re.search(r"minmax\(320px", p.read_text(encoding="utf-8")))


class ShriftTest(TestCase):
    """Panel shrifti — Inter (egasi tanlagan, 16.11.2026): saytning o'zidan beriladi, hamma
    kompyuter va telefonda bir xil. Shrift yuklanmasa ham sahifa ochilishi shart."""

    BOLAKLAR = ("latin", "latin-ext", "cyrillic", "cyrillic-ext")
    PAPKA = ROOT / "dashboard/static/dashboard/fonts"

    def test_fayllar_joyida(self):
        for b in self.BOLAKLAR:
            nom = f"dashboard/fonts/inter-{b}-wght-normal.woff2"
            with self.subTest(fayl=nom):
                yol = finders.find(nom)          # collectstatic shu yerdan oladi
                self.assertIsNotNone(yol, "shrift fayli statik fayllar orasida topilmadi")
                self.assertEqual(Path(yol).read_bytes()[:4], b"wOF2")
        # litsenziya (OFL-1.1) shrift bilan birga tarqatilishi shart
        self.assertIn("SIL OPEN FONT LICENSE Version 1.1", (self.PAPKA / "OFL.txt").read_text(encoding="utf-8"))
        # ortiqcha og'irlik yo'q: to'rttala fayl birga 200 KB dan kichik
        self.assertLess(sum(f.stat().st_size for f in self.PAPKA.glob("*.woff2")), 200_000)

    def _tekshir(self, html):
        elonlar = re.findall(r"@font-face \{(.*?)\}", html, flags=re.S)
        self.assertEqual(len(elonlar), 4)
        for e in elonlar:
            self.assertIn('font-family: "Inter"', e)
            self.assertIn("font-weight: 100 900", e)      # bitta fayl — hamma qalinlik
            self.assertIn("font-display: swap", e)        # yuklanguncha yozuv ko'rinib turadi
            self.assertIn("unicode-range:", e)            # kerakli bo'lakgina yuklanadi
        manzillar = re.findall(r'url\("([^"]+)"\)', html)
        self.assertEqual(sorted(m.rsplit("/", 1)[1] for m in manzillar),
                         sorted(f"inter-{b}-wght-normal.woff2" for b in self.BOLAKLAR))
        self.assertTrue(all(m.startswith("/static/dashboard/fonts/") for m in manzillar), manzillar)
        # lotin bo'lagi oldindan yuklanadi (crossorigin — shrift uchun shart)
        self.assertRegex(html, r'<link rel="preload" href="/static/dashboard/fonts/inter-latin-wght-normal\.woff2" '
                               r'as="font" type="font/woff2" crossorigin>')
        self.assertIn('font-family: "Inter", "Segoe UI", system-ui, sans-serif', html)

    def test_panel_va_kirish_sahifasida(self):
        self._tekshir(self.client.get("/kirish/").content.decode())
        self.client.force_login(User.objects.create_superuser("egasi", password="x"))
        html = self.client.get("/smenalar/").content.decode()
        self._tekshir(html)
        # e'lon uslublardan oldin turadi va tashqi saytga murojaat yo'q
        self.assertLess(html.index("@font-face"), html.index("--bg:"))
        self.assertNotIn("fonts.googleapis.com", html)
        self.assertNotIn("fonts.gstatic.com", html)

    def test_manifestda_yoq_fayl_sahifani_yiqitmaydi(self):
        chiz = lambda: Template('{% load panel_tags %}{% statik "dashboard/fonts/x.woff2" %}').render(Context())
        with mock.patch("dashboard.templatetags.panel_tags.staticfiles_storage.url",
                        side_effect=ValueError("Missing staticfiles manifest entry")):
            self.assertEqual(chiz(), "/static/dashboard/fonts/x.woff2")
        # serverda (collectstatic dan keyin) — xeshli manzil: brauzer abadiy keshlaydi
        with mock.patch("dashboard.templatetags.panel_tags.staticfiles_storage.url",
                        return_value="/static/dashboard/fonts/x.0a1b2c3d4e5f.woff2"):
            self.assertEqual(chiz(), "/static/dashboard/fonts/x.0a1b2c3d4e5f.woff2")

