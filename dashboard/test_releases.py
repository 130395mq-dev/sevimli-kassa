import tempfile, hashlib
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from sales.models import KassaRelease

class ReleasesPageTest(TestCase):
    def setUp(self):
        User.objects.create_superuser("admin", "a@a.uz", "admin")
        self.client.login(username="admin", password="admin")

    def test_yuklash_va_royxat(self):
        with tempfile.TemporaryDirectory() as d, self.settings(MEDIA_ROOT=d):
            content = b"MZ" + b"x" * 1_100_000
            r = self.client.post("/versiyalar/", {
                "action": "upload", "version": "1.2.0", "notes": "Sinov",
                "mandatory": "1",
                "file": SimpleUploadedFile("SevimliKassa.exe", content),
            }, follow=True)
            self.assertEqual(r.status_code, 200)
            rel = KassaRelease.objects.get()
            self.assertEqual(rel.version, "1.2.0")
            self.assertTrue(rel.mandatory)
            self.assertEqual(rel.sha256, hashlib.sha256(content).hexdigest())
            self.assertEqual(rel.size, len(content))
            self.assertIn("1.2.0", r.content.decode())

            # Kichikroq versiya rad etiladi
            r = self.client.post("/versiyalar/", {
                "action": "upload", "version": "1.1.0",
                "file": SimpleUploadedFile("SevimliKassa.exe", content),
            }, follow=True)
            self.assertEqual(KassaRelease.objects.count(), 1)
            self.assertIn("katta bo&#x27;lishi kerak", r.content.decode())

    def test_kichik_fayl_rad(self):
        r = self.client.post("/versiyalar/", {
            "action": "upload", "version": "1.2.0",
            "file": SimpleUploadedFile("SevimliKassa.exe", b"MZ"),
        }, follow=True)
        self.assertEqual(KassaRelease.objects.count(), 0)
        self.assertIn("juda kichik", r.content.decode())

    def test_kassalar_sahifasi_ochiladi(self):
        self.assertEqual(self.client.get("/kassalar/").status_code, 200)
        self.assertEqual(self.client.get("/versiyalar/").status_code, 200)
