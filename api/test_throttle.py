"""I17: kirish urinishlarini cheklash — bazada, hamma jarayon uchun umumiy.

Qoplanadi: kassa ulanishi, kassir kirishi, panel; login yozilishi
variantlari (katta-kichik harf, bo'shliq, to'liq kenglik); begona IP'dan
hujum do'kondagi xodimni yopmasligi; umumiy chegara; muddat o'tib o'zi
ochilishi; qo'lda ochish (buyruq, admin); X-Forwarded-For soxtaligi;
bir nechta alohida jarayon bitta hisobni ko'rishi.
"""
import os
import subprocess
import sys
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock, skipUnless

from django.conf import settings as dj_settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from api import throttle
from api.models import LoginThrottle
from api.tests import ApiTestCase
from sales.models import Cashier, Register

SHOP_IP = "203.0.113.10"      # do'kon NAT manzili (sinov manzili)
ATTACKER_IPS = [f"198.51.100.{i}" for i in range(1, 8)]


def later(minutes):
    return mock.patch("api.throttle.timezone.now",
                      return_value=timezone.now() + timedelta(minutes=minutes))


class KassaLoginThrottleTest(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.cashier = Cashier.objects.create(login="kassir1", name="Kassir")
        self.cashier.set_password("togri-parol")
        self.cashier.save()

    def _login(self, login, password, ip=SHOP_IP):
        return self.post("/api/v1/login", {"login": login, "password": password},
                         HTTP_X_REAL_IP=ip)

    def test_kop_xatodan_keyin_login_yopiladi_togri_parol_ham(self):
        for _ in range(throttle.PAIR_LIMIT):
            self.assertEqual(self._login("kassir1", "xato").status_code, 401)
        self.assertEqual(self._login("kassir1", "togri-parol").status_code, 429)

    def test_boshqa_login_bloklanmaydi(self):
        """Bir do'kon — bitta IP: bir kassirning xatosi boshqasini to'xtatmaydi."""
        for _ in range(throttle.PAIR_LIMIT):
            self._login("kassir1", "xato")
        other = Cashier.objects.create(login="kassir2", name="Ikkinchi")
        other.set_password("p2")
        other.save()
        self.assertEqual(self._login("kassir2", "p2").status_code, 200)

    def test_muvaffaqiyat_hisoblagichni_tozalaydi(self):
        for _ in range(throttle.PAIR_LIMIT - 1):
            self._login("kassir1", "xato")
        self.assertEqual(self._login("kassir1", "togri-parol").status_code, 200)
        for _ in range(throttle.PAIR_LIMIT - 1):
            self._login("kassir1", "xato")
        self.assertEqual(self._login("kassir1", "togri-parol").status_code, 200)

    def test_yozilish_variantlari_bitta_hisob(self):
        variants = ["kassir1", "KASSIR1", " Kassir1 ", "ｋａｓｓｉｒ１", "KaSsIr1"]
        for i in range(throttle.PAIR_LIMIT):
            self._login(variants[i % len(variants)], "xato")
        self.assertEqual(self._login("kassir1", "togri-parol").status_code, 429)

    def test_muddat_otgach_ozi_ochiladi(self):
        for _ in range(throttle.PAIR_LIMIT):
            self._login("kassir1", "xato")
        with later(14):
            self.assertEqual(self._login("kassir1", "togri-parol").status_code, 429)
        with later(16):
            self.assertEqual(self._login("kassir1", "togri-parol").status_code, 200)


class ConnectThrottleTest(TestCase):
    def setUp(self):
        self.reg = Register.objects.create(code="k9", name="Kassa 9", login="kassa9")
        self.reg.set_password("kassa-parol")
        self.reg.save()

    def _connect(self, pwd, login="kassa9", ip=SHOP_IP, **extra):
        return self.client.post("/api/v1/connect", {"login": login, "password": pwd},
                                content_type="application/json", HTTP_X_REAL_IP=ip, **extra)

    def test_connect_cheklanadi(self):
        for _ in range(throttle.PAIR_LIMIT):
            self.assertEqual(self._connect("xato").status_code, 401)
        self.assertEqual(self._connect("kassa-parol").status_code, 429)

    def test_begona_ip_hujumi_dokondagi_kassani_yopmaydi(self):
        for _ in range(throttle.PAIR_LIMIT + 5):
            self._connect("xato", ip=ATTACKER_IPS[0])
        self.assertEqual(self._connect("xato", ip=ATTACKER_IPS[0]).status_code, 429)
        self.assertEqual(self._connect("kassa-parol", ip=SHOP_IP).status_code, 200)

    def test_kop_ipdan_hujum_umumiy_chegaraga_uriladi(self):
        """Botnet: har IP'dan 10 tadan — jami GLOBAL_LIMIT'dan keyin hammaga yopiq.
        Bu lockout-DoS narxi: 15 daqiqa. Ulanib bo'lgan kassalar ishlayveradi."""
        n = 0
        for ip in ATTACKER_IPS:
            for _ in range(throttle.PAIR_LIMIT):
                if n < throttle.GLOBAL_LIMIT:
                    self._connect("xato", ip=ip)
                    n += 1
        self.assertEqual(self._connect("kassa-parol", ip=SHOP_IP).status_code, 429)
        with later(16):
            self.assertEqual(self._connect("kassa-parol", ip=SHOP_IP).status_code, 200)

    def test_x_forwarded_for_bilan_ip_almashtirib_bolmaydi(self):
        for i in range(throttle.PAIR_LIMIT):
            self._connect("xato", ip=ATTACKER_IPS[0], HTTP_X_FORWARDED_FOR=f"10.9.9.{i}")
        r = self._connect("xato", ip=ATTACKER_IPS[0], HTTP_X_FORWARDED_FOR="10.9.9.200")
        self.assertEqual(r.status_code, 429)

    def test_boshqa_kassa_loginiga_tegmaydi(self):
        other = Register.objects.create(code="k8", name="Kassa 8", login="kassa8")
        other.set_password("p8")
        other.save()
        for _ in range(throttle.PAIR_LIMIT):
            self._connect("xato")
        self.assertEqual(self._connect("p8", login="kassa8").status_code, 200)


class RecoveryTest(TestCase):
    def setUp(self):
        for _ in range(throttle.PAIR_LIMIT):
            throttle.failed("connect", "kassa9", SHOP_IP)
            throttle.failed("panel", "egasi", SHOP_IP)
            throttle.failed("login", "5:kassir1", SHOP_IP)

    def test_buyruq_bitta_loginni_ochadi(self):
        out = StringIO()
        call_command("kirish_ochish", "KASSA9", stdout=out)
        self.assertIn("Ochildi", out.getvalue())
        self.assertFalse(throttle.blocked("connect", "kassa9", SHOP_IP))
        self.assertTrue(throttle.blocked("panel", "egasi", SHOP_IP))

    def test_buyruq_kassir_loginini_ochadi(self):
        call_command("kirish_ochish", "kassir1", stdout=StringIO())
        self.assertFalse(throttle.blocked("login", "5:kassir1", SHOP_IP))

    def test_buyruq_hammasini_ochadi(self):
        call_command("kirish_ochish", "--hammasi", stdout=StringIO())
        self.assertFalse(LoginThrottle.objects.exists())

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_admin_sahifasida_korinadi_va_ochiladi(self):
        User.objects.create_superuser("bosh", password="bosh-parol")
        c = Client()
        c.force_login(User.objects.get(username="bosh"))
        r = c.get("/admin/api/loginthrottle/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "connect:kassa9")
        row = LoginThrottle.objects.get(key="connect:kassa9")
        r = c.post(f"/admin/api/loginthrottle/{row.pk}/delete/", {"post": "yes"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(LoginThrottle.objects.filter(key="connect:kassa9").exists())

    def test_log_parolni_yozmaydi(self):
        with self.assertLogs("api", level="WARNING") as logs:
            for _ in range(throttle.PAIR_LIMIT):
                throttle.failed("connect", "yangi", SHOP_IP)
        self.assertIn("vaqtincha yopildi", logs.output[0])
        self.assertNotIn("parol", logs.output[0].lower())


class PanelLoginThrottleTest(TestCase):
    def setUp(self):
        User.objects.create_user("egasi", password="panel-parol")

    def _post(self, c, user, pwd, ip=SHOP_IP):
        return c.post("/kirish/", {"username": user, "password": pwd}, HTTP_X_REAL_IP=ip)

    def test_panel_login_cheklanadi(self):
        c = Client()
        for _ in range(throttle.PAIR_LIMIT):
            self.assertEqual(self._post(c, "egasi", "xato").status_code, 200)
        r = self._post(c, "egasi", "panel-parol")
        self.assertEqual(r.status_code, 429)
        self.assertNotIn("_auth_user_id", c.session)

    def test_nfkc_variantlari_aylanib_otmaydi(self):
        # Django panel formasi loginni NFKC qiladi: «ｅｇａｓｉ» == «egasi».
        c = Client()
        for i, user in enumerate(["ｅｇａｓｉ", "Egasi", "EGASI", " egasi"] * 3):
            if i < throttle.PAIR_LIMIT:
                self._post(c, user, "xato")
        self.assertEqual(self._post(c, "egasi", "panel-parol").status_code, 429)

    def test_togri_login_ishlaydi(self):
        r = self._post(Client(), "egasi", "panel-parol")
        self.assertEqual(r.status_code, 302)


@skipUnless(connection.vendor == "postgresql", "alohida jarayonlar umumiy baza talab qiladi")
class CrossProcessTest(TransactionTestCase):
    """Gunicorn'dagidek: 4 ta ALOHIDA Python jarayoni bir vaqtda xato uradi."""

    SCRIPT = (
        "import django, sys\n"
        "django.setup()\n"
        "from api import throttle\n"
        "for _ in range(int(sys.argv[1])):\n"
        "    throttle.failed('connect', 'kassa9', '203.0.113.10')\n"
    )

    def _env(self):
        s = connection.settings_dict
        url = (f"postgres://{s['USER']}:{s['PASSWORD']}@{s['HOST'] or '127.0.0.1'}:"
               f"{s['PORT'] or 5432}/{s['NAME']}")
        env = dict(os.environ, DATABASE_URL=url,
                   DJANGO_SETTINGS_MODULE=os.environ.get("DJANGO_SETTINGS_MODULE", "config.settings"))
        env.pop("MOYSKLAD_TOKEN", None)
        return env

    def test_jarayonlar_bitta_hisobni_koradi(self):
        root = Path(dj_settings.BASE_DIR)
        procs = [subprocess.Popen([sys.executable, "-c", self.SCRIPT, "3"], cwd=root,
                                  env=self._env()) for _ in range(4)]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        row = LoginThrottle.objects.get(key="connect:kassa9|ip:203.0.113.10")
        self.assertEqual(row.failures, 12)          # birorta ham yo'qolmadi
        self.assertTrue(throttle.blocked("connect", "kassa9", SHOP_IP))
        self.assertEqual(LoginThrottle.objects.get(key="connect:kassa9").failures, 12)
