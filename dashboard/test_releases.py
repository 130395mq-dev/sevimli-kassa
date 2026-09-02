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


class RegisterCreateTest(TestCase):
    """Kassa yaratishda ombor so'raladi va o'sha ombor biriktiriladi."""

    def setUp(self):
        from catalog.models import Warehouse

        User.objects.create_superuser("admin2", "a2@a.uz", "admin")
        self.client.login(username="admin2", password="admin")
        self.wh = Warehouse.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000b2", name="Sevimli Shaxar"
        )

    def test_omborsiz_yaratilmaydi(self):
        from sales.models import Register

        r = self.client.post("/kassalar/", {
            "action": "create", "name": "Kassa-1", "login": "shaxar-1",
            "password": "1234",
        }, follow=True)
        self.assertEqual(Register.objects.count(), 0)
        self.assertIn("Ombor", r.content.decode())

    def test_ombor_bilan_yaratiladi(self):
        from sales.models import Register

        self.client.post("/kassalar/", {
            "action": "create", "name": "Kassa-1", "login": "shaxar-1",
            "password": "1234", "warehouse": str(self.wh.ms_id),
        }, follow=True)
        reg = Register.objects.get()
        self.assertEqual(str(reg.warehouse_ms_id), str(self.wh.ms_id))
        self.assertEqual(reg.warehouse_name, "Sevimli Shaxar")


class InstallerPageTest(TestCase):
    """Yangi kassaga o'rnatish sahifasi — kirishsiz ochilishi kerak."""

    def test_versiyasiz_ochiladi(self):
        r = self.client.get("/ornatish/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("chiqarilmagan", r.content.decode())
        # Fayl yo'q — 404
        self.assertEqual(self.client.get("/ornatish/fayl/").status_code, 404)

    def test_faylni_beradi(self):
        content = b"MZ" + b"x" * 1_100_000
        with tempfile.TemporaryDirectory() as d, self.settings(MEDIA_ROOT=d):
            rel = KassaRelease(version="1.2.0", size=len(content),
                               sha256=hashlib.sha256(content).hexdigest())
            rel.file.save("SevimliKassa-1.2.0.exe",
                          SimpleUploadedFile("x.exe", content), save=True)

            r = self.client.get("/ornatish/")
            self.assertEqual(r.status_code, 200)
            self.assertIn("1.2.0", r.content.decode())

            r = self.client.get("/ornatish/fayl/")
            self.assertEqual(r.status_code, 200)
            self.assertIn("SevimliKassa.exe", r["Content-Disposition"])
            self.assertEqual(b"".join(r.streaming_content), content)


class CashiersPageTest(TestCase):
    """Kassir: faqat login va parol. Ism so'ralmaydi."""

    def setUp(self):
        User.objects.create_superuser("admin2", "a2@a.uz", "admin")
        self.client.login(username="admin2", password="admin")

    def test_ismsiz_qoshiladi(self):
        from sales.models import Cashier

        r = self.client.post("/kassirlar/", {
            "action": "create", "login": "nilufar", "pin": "1234",
        }, follow=True)
        self.assertEqual(r.status_code, 200)
        c = Cashier.objects.get()
        self.assertEqual(c.login, "nilufar")
        self.assertEqual(c.name, "nilufar")
        self.assertTrue(c.check_password("1234"))

        # Loginsiz qo'shilmaydi
        self.client.post("/kassirlar/", {"action": "create", "pin": "1234"})
        self.assertEqual(Cashier.objects.count(), 1)

    def test_butunlay_ochirish(self):
        from sales.models import Cashier

        c = Cashier(name="Eski", login="eski")
        c.set_password("1234")
        c.save()

        r = self.client.post("/kassirlar/", {
            "action": "delete", "id": c.pk,
        }, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Cashier.objects.count(), 0)
        self.assertIn("butunlay o", r.content.decode())
