"""
Panel superuser'ini XAVFSIZ tarzda ta'minlaydi — muhit o'zgaruvchisidan.

Nega: ilgari deploy'да `setup_local` ishlar, u esa panelga `admin/admin`
superuser yaratardi — bu har kim panelga kira olishi degani (xavfsizlik
teshigi). Endi parol KODDA emas, Railway'ning maxfiy o'zgaruvchisида turadi.

Sozlash (Railway -> service -> Variables):
    ADMIN_USERNAME   (ixtiyoriy, standart: admin)
    ADMIN_PASSWORD   (MAJBURIY — kuchli parol qo'ying)
    ADMIN_EMAIL      (ixtiyoriy)

Har deploy'да parol shu o'zgaruvchiga tenglashtiriladi (haqiqat manbai —
Railway). ADMIN_PASSWORD berilmasa — hech narsa o'zgarmaydi (mavjud admin
qulflanib qolmasin), lekin ogohlantirish chiqadi.

Demo ma'lumot (kassir, tovar, kassa) YARATMAYDI — faqat superuser.
"""

import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Panel superuser'ini ADMIN_PASSWORD muhit o'zgaruvchisidan ta'minlaydi."

    def handle(self, *args, **opts):
        username = (os.environ.get("ADMIN_USERNAME") or "admin").strip()
        password = (os.environ.get("ADMIN_PASSWORD") or "").strip()
        email = (os.environ.get("ADMIN_EMAIL") or "").strip()

        if not password:
            self.stdout.write(self.style.WARNING(
                "ADMIN_PASSWORD o'rnatilmagan — superuser o'zgartirilmadi. "
                "Railway'da ADMIN_PASSWORD ni qo'shing (kuchli parol)."
            ))
            return

        user, created = User.objects.get_or_create(
            username=username,
            defaults={"email": email, "is_staff": True, "is_superuser": True},
        )
        user.is_staff = True
        user.is_superuser = True
        if email:
            user.email = email
        user.set_password(password)
        user.save()

        self.stdout.write(self.style.SUCCESS(
            f"Superuser {'yaratildi' if created else 'yangilandi'}: {username}"
        ))
