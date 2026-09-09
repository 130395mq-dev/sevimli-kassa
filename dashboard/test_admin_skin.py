"""Django admin panel dizayni — Sevimli Kassa ranglarida ochilishi.

templates/admin/base_site.html admin'ni panel palitrasiga bo'yaydi.
Shablonda xato bo'lsa admin butunlay ochilmay qoladi — shuning uchun
sahifalar haqiqatan ochilishini va rang qo'llanganini tekshiramiz.
"""

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

# Testda statik fayllar «manifest» siz beriladi: collectstatic ishlamagan
# muhitda admin CSS'i yo'qligi uchun sahifa yiqilmasin. Bu dizaynga aloqasiz.
@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
})
class AdminSkinTest(TestCase):
    def setUp(self):
        User.objects.create_superuser("t_admin", "t@a.uz", "pw12345")
        self.client.login(username="t_admin", password="pw12345")

    def test_admin_bosh_sahifa_ochiladi(self):
        r = self.client.get("/admin/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Sevimli Kassa")

    def test_panel_rangi_qollangan(self):
        r = self.client.get("/admin/")
        self.assertContains(r, "#235347")  # panelning asosiy yashili

    def test_tolov_turlari_sahifasi_ochiladi(self):
        r = self.client.get("/admin/sales/paymentmethod/")
        self.assertEqual(r.status_code, 200)

    def test_kirish_sahifasi_ochiladi(self):
        self.client.logout()
        r = self.client.get("/admin/login/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "#235347")
