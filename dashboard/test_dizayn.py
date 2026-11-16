"""Design system tekshiruvlari (docs/DESIGN_SYSTEM.md).

* matn va belgilar kontrasti ikkala rejimda WCAG AA dan past emas;
* sahifalar faqat base.html da aniqlangan tokenlarni ishlatadi (aniqlanmagan token —
  jimgina «rangsiz» qolgan joy: kassa sozlamasida qora fonda oq maydonlar shundan edi);
* shablonlarda ko'p qatorli `{# … #}` yo'q (Django uni matn qilib chiqaradi);
* bosh sahifa yorliqlari va karkas o'z joyida."""

from __future__ import annotations

import re
from pathlib import Path

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from dashboard.templatetags.aloqa_tags import summary

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
