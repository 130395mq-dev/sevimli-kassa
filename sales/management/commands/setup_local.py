"""
Bitta kompyuterda sinab ko'rish uchun hammasini tayyorlaydi.

    python manage.py setup_local

Nima qiladi:
  - to'lov turlarini yaratadi (Naqd, Terminal-1, Terminal-2, Click, Payme)
  - sinov savdo nuqtasi va kassa ochadi
  - namuna tovarlar qo'yadi
  - kassa tokenini chiqaradi

MoySklad'ga umuman tegmaydi va undan hech narsa o'qimaydi. Bu — dasturni
ko'rish uchun. Haqiqiy ishga tushirishda `sync_catalog` ishlatiladi.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import Barcode, Product, RetailStore
from sales.models import PaymentMethod, Register

METHODS = [
    ("naqd", "Naqd", True, 1),
    ("terminal-1", "Terminal-1", False, 2),
    ("terminal-2", "Terminal-2", False, 3),
    ("click", "Click", False, 4),
    ("payme", "Payme", False, 5),
]

# (nom, kod, narx so'mda, shtrix-kod, vaznlimi, plu)
GOODS = [
    ("Buhanka S", "0001", 3_000, "4780001000017", False, None),
    ("Sut 1 l Nestle", "0002", 12_000, "4780001000024", False, None),
    ("Shakar 1 kg", "0003", 10_000, "4780001000031", False, None),
    ("Choy Akbar 250 g", "0004", 6_000, "4780001000048", False, None),
    ("Yog' Oleyna 1 l", "0005", 25_000, "4780001000055", False, None),
    ("Tuxum 10 dona", "0006", 18_000, "4780001000062", False, None),
    ("Go'sht mol (kg)", "0007", 95_000, "", True, 123),
    ("Tvorog (kg)", "0008", 32_000, "", True, 124),
    ("Makaron 400 g", "0009", 8_500, "4780001000093", False, None),
    ("Guruch Lazer 1 kg", "0010", 22_000, "4780001000109", False, None),
    ("Non lavash", "0011", 4_000, "4780001000116", False, None),
    ("Suv 1.5 l", "0012", 3_500, "4780001000123", False, None),
]

STORE_ID = "00000000-0000-0000-0000-00000000ffff"


class Command(BaseCommand):
    help = "Bitta kompyuterda sinash uchun hamma narsani tayyorlaydi"

    def add_arguments(self, parser):
        parser.add_argument(
            "--with-sales", action="store_true",
            help="Panelni ko'rish uchun bugungi savdolarni ham qo'shadi",
        )

    @transaction.atomic
    def handle(self, *args, **o):
        for code, name, is_cash, sort in METHODS:
            PaymentMethod.objects.get_or_create(
                code=code,
                defaults={"name": name, "is_cash": is_cash, "sort": sort},
            )

        store, _ = RetailStore.objects.get_or_create(
            ms_id=STORE_ID,
            defaults={
                "name": "Sinov filiali",
                "organization_ms_id": "00000000-0000-0000-0000-0000000000a1",
                "store_ms_id": "00000000-0000-0000-0000-0000000000b2",
            },
        )

        for i, (name, code, price, barcode, is_weight, plu) in enumerate(GOODS, start=1):
            product, _ = Product.objects.update_or_create(
                ms_id=f"00000000-0000-0000-0000-{i:012d}",
                defaults={
                    "name": name,
                    "code": code,
                    "sale_price": price * 100,
                    "is_weight": is_weight,
                    "plu": plu,
                    "uom_name": "kg" if is_weight else "dona",
                },
            )
            if barcode:
                Barcode.objects.get_or_create(product=product, value=barcode)

        register, created = Register.objects.get_or_create(
            code="sinov-kassa",
            defaults={"name": "Kassa-1", "store": store, "login": "kassa1"},
        )
        if created or not register.password_hash:
            register.login = register.login or "kassa1"
            register.set_password("1111")
            register.save()

        # Sinov kassiri
        from sales.models import Cashier

        cashier, cashier_new = Cashier.objects.get_or_create(
            login="nilufar",
            defaults={"name": "Rahimova Nilufar", "is_manager": True},
        )
        if cashier_new or not cashier.pin_hash:
            cashier.set_pin("1234")
            cashier.save()

        # Panelga kirish uchun foydalanuvchi. Faqat sinov rejimi uchun:
        # ishlab chiqarishda `python manage.py createsuperuser` bilan
        # o'z parolingizni qo'yasiz.
        from django.contrib.auth.models import User

        if not User.objects.filter(username="admin").exists():
            User.objects.create_superuser("admin", "", "admin")

        if o["with_sales"]:
            self.make_sales(store, register)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("✓ Hammasi tayyor"))
        self.stdout.write("")
        self.stdout.write(f"  Nuqta   : {store.name}")
        self.stdout.write(f"  Kassa   : {register.name}")
        self.stdout.write(f"  Tovarlar: {Product.objects.count()} ta")
        self.stdout.write("")
        self.stdout.write("  Server  : http://127.0.0.1:8000")
        self.stdout.write("")
        self.stdout.write("  Kassa logini : kassa1")
        self.stdout.write("  Kassa paroli : 1111")
        self.stdout.write("")
        self.stdout.write("  Kassir       : Rahimova Nilufar")
        self.stdout.write("  PIN          : 1234")
        self.stdout.write("")
        self.stdout.write("  Panel        : admin / admin")
        self.stdout.write("")
        if not created:
            self.stdout.write(
                self.style.WARNING(
                    "  (kassa avvaldan bor edi, tokeni o'zgarmadi)"
                )
            )

    def make_sales(self, store, register):
        """Panel bo'sh ko'rinmasin — bugungi savdolarni o'ylab topamiz.

        Bu faqat ko'rsatish uchun. Haqiqiy savdo kassadan keladi.
        """
        import random
        from datetime import timedelta
        from decimal import Decimal

        from django.utils import timezone

        from sales.models import Payment, Sale, SaleItem, Shift

        random.seed(7)

        # Ikkinchi nuqta — panel bir nechta nuqtani qanday ko'rsatishini
        # ko'rish uchun
        other, _ = RetailStore.objects.get_or_create(
            ms_id="00000000-0000-0000-0000-00000000fffe",
            defaults={"name": "Yunusobod filiali"},
        )
        second, _ = Register.objects.get_or_create(
            code="sinov-kassa-2",
            defaults={"name": "Kassa-1", "store": other},
        )

        methods = {m.code: m for m in PaymentMethod.objects.all()}
        cashless = [methods[c] for c in ("terminal-1", "click", "payme")]
        goods = list(Product.objects.all())

        now = timezone.now()
        opened = now.replace(hour=8, minute=0, second=0, microsecond=0)

        for reg, count in ((register, 34), (second, 21)):
            if reg.shifts.exists():
                continue
            shift = Shift.objects.create(
                register=reg, number=1,
                cashier="Rahimova Nilufar" if reg == register else "Karimov Aziz",
                opened_at=opened, opening_cash=300_000_00,
            )
            for i in range(1, count + 1):
                lines = random.sample(goods, random.randint(1, 3))
                total = 0
                sale = Sale.objects.create(
                    shift=shift, number=i,
                    created_at=opened + timedelta(minutes=i * 13),
                    sync_status=Sale.SENT,
                )
                for pos, product in enumerate(lines, start=1):
                    qty = Decimal("0.750") if product.is_weight else Decimal(
                        random.choice(["1", "1", "2"])
                    )
                    amount = int(product.sale_price * qty)
                    total += amount
                    SaleItem.objects.create(
                        sale=sale, position=pos, product=product,
                        ms_product_id=product.ms_id, name=product.name,
                        quantity=qty, price=product.sale_price, total=amount,
                    )
                sale.gross_total = total
                sale.net_total = total
                sale.points_earned = total // 100 // 100
                sale.save()

                if i % 3 == 0:
                    Payment.objects.create(
                        sale=sale, method=random.choice(cashless), amount=total
                    )
                else:
                    tendered = ((total // 50_000_00) + 1) * 50_000_00
                    Payment.objects.create(
                        sale=sale, method=methods["naqd"], amount=total,
                        tendered=tendered, change=tendered - total,
                    )

            # Bittasi ataylab «tiqilib qolgan» — panel xatoni qanday
            # ko'rsatishini ko'rish uchun
            last = shift.sales.order_by("-number").first()
            if reg == register and last:
                last.sync_status = Sale.STUCK
                last.sync_attempts = 12
                last.sync_error = (
                    "«Guruch Lazer 1 kg» MoySklad'da topilmadi — "
                    "tovar arxivga olingan bo'lishi mumkin"
                )
                last.save()

        # Birinchi kassa hozir ulangan, ikkinchisi — yo'q
        register.last_seen_at = now
        register.save(update_fields=["last_seen_at"])
        second.last_seen_at = now - timedelta(minutes=40)
        second.save(update_fields=["last_seen_at"])
