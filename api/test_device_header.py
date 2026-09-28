"""I08: qurilma belgisi (X-Device) — bosqichli o'tish."""
from django.test import override_settings

from api.tests import ApiTestCase
from sales.models import Register


class DeviceHeaderTest(ApiTestCase):
    def _hello(self, device=None):
        extra = self.auth()
        if device:
            extra["HTTP_X_DEVICE"] = device
        return self.client.get("/api/v1/hello", **extra)

    def test_biriktirilmagan_eski_kassa_ishlayveradi(self):
        self.assertEqual(self._hello().status_code, 200)

    def test_biriktirilgan_kassa_sarlavhasiz_hozircha_otadi_va_loglanadi(self):
        self.assertEqual(self._hello("pc-1").status_code, 200)   # biriktirildi
        with self.assertLogs("api", level="WARNING") as log:
            self.assertEqual(self._hello().status_code, 200)
        self.assertIn("Qurilma belgisisiz", log.output[0])

    @override_settings(DEVICE_HEADER_REQUIRED=True)
    def test_qatiy_rejimda_biriktirilgan_kassa_sarlavhasiz_yopiq(self):
        self.assertEqual(self._hello("pc-1").status_code, 200)
        self.assertEqual(self._hello().status_code, 401)
        self.assertEqual(self._hello("pc-1").status_code, 200)

    @override_settings(DEVICE_HEADER_REQUIRED=True)
    def test_qatiy_rejimda_ham_biriktirilmagan_eski_kassa_ochiq(self):
        Register.objects.filter(pk=self.register.pk).update(device="")
        self.assertEqual(self._hello().status_code, 200)

    def test_boshqa_kompyuter_avvalgidek_yopiq(self):
        self.assertEqual(self._hello("pc-1").status_code, 200)
        self.assertEqual(self._hello("pc-2").status_code, 401)
