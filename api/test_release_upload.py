"""Versiyani skript orqali chiqarish — /api/v1/release/upload.

Bu yo'l CSRF va login sessiyasini talab qilmaydi (skript uchun), shuning
uchun MAXFIY KALIT yagona himoya. Kalit tekshiruvi ishlashini va noto'g'ri
fayl/versiya o'tib ketmasligini shu yerda qattiq tekshiramiz.
"""

import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from sales.models import KassaRelease

SECRET = "test-kalit-12345"


def _zip(size: int = 2_000_000, name: str = "SevimliKassa.zip"):
    return SimpleUploadedFile(name, b"x" * size, content_type="application/zip")


@override_settings(RELEASE_UPLOAD_TOKEN=SECRET)
class ReleaseUploadTest(TestCase):
    url = "/api/v1/release/upload"

    def _post(self, token=SECRET, **extra):
        data = {"version": "2.0.0", "notes": "sinov", "file": _zip()}
        data.update(extra)
        headers = {"HTTP_X_RELEASE_TOKEN": token} if token is not None else {}
        return self.client.post(self.url, data, **headers)

    # ---- xavfsizlik -------------------------------------------------

    def test_kalitsiz_rad_etiladi(self):
        r = self._post(token=None)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(KassaRelease.objects.count(), 0)

    def test_notogri_kalit_rad_etiladi(self):
        r = self._post(token="boshqa-kalit")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(KassaRelease.objects.count(), 0)

    @override_settings(RELEASE_UPLOAD_TOKEN="")
    def test_kalit_sozlanmagan_bolsa_yol_yopiq(self):
        r = self._post(token="nimadir")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(KassaRelease.objects.count(), 0)

    def test_get_qabul_qilinmaydi(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 405)

    # ---- muvaffaqiyatli yuklash -------------------------------------

    def test_togri_kalit_bilan_yuklanadi(self):
        with tempfile.TemporaryDirectory() as d, self.settings(MEDIA_ROOT=d):
            r = self._post()
            self.assertEqual(r.status_code, 200, r.content)
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["version"], "2.0.0")
            rel = KassaRelease.objects.get(version="2.0.0")
            self.assertEqual(rel.size, 2_000_000)
            self.assertEqual(len(rel.sha256), 64)

    def test_majburiy_belgisi_uzatiladi(self):
        with tempfile.TemporaryDirectory() as d, self.settings(MEDIA_ROOT=d):
            r = self._post(mandatory="1")
            self.assertEqual(r.status_code, 200, r.content)
            self.assertTrue(KassaRelease.objects.get(version="2.0.0").mandatory)

    # ---- tekshiruvlar -----------------------------------------------

    def test_zip_bolmasa_rad_etiladi(self):
        r = self._post(file=_zip(name="dastur.exe"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(KassaRelease.objects.count(), 0)

    def test_juda_kichik_fayl_rad_etiladi(self):
        r = self._post(file=_zip(size=1000))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(KassaRelease.objects.count(), 0)

    def test_notogri_versiya_rad_etiladi(self):
        r = self._post(version="salom")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(KassaRelease.objects.count(), 0)

    def test_takroriy_versiya_rad_etiladi(self):
        with tempfile.TemporaryDirectory() as d, self.settings(MEDIA_ROOT=d):
            self.assertEqual(self._post().status_code, 200)
            r = self._post()  # xuddi shu 2.0.0
            self.assertEqual(r.status_code, 400)
            self.assertIn("allaqachon", r.json()["error"])
            self.assertEqual(KassaRelease.objects.count(), 1)

    def test_eski_versiya_rad_etiladi(self):
        with tempfile.TemporaryDirectory() as d, self.settings(MEDIA_ROOT=d):
            self.assertEqual(self._post(version="2.0.0").status_code, 200)
            r = self._post(version="1.9.0")  # kichikroq
            self.assertEqual(r.status_code, 400)
            self.assertEqual(KassaRelease.objects.count(), 1)

    def test_katta_versiya_otadi(self):
        with tempfile.TemporaryDirectory() as d, self.settings(MEDIA_ROOT=d):
            self.assertEqual(self._post(version="2.0.0").status_code, 200)
            r = self._post(version="2.10.0")  # 2.10 > 2.0 (raqam bo'yicha)
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual(KassaRelease.objects.count(), 2)
