"""Django admin standart ko'rinishda ochilishini tekshiradi.

templates/admin/base_site.html bo'sh (hech narsa o'zgartirmaydi), lekin
shablon mavjud bo'lgani uchun undagi xato adminni butunlay yiqitishi
mumkin edi. Shuning uchun sahifalar haqiqatan ochilishini tekshiramiz.
"""

from django.contrib.auth.models import User
from django.test import TestCase, override_settings


# Testda statik fayllar «manifest» siz beriladi: collectstatic ishlamagan
# muhitda admin CSS'i yo'qligi uchun sahifa yiqilmasin.
@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
})
class AdminOchiladiTest(TestCase):
    def setUp(self):
        User.objects.create_superuser("t_admin", "t@a.uz", "pw12345")
        self.client.login(username="t_admin", password="pw12345")

    def test_admin_bosh_sahifa_ochiladi(self):
        r = self.client.get("/admin/")
        self.assertEqual(r.status_code, 200)

    def test_tolov_turlari_sahifasi_ochiladi(self):
        r = self.client.get("/admin/sales/paymentmethod/")
        self.assertEqual(r.status_code, 200)

    def test_kirish_sahifasi_ochiladi(self):
        self.client.logout()
        r = self.client.get("/admin/login/")
        self.assertEqual(r.status_code, 200)
