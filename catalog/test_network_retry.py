"""MoySklad bir sahifani 60 s ichida bermasa — sinxronizatsiya yiqilmasin.

2026-10-12 21:38 da `sync` xizmati «Crashed» bo'ldi: qoldiq hisobotining
(63 sahifa) bitta sahifasi 60 s ichida kelmadi, `requests` tarmoq xatosini
otdi, bizning mijoz faqat HTTP 5xx/429 ni qayta urinardi — jarayon uzun
traceback bilan to'xtadi. 1-oktabrdan beri bu 12 marta bo'lgan.
"""

from unittest.mock import MagicMock, patch

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from moysklad.client import MoySkladClient


def _ok(payload):
    response = MagicMock()
    response.status_code = 200
    response.content = b"{}"
    response.json.return_value = payload
    response.headers = {}
    return response


def _read_timeout():
    return requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='api.moysklad.ru', port=443): Read timed out."
    )


@patch("moysklad.client.time.sleep")
class NetworkRetryTest(SimpleTestCase):
    def _client(self, retries, outcomes):
        client = MoySkladClient(token="t", network_retries=retries)
        client._session = MagicMock()
        client._session.request.side_effect = outcomes
        return client

    def test_get_qayta_soraladi_va_javob_qaytadi(self, sleep):
        client = self._client(3, [_read_timeout(), requests.exceptions.ReadTimeout("60"),
                                  _ok({"rows": [1]})])
        self.assertEqual(client.get("report/stock/bystore"), {"rows": [1]})
        self.assertEqual(client._session.request.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10])

    def test_uzilgan_sahifa_ham_qayta_soraladi(self, sleep):
        broken = requests.exceptions.ChunkedEncodingError("Connection reset by peer")
        client = self._client(3, [broken, _ok({"rows": []})])
        self.assertEqual(client.get("entity/product"), {"rows": []})

    def test_urinishlar_tugasa_asl_xato_otiladi(self, sleep):
        client = self._client(3, [_read_timeout() for _ in range(10)])
        with self.assertRaises(requests.ConnectionError):
            client.get("report/stock/bystore")
        self.assertEqual(client._session.request.call_count, 4)  # 1 + 3 qayta

    def test_post_hech_qachon_takrorlanmaydi(self, sleep):
        # Javob yo'qolgan bo'lsa ham hujjat yozilgan bo'lishi mumkin —
        # takrorlash MoySklad'da dublikat chek yaratardi.
        client = self._client(3, [_read_timeout(), _ok({"id": "x"})])
        with self.assertRaises(requests.ConnectionError):
            client.post("entity/retaildemand", {"name": "1"})
        self.assertEqual(client._session.request.call_count, 1)
        sleep.assert_not_called()

    def test_standart_mijoz_avvalgidek_darhol_xato_beradi(self, sleep):
        # Panel/kassa so'rovlari uchun hech narsa o'zgarmagan: kutmaydi.
        client = MoySkladClient(token="t")
        client._session = MagicMock()
        client._session.request.side_effect = [_read_timeout(), _ok({})]
        with self.assertRaises(requests.ConnectionError):
            client.get("entity/product")
        self.assertEqual(client._session.request.call_count, 1)

    def test_http_xato_urinishlari_tarmoq_bilan_aralashmaydi(self, sleep):
        bad = MagicMock(status_code=503, content=b"x", text="x", headers={})
        bad.json.side_effect = ValueError
        client = self._client(3, [_read_timeout(), bad, _ok({"rows": [2]})])
        self.assertEqual(client.get("entity/product"), {"rows": [2]})
        self.assertEqual(client._session.request.call_count, 3)


@override_settings(MOYSKLAD_TOKEN="test-token")
class SyncCommandNetworkTest(TestCase):
    def _fake_client(self, **kwargs):
        client = MagicMock()
        client.iter_list.side_effect = _read_timeout()
        client.state.limit = None
        self.created_with = kwargs
        return client

    def test_tarmoq_xatosi_qisqa_xabar_va_holat_yoziladi(self):
        from io import StringIO

        from catalog.models import SyncState

        out = StringIO()
        with patch("catalog.management.commands.sync_catalog.MoySkladClient",
                   side_effect=self._fake_client):
            with self.assertRaises(CommandError) as ctx:
                call_command("sync_catalog", "--only", "stock", stdout=out)

        self.assertIn("qayta uriniladi", str(ctx.exception))
        self.assertIn("MoySklad javob bermadi", out.getvalue())
        self.assertEqual(self.created_with.get("network_retries"), 3)
        state = SyncState.objects.get(entity="stock")
        self.assertIn("Read timed out", state.last_error)
        self.assertIsNone(state.last_success_at)
