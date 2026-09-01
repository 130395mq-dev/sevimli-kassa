"""
Mijozlarning bonus balanslarini faylga chiqaradi — zaxira nusxa uchun.

Bu ko'chirish emas. Ballar MoySklad'da qoladi. Bu shunchaki sanalangan
suratkash: agar keyinchalik biror narsa noto'g'ri yozilsa, nimadan
boshlanganini bilish uchun.

Ishlatilishi:

    python manage.py sync_catalog --only customers    # avval yangilang
    python manage.py export_bonuses                   # keyin chiqaring

Natija: bonus-zaxira-2026-08-31.csv

CSV Excel'da ochiladi. Agar raqamlar birlashib ketsa — Excel'da
"Данные → Из текста" orqali oching va ajratgich sifatida ";" ni tanlang.
"""

import csv
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db.models import Sum

from catalog.models import Customer


class Command(BaseCommand):
    help = "Mijozlarning bonus balanslarini CSV faylga chiqaradi (zaxira uchun)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--out",
            default="",
            help="Fayl nomi. Ko'rsatilmasa: bonus-zaxira-YYYY-MM-DD.csv",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Bali yo'q mijozlarni ham chiqaradi",
        )

    def handle(self, *args, **options):
        qs = Customer.objects.filter(archived=False)
        if not options["all"]:
            qs = qs.exclude(bonus_points=0)
        qs = qs.order_by("-bonus_points")

        total_people = qs.count()
        if total_people == 0:
            self.stdout.write(
                self.style.WARNING(
                    "Bali bor mijoz topilmadi. Avval sinxronizatsiya qiling:\n"
                    "  python manage.py sync_catalog --only customers"
                )
            )
            return

        path = Path(options["out"] or f"bonus-zaxira-{date.today():%Y-%m-%d}.csv")

        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            # utf-8-sig — Excel kirillcha va o'zbekcha harflarni to'g'ri o'qishi uchun
            # ";" — Excel'ning rus/o'zbek versiyasidagi standart ajratgich
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(
                [
                    "MoySklad ID",
                    "Ism",
                    "Telefon",
                    "Diskont karta",
                    "Bonus ball",
                    "Nakopitelniy %",
                    "Shaxsiy chegirma %",
                    "Umumiy savdo (so'm)",
                ]
            )
            for c in qs.iterator(chunk_size=500):
                writer.writerow(
                    [
                        c.ms_id,
                        c.name,
                        c.phone,
                        c.discount_card,
                        c.bonus_points,
                        c.accumulation_discount,
                        c.personal_discount,
                        c.sales_amount // 100,
                    ]
                )

        total_points = qs.aggregate(t=Sum("bonus_points"))["t"] or 0

        self.stdout.write(self.style.SUCCESS(f"✓ Tayyor: {path}"))
        self.stdout.write(f"  Mijozlar     : {total_people}")
        self.stdout.write(f"  Jami ballar  : {total_points}")
        self.stdout.write(
            f"  So'mda       : {total_points}  (1 ball = 1 so'm)"
        )
        self.stdout.write("")
        self.stdout.write(
            "Bu — sizning mijozlar oldidagi majburiyatingiz. "
            "Faylni saqlab qo'ying."
        )
