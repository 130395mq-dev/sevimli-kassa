"""
Sinov uchun namuna ma'lumot.

Bu buyruq MoySklad'ga hech narsa yozmaydi va undan hech narsa o'qimaydi —
faqat lokal bazaga o'ylab topilgan smena qo'yadi, chek qanday chiqishini
ko'rish uchun.

    python manage.py seed_demo
    python manage.py shift_receipt

Ishlab chiqarish bazasida ishlatmang.
"""

import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from catalog.models import RetailStore
from sales.models import (
    CashOperation,
    Payment,
    PaymentMethod,
    Register,
    Sale,
    SaleItem,
    Shift,
)

METHODS = [
    ("naqd", "Naqd", True, 1),
    ("terminal-1", "Terminal-1", False, 2),
    ("terminal-2", "Terminal-2", False, 3),
    ("click", "Click", False, 4),
    ("payme", "Payme", False, 5),
]

# Namuna tovar ID'lari — haqiqiy emas, faqat JSON to'liq ko'rinsin deb
PRODUCT_IDS = {
    "Buhanka S": "00000000-0000-0000-0000-000000000101",
    "Sut 1 l": "00000000-0000-0000-0000-000000000102",
    "Shakar 1 kg": "00000000-0000-0000-0000-000000000103",
    "Choy Akbar": "00000000-0000-0000-0000-000000000104",
    "Yog' 1 l": "00000000-0000-0000-0000-000000000105",
    "Tuxum 10 dona": "00000000-0000-0000-0000-000000000106",
}

GOODS = [
    ("Buhanka S", 3_000_00),
    ("Sut 1 l", 12_000_00),
    ("Shakar 1 kg", 10_000_00),
    ("Choy Akbar", 6_000_00),
    ("Yog' 1 l", 25_000_00),
    ("Tuxum 10 dona", 18_000_00),
]


class Command(BaseCommand):
    help = "Sinov uchun namuna smena yaratadi (faqat lokal baza)"

    def add_arguments(self, parser):
        parser.add_argument("--receipts", type=int, default=40)

    @transaction.atomic
    def handle(self, *args, **o):
        random.seed(42)  # har safar bir xil natija — solishtirish oson bo'lsin

        for code, name, is_cash, sort in METHODS:
            PaymentMethod.objects.get_or_create(
                code=code, defaults={"name": name, "is_cash": is_cash, "sort": sort}
            )
        methods = {m.code: m for m in PaymentMethod.objects.all()}

        store, _ = RetailStore.objects.get_or_create(
            ms_id="00000000-0000-0000-0000-0000000000de",
            defaults={
                "name": "Namuna filiali",
                # Bular haqiqiy MoySklad ID'lari emas — dry-run'da JSON
                # qanday chiqishini ko'rish uchun o'ylab topilgan.
                "organization_ms_id": "00000000-0000-0000-0000-0000000000a1",
                "store_ms_id": "00000000-0000-0000-0000-0000000000b2",
            },
        )
        register, _ = Register.objects.get_or_create(
            code="demo-kassa", defaults={"name": "Kassa-2", "store": store}
        )

        number = (
            Shift.objects.filter(register=register).order_by("-number").values_list("number", flat=True).first() or 0
        ) + 1
        opened = timezone.now() - timedelta(hours=9)
        shift = Shift.objects.create(
            register=register,
            number=number,
            cashier="Rahimova Nilufar",
            opened_at=opened,
            opening_cash=300_000_00,
        )

        cashless = [methods[c] for c in ("terminal-1", "terminal-2", "click", "payme")]

        for i in range(1, o["receipts"] + 1):
            lines = random.sample(GOODS, random.randint(1, 3))
            gross = 0
            items = []
            for pos, (name, price) in enumerate(lines, start=1):
                qty = Decimal(random.choice(["1.000", "2.000", "0.750"]))
                total = int(price * qty)
                gross += total
                items.append((pos, name, qty, price, total))

            # Har beshinchi chekda chegirma
            discount = int(gross * 0.05) if i % 5 == 0 else 0
            net = gross - discount

            sale = Sale.objects.create(
                shift=shift,
                number=i,
                created_at=opened + timedelta(minutes=i * 11),
                gross_total=gross,
                discount_total=discount,
                net_total=net,
                points_earned=net // 100 // 100,  # 100 so'm = 1 ball
            )
            for pos, name, qty, price, total in items:
                SaleItem.objects.create(
                    sale=sale, position=pos, name=name,
                    quantity=qty, price=price, total=total,
                    ms_product_id=PRODUCT_IDS[name],
                )

            if i % 4 == 0:
                # Aralash to'lov: yarmi naqd, yarmi karta
                half = net // 2
                Payment.objects.create(sale=sale, method=methods["naqd"], amount=half)
                Payment.objects.create(
                    sale=sale, method=random.choice(cashless), amount=net - half
                )
            elif i % 3 == 0:
                Payment.objects.create(
                    sale=sale, method=random.choice(cashless), amount=net
                )
            else:
                tendered = ((net // 50_000_00) + 1) * 50_000_00
                Payment.objects.create(
                    sale=sale, method=methods["naqd"], amount=net,
                    tendered=tendered, change=tendered - net,
                )

        # Bitta qaytarish
        first = shift.sales.filter(kind=Sale.SALE).first()
        ret = Sale.objects.create(
            shift=shift, kind=Sale.RETURN, number=1, origin=first,
            created_at=timezone.now() - timedelta(hours=1),
            gross_total=first.net_total, net_total=first.net_total,
        )
        Payment.objects.create(sale=ret, method=methods["naqd"], amount=first.net_total)

        CashOperation.objects.create(
            shift=shift, kind=CashOperation.IN, amount=200_000_00, comment="Razmen"
        )
        CashOperation.objects.create(
            shift=shift, kind=CashOperation.OUT, amount=1_000_000_00,
            comment="Inkassatsiya",
        )

        self.stdout.write(self.style.SUCCESS(f"✓ Namuna smena yaratildi: ID {shift.pk}"))
        self.stdout.write("")
        self.stdout.write("Chekni ko'rish:")
        self.stdout.write(f"  python manage.py shift_receipt --shift {shift.pk}")
        self.stdout.write("Yopish:")
        self.stdout.write(
            f"  python manage.py shift_receipt --shift {shift.pk} --close --counted <sanalgan>"
        )
