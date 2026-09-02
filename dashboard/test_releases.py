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


class RegistersPageTest(TestCase):
    """Kassa: omborni tanlash yetarli, login-parol o'zi tayyorlanadi."""

    def setUp(self):
        from catalog.models import Warehouse

        User.objects.create_superuser("admin2", "a2@a.uz", "admin")
        self.client.login(username="admin2", password="admin")
        self.wh = Warehouse.objects.create(
            ms_id="11111111-1111-1111-1111-111111111111", name="Chilonzor"
        )

    def test_faqat_ombor_bilan_yaratiladi(self):
        from sales.models import Register

        r = self.client.post("/kassalar/", {
            "action": "create", "warehouse": str(self.wh.ms_id),
        }, follow=True)
        self.assertEqual(r.status_code, 200)

        reg = Register.objects.get()
        self.assertEqual(reg.name, "Kassa-1")
        self.assertEqual(reg.login, "chilonzor")
        self.assertEqual(len(reg.password_plain), 6)
        self.assertTrue(reg.check_password(reg.password_plain))
        self.assertEqual(str(reg.settings.warehouse_ms_id), str(self.wh.ms_id))
        # Parol panelda ko'rinib turadi
        self.assertIn(reg.password_plain, r.content.decode())

    def test_ikkinchisiga_boshqa_login(self):
        from sales.models import Register

        for _ in range(2):
            self.client.post("/kassalar/", {
                "action": "create", "warehouse": str(self.wh.ms_id),
            })
        logins = sorted(Register.objects.values_list("login", flat=True))
        self.assertEqual(logins, ["chilonzor", "chilonzor-2"])

    def test_smenasiz_kassa_ochiriladi(self):
        from sales.models import Register

        self.client.post("/kassalar/", {
            "action": "create", "warehouse": str(self.wh.ms_id),
        })
        reg = Register.objects.get()
        self.client.post("/kassalar/", {"action": "delete", "id": reg.pk})
        self.assertEqual(Register.objects.count(), 0)

    def test_smenali_kassa_ochirilmaydi_bloklanadi(self):
        from django.utils import timezone

        from sales.models import Register, Shift

        self.client.post("/kassalar/", {
            "action": "create", "warehouse": str(self.wh.ms_id),
        })
        reg = Register.objects.get()
        Shift.objects.create(
            register=reg, number=1, cashier=reg.name, opened_at=timezone.now(),
        )

        r = self.client.post(
            "/kassalar/", {"action": "delete", "id": reg.pk}, follow=True
        )
        reg.refresh_from_db()
        self.assertEqual(Register.objects.count(), 1)
        self.assertFalse(reg.active)
        self.assertIn("savdo tarixi", r.content.decode())

    def test_kassirlar_sahifasi_yoq(self):
        self.assertEqual(self.client.get("/kassirlar/").status_code, 404)


class PricesPageTest(TestCase):
    """Sotuv narxi: kassa qaysi narxda sotishi paneldan belgilanadi."""

    def setUp(self):
        from catalog.models import PriceType, Warehouse

        User.objects.create_superuser("admin3", "a3@a.uz", "admin")
        self.client.login(username="admin3", password="admin")
        self.wh = Warehouse.objects.create(
            ms_id="22222222-2222-2222-2222-222222222222", name="Ulgurji"
        )
        PriceType.objects.create(
            ms_id="33333333-3333-3333-3333-333333333333", name="Чакана нарх", sort=1
        )
        PriceType.objects.create(
            ms_id="44444444-4444-4444-4444-444444444444", name="Улгуржи нархи", sort=2
        )
        self.client.post("/kassalar/", {
            "action": "create", "warehouse": str(self.wh.ms_id),
        })

    def test_narx_biriktiriladi_va_tugma_ochadi(self):
        from sales.models import Register

        reg = Register.objects.get()
        st = reg.settings
        st.allow_price_type_switch = True
        st.save()

        r = self.client.post("/narxlar/", {
            f"pt-{reg.pk}": "Улгуржи нархи",
        }, follow=True)
        self.assertEqual(r.status_code, 200)

        st.refresh_from_db()
        self.assertEqual(st.price_type, "Улгуржи нархи")
        # Kassada almashtirish tugmasi chiqmaydi
        self.assertFalse(st.allow_price_type_switch)

    def test_sahifa_kassalarni_korsatadi(self):
        r = self.client.get("/narxlar/")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Ulgurji", body)
        self.assertIn("Чакана нарх", body)
