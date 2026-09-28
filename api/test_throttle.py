"""I17: kirish urinishlarini cheklash — login bo'yicha, IP bo'yicha emas."""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase

from api import throttle
from api.tests import ApiTestCase
from sales.models import Cashier, Register


class KassaLoginThrottleTest(ApiTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.cashier = Cashier.objects.create(login="kassir1", name="Kassir")
        self.cashier.set_password("togri-parol")
        self.cashier.save()

    def _login(self, login, password):
        return self.post("/api/v1/login", {"login": login, "password": password})

    def test_kop_xatodan_keyin_login_yopiladi_togri_parol_ham(self):
        for _ in range(throttle.LIMIT):
            self.assertEqual(self._login("kassir1", "xato").status_code, 401)
        self.assertEqual(self._login("kassir1", "togri-parol").status_code, 429)

    def test_boshqa_login_bloklanmaydi(self):
        """Bir do'kon — bitta IP: bir kassirning xatosi boshqasini to'xtatmaydi."""
        for _ in range(throttle.LIMIT):
            self._login("kassir1", "xato")
        other = Cashier.objects.create(login="kassir2", name="Ikkinchi")
        other.set_password("p2"); other.save()
        self.assertEqual(self._login("kassir2", "p2").status_code, 200)

    def test_muvaffaqiyat_hisoblagichni_tozalaydi(self):
        for _ in range(throttle.LIMIT - 1):
            self._login("kassir1", "xato")
        self.assertEqual(self._login("kassir1", "togri-parol").status_code, 200)
        for _ in range(throttle.LIMIT - 1):
            self._login("kassir1", "xato")
        self.assertEqual(self._login("kassir1", "togri-parol").status_code, 200)


class ConnectThrottleTest(TestCase):
    def setUp(self):
        cache.clear()
        self.reg = Register.objects.create(code="k9", name="Kassa 9", login="kassa9")
        self.reg.set_password("kassa-parol")
        self.reg.save()

    def _connect(self, pwd):
        return self.client.post("/api/v1/connect", {"login": "kassa9", "password": pwd},
                                content_type="application/json")

    def test_connect_cheklanadi(self):
        for _ in range(throttle.LIMIT):
            self.assertEqual(self._connect("xato").status_code, 401)
        self.assertEqual(self._connect("kassa-parol").status_code, 429)


class PanelLoginThrottleTest(TestCase):
    def setUp(self):
        cache.clear()
        User.objects.create_user("egasi", password="panel-parol")

    def test_panel_login_cheklanadi(self):
        c = Client()
        for _ in range(throttle.LIMIT):
            r = c.post("/kirish/", {"username": "egasi", "password": "xato"})
            self.assertEqual(r.status_code, 200)
        r = c.post("/kirish/", {"username": "egasi", "password": "panel-parol"})
        self.assertEqual(r.status_code, 429)
        self.assertNotIn("_auth_user_id", c.session)

    def test_togri_login_ishlaydi(self):
        r = Client().post("/kirish/", {"username": "egasi", "password": "panel-parol"})
        self.assertEqual(r.status_code, 302)
