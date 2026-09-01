"""
Navbatdagi cheklarni MoySklad'ga yuboradi.

    python manage.py sync_sales --dry-run    # nima yuborilishini ko'rish
    python manage.py sync_sales              # haqiqiy yuborish
    python manage.py sync_sales --stuck      # tiqilib qolganlarni ko'rish

Railway'da cron sifatida har 1-2 daqiqada ishlatiladi.

Qayta urinish oralig'i o'sib boradi: 1, 2, 4, 8... daqiqa. Sabab —
MoySklad bir xil xatoli so'rov takrorlanaversa API'ni butunlay o'chirib
qo'yadi. Shoshilgandan ko'ra kutgan yaxshi.
"""

import json
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from moysklad.client import MoySkladClient
from sales.models import Sale
from sales.writer import SaleWriter, SumMismatch, WriteError

BACKOFF_MINUTES = [1, 2, 4, 8, 15, 30, 60]


class Command(BaseCommand):
    help = "Navbatdagi cheklarni MoySklad'ga yuboradi"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Hech narsa yubormaydi, JSON'ni ko'rsatadi")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--stuck", action="store_true",
                            help="Tiqilib qolgan cheklarni ko'rsatadi")
        parser.add_argument("--retry-stuck", action="store_true",
                            help="Tiqilganlarni navbatga qaytaradi")

    def handle(self, *args, **o):
        if o["stuck"]:
            return self.show_stuck()
        if o["retry_stuck"]:
            return self.retry_stuck()

        now = timezone.now()
        queue = (
            Sale.objects.filter(
                sync_status__in=[Sale.NEW, Sale.FAILED]
            )
            .filter(models_q(now))
            .select_related("shift__register__store", "customer")
            .order_by("created_at")[: o["limit"]]
        )
        queue = list(queue)

        if not queue:
            self.stdout.write("Navbat bo'sh.")
            return

        if o["dry_run"]:
            return self.dry_run(queue)

        if not settings.MOYSKLAD_TOKEN:
            self.stderr.write(self.style.ERROR("MOYSKLAD_TOKEN sozlanmagan"))
            return

        client = MoySkladClient(token=settings.MOYSKLAD_TOKEN)
        writer = SaleWriter(client)

        sent = failed = 0
        for sale in queue:
            try:
                writer.send(sale)
            except SumMismatch as e:
                # Hujjat yozildi, lekin raqam mos kelmadi. Qayta yuborish
                # yordam bermaydi — odam ko'rishi kerak.
                self.mark_stuck(sale, str(e))
                failed += 1
                self.stderr.write(self.style.ERROR(f"  ✗ {sale} — {e}"))
            except WriteError as e:
                self.mark_failed(sale, str(e))
                failed += 1
                self.stderr.write(self.style.WARNING(f"  ! {sale} — {e}"))
            else:
                sale.sync_status = Sale.SENT
                sale.synced_at = timezone.now()
                sale.sync_error = ""
                sale.next_attempt_at = None
                sale.save(update_fields=[
                    "sync_status", "synced_at", "sync_error", "next_attempt_at"
                ])
                sent += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Yuborildi: {sent}"))
        if failed:
            self.stdout.write(self.style.WARNING(f"Xato: {failed}"))

    # ------------------------------------------------------------------

    def dry_run(self, queue):
        client = MoySkladClient(token=settings.MOYSKLAD_TOKEN or "dry-run")
        writer = SaleWriter(client, dry_run=True)

        for sale in queue[:3]:  # uchtasi yetarli, qolganini ko'rish shart emas
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== {sale} ==="))
            try:
                writer.send(sale)
            except WriteError as e:
                self.stderr.write(self.style.ERROR(f"  ✗ {e}"))
                continue
            for entity, payload in writer.payloads:
                self.stdout.write(f"\nPOST entity/{entity}")
                self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False))
            writer.payloads.clear()

        self.stdout.write("")
        self.stdout.write(f"Navbatda jami: {len(queue)} chek")
        self.stdout.write(
            self.style.WARNING(
                "Bu faqat ko'rsatish edi. Hech narsa yuborilmadi."
            )
        )

    def mark_failed(self, sale, error):
        sale.sync_attempts += 1
        sale.sync_error = error[:2000]

        if sale.sync_attempts >= settings.SYNC_MAX_ATTEMPTS:
            sale.sync_status = Sale.STUCK
            sale.next_attempt_at = None
        else:
            sale.sync_status = Sale.FAILED
            idx = min(sale.sync_attempts - 1, len(BACKOFF_MINUTES) - 1)
            sale.next_attempt_at = timezone.now() + timedelta(
                minutes=BACKOFF_MINUTES[idx]
            )

        sale.save(update_fields=[
            "sync_attempts", "sync_error", "sync_status", "next_attempt_at"
        ])

    def mark_stuck(self, sale, error):
        sale.sync_attempts += 1
        sale.sync_error = error[:2000]
        sale.sync_status = Sale.STUCK
        sale.next_attempt_at = None
        sale.save(update_fields=[
            "sync_attempts", "sync_error", "sync_status", "next_attempt_at"
        ])

    def show_stuck(self):
        rows = Sale.objects.filter(sync_status=Sale.STUCK).select_related("shift")
        if not rows:
            self.stdout.write("Tiqilib qolgan chek yo'q.")
            return
        self.stdout.write(self.style.WARNING(f"{rows.count()} ta chek tiqilib qolgan:"))
        for s in rows[:50]:
            self.stdout.write(f"\n  [{s.pk}] {s} · {s.created_at:%d.%m %H:%M} · "
                              f"{s.net_total // 100} so'm")
            self.stdout.write(f"      {s.sync_error[:200]}")

    def retry_stuck(self):
        n = Sale.objects.filter(sync_status=Sale.STUCK).update(
            sync_status=Sale.NEW, sync_attempts=0, next_attempt_at=None
        )
        self.stdout.write(self.style.SUCCESS(f"{n} ta chek navbatga qaytarildi."))


def models_q(now):
    """Vaqti kelgan cheklar: yangi, yoki kutish muddati o'tgan."""
    from django.db.models import Q

    return Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
