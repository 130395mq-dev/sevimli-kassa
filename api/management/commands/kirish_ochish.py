"""Yopilgan kirishni ochish (audit I17).

    python manage.py kirish_ochish kassa3        # shu login (hamma IP, hamma tur)
    python manage.py kirish_ochish --hammasi     # hamma cheklov

Faqat hisoblagich o'chadi: parol, kassa, sessiya, savdoga tegilmaydi.
"""
from django.core.management.base import BaseCommand, CommandError

from api import throttle


class Command(BaseCommand):
    help = "Ko'p xato urinish sababli yopilgan loginni ochadi"

    def add_arguments(self, parser):
        parser.add_argument("login", nargs="?")
        parser.add_argument("--hammasi", action="store_true")

    def handle(self, *args, login=None, hammasi=False, **opts):
        if not login and not hammasi:
            raise CommandError("Login yoki --hammasi kerak")
        n = throttle.clear(None if hammasi else login)
        self.stdout.write(f"Ochildi: {n} ta yozuv o'chirildi")
