"""Egaga ogohlantirish (sales/ogoh.py) testlari.

Asosiy talablar: sozlanmagan bo'lsa jim turadi; hech qachon xato
ko'tarmaydi; bir mavzu bo'yicha qayta-qayta yozmaydi; token jurnalga
tushmaydi.
"""

import json
import urllib.error
from unittest import mock

from django.core.cache import cache, caches
from django.test import TestCase, override_settings

from api.tests import ApiTestCase
from sales import ogoh

TOKEN = "123456:SINOV-maxfiy-token"
YOQILGAN = override_settings(ALERT_TELEGRAM_TOKEN=TOKEN, ALERT_TELEGRAM_CHAT="-100777")


class _SyncThread:
    """Fon oqimi o'rniga — shu zahoti bajaradi (testda kutmaslik uchun)."""

    def __init__(self, target=None, args=(), **kw):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def _ok_response():
    response = mock.MagicMock()
    response.__enter__.return_value.status = 200
    return response


class OgohTest(TestCase):
    def setUp(self):
        cache.clear()
        caches["shared"].clear()

    def test_sozlanmagan_bolsa_hech_narsa_qilmaydi(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("so'rov ketdi")):
            self.assertFalse(ogoh.enabled())
            self.assertFalse(ogoh.send("salom"))
            self.assertFalse(ogoh.once("mavzu", "salom"))

    @override_settings(ALERT_TELEGRAM_TOKEN=TOKEN, ALERT_TELEGRAM_CHAT="")
    def test_faqat_token_yetarli_emas(self):
        self.assertFalse(ogoh.enabled())

    @YOQILGAN
    def test_xabar_telegramga_ketadi(self):
        with mock.patch("urllib.request.urlopen", return_value=_ok_response()) as urlopen:
            self.assertTrue(ogoh.send("Kassa-3: 119 ta chek o'tmayapti"))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        body = json.loads(request.data)
        self.assertEqual(body["chat_id"], "-100777")
        self.assertEqual(body["text"], "Kassa-3: 119 ta chek o'tmayapti")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], ogoh.TIMEOUT)

    @YOQILGAN
    def test_uzun_matn_qisqartiriladi(self):
        with mock.patch("urllib.request.urlopen", return_value=_ok_response()) as urlopen:
            ogoh.send("x" * 10_000)
        self.assertEqual(len(json.loads(urlopen.call_args.args[0].data)["text"]), 3500)

    @YOQILGAN
    def test_bir_mavzu_boyicha_bir_marta(self):
        with mock.patch("sales.ogoh.threading.Thread", _SyncThread), \
             mock.patch("sales.ogoh.send", return_value=True) as send:
            self.assertTrue(ogoh.once("kassa-navbat:3", "birinchi"))
            self.assertFalse(ogoh.once("kassa-navbat:3", "ikkinchi"))
            self.assertTrue(ogoh.once("kassa-navbat:4", "boshqa kassa"))
        self.assertEqual([c.args[0] for c in send.call_args_list], ["birinchi", "boshqa kassa"])

    @YOQILGAN
    def test_muddat_otgach_yana_yuboradi(self):
        with mock.patch("sales.ogoh.threading.Thread", _SyncThread), \
             mock.patch("sales.ogoh.send", return_value=True) as send:
            self.assertTrue(ogoh.once("m", "1"))
            caches["shared"].delete("ogoh:m")            # muddat o'tdi
            self.assertTrue(ogoh.once("m", "2"))
        self.assertEqual(send.call_count, 2)

    @YOQILGAN
    def test_telegram_rad_etsa_jim_va_token_jurnalda_yoq(self):
        err = urllib.error.HTTPError(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage", 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err), \
             self.assertLogs("sales.ogoh", "WARNING") as logs:
            self.assertFalse(ogoh.send("salom"))
        text = "\n".join(logs.output)
        self.assertIn("401", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("SINOV-maxfiy", text)

    @YOQILGAN
    def test_tarmoq_xatosi_ham_jim(self):
        for exc in (urllib.error.URLError(f"bot{TOKEN} ulanmadi"), TimeoutError(), OSError("uzildi")):
            with self.subTest(exc=type(exc).__name__), \
                 mock.patch("urllib.request.urlopen", side_effect=exc), \
                 self.assertLogs("sales.ogoh", "WARNING") as logs:
                self.assertFalse(ogoh.send("salom"))
                self.assertNotIn(TOKEN, "\n".join(logs.output))

    @YOQILGAN
    def test_kesh_ishlamasa_ham_xato_kotarmaydi(self):
        broken = mock.MagicMock()
        broken.add.side_effect = RuntimeError("jadval yo'q")
        with mock.patch("sales.ogoh.caches") as fake, \
             self.assertLogs("sales.ogoh", "ERROR"):
            fake.__getitem__.return_value = broken
            self.assertFalse(ogoh.once("m", "salom"))


class KassaOgohTest(ApiTestCase):
    """hello va avto smena egaga xabar beradi (faqat yoqilgan bo'lsa)."""

    UUID = "3f2b8c1e-7a44-4d0b-9f65-0c1d2e3f4a5b"

    def setUp(self):
        super().setUp()
        cache.clear()
        caches["shared"].clear()

    def hello(self, **params):
        return self.client.get("/api/v1/hello", params, **self.auth())

    @YOQILGAN
    def test_tiqilgan_cheklar_haqida_xabar(self):
        with mock.patch("sales.ogoh.once") as once:
            r = self.hello(local_pending=119, local_stuck=119,
                           local_error=f"{self.UUID}: Ochiq smena yo'q")
        self.assertEqual(r.status_code, 200)
        key, text = once.call_args.args
        self.assertEqual(key, f"kassa-navbat:{self.register.pk}")
        self.assertIn("Kassa-1", text)
        self.assertIn("119 ta chekni server qabul qilmadi: Ochiq smena yo'q", text)
        self.assertIn("Ma'lumotlarni yangilash", text)

    @YOQILGAN
    def test_oddiy_navbat_uchun_xabar_yoq(self):
        with mock.patch("sales.ogoh.once") as once:
            self.hello(local_pending=1, local_stuck=0)       # chek yo'lda
            self.hello(local_pending=0, local_stuck=0)
            self.hello()
        once.assert_not_called()

    def test_ochirilgan_bolsa_hello_xabar_tayyorlamaydi(self):
        with mock.patch("sales.ogoh.once") as once:
            self.hello(local_pending=5, local_stuck=5, local_error=f"{self.UUID}: Ochiq smena yo'q")
            self.post("/api/v1/sales", self.sale_payload())      # avto smena
        self.assertTrue(all(call.args[0].startswith("avto-smena:") for call in once.call_args_list))

    @YOQILGAN
    def test_xabar_xatosi_hello_ni_buzmaydi(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("uzildi")), \
             mock.patch("sales.ogoh.threading.Thread", _SyncThread):
            r = self.hello(local_pending=5, local_stuck=5,
                           local_error=f"{self.UUID}: Ochiq smena yo'q")
        self.assertEqual(r.status_code, 200)

    @YOQILGAN
    def test_avto_smena_haqida_xabar(self):
        with mock.patch("sales.ogoh.once") as once:
            self.assertEqual(self.post("/api/v1/sales", self.sale_payload()).status_code, 201)
            self.assertEqual(self.post("/api/v1/sales", self.sale_payload()).status_code, 201)
        keys = [c.args[0] for c in once.call_args_list]
        self.assertEqual(keys, [f"avto-smena:{self.register.pk}"])      # faqat ochilganda
        self.assertIn("smenasiz sotyapti", once.call_args.args[1])
