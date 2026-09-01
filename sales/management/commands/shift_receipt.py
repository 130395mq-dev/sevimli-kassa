"""
Smena chekini ko'rish va smenani yopish.

    python manage.py shift_receipt                    # ochiq smenalar ro'yxati
    python manage.py shift_receipt --shift 3          # oraliq hisobot
    python manage.py shift_receipt --shift 3 --close --counted 626000

`--counted` so'mda kiritiladi (tiyinda emas) — buyruqni odam yozadi.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from sales.models import Shift
from sales.services import ShiftError, build_receipt, close_shift, pending_count
from shared.receipt import render


class Command(BaseCommand):
    help = "Smena chekini chiqaradi; --close bilan smenani yopadi"

    def add_arguments(self, parser):
        parser.add_argument("--shift", type=int, help="Smena ID (baza ID'si)")
        parser.add_argument("--close", action="store_true", help="Smenani yopish")
        parser.add_argument(
            "--counted", type=int, default=None, help="Kassir sanagan naqd, so'mda"
        )
        parser.add_argument("--width", type=int, default=None, help="48 yoki 32")

    def handle(self, *args, **o):
        if not o["shift"]:
            self.list_open()
            return

        try:
            shift = Shift.objects.select_related("register__store").get(pk=o["shift"])
        except Shift.DoesNotExist:
            raise CommandError(f"Smena {o['shift']} topilmadi")

        counted = o["counted"] * 100 if o["counted"] is not None else None

        if o["close"]:
            try:
                receipt = close_shift(shift, counted_cash=counted)
            except ShiftError as e:
                raise CommandError(str(e))
        else:
            if counted is not None:
                shift.counted_cash = counted
            receipt = build_receipt(shift, market=settings.MARKET_NAME)

        self.stdout.write("")
        self.stdout.write(render(receipt, o["width"] or settings.RECEIPT_WIDTH))

        waiting = pending_count(shift)
        if waiting:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"Diqqat: {waiting} ta chek hali MoySklad'ga yetib bormagan — "
                    "navbatda turibdi."
                )
            )

    def list_open(self):
        rows = Shift.objects.filter(status=Shift.OPEN).select_related("register__store")
        if not rows:
            self.stdout.write("Ochiq smena yo'q.")
            return
        self.stdout.write("Ochiq smenalar:")
        for s in rows:
            self.stdout.write(
                f"  [{s.pk}] {s.register.store.name} · {s.register.name} "
                f"#{s.number} · {s.cashier} · {s.sales.count()} chek"
            )
