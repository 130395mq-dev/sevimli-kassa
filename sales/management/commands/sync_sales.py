"""
Navbatdagi cheklarni MoySklad'ga yuboradi.

    python manage.py sync_sales --dry-run    # nima yuborilishini ko'rish
    python manage.py sync_sales              # bir marta yuborib chiqadi
    python manage.py sync_sales --loop       # to'xtovsiz (har 20 soniyada)
    python manage.py sync_sales --stuck      # tiqilib qolganlarni ko'rish

Railway'da bu buyruq `--loop` bilan TO'XTOVSIZ xizmat sifatida ishlaydi:
har ~20 soniyada navbatni tekshiradi. Shu bois chek MoySklad'ga deyarli
darhol (eng ko'pi 20 soniyada) tushadi — cron'ning 5 daqiqalik chegarasi
bu yerda yo'q.

Nega ichki (Python) sikl, `while true` (shell) EMAS:
  - Shell'ga bog'liq emas (konteynerda bash/sh bo'lmasligi mumkin).
  - Har aylanish xatoni ushlaydi — bitta xato butun xizmatni yiqitmaydi.
  - Har aylanishда DB ulanishi yangilanadi (uzoq ishlaydigan jarayonда
    eski ulanish uzilib qolishi mumkin).
  - Yurak urishi (heartbeat) log'ga yoziladi — ishlayotganи ko'rinadi.

Qayta urinish oralig'i o'sib boradi: 1, 2, 4, 8... daqiqa. Sabab —
MoySklad bir xil xatoli so'rov takrorlanaversa API'ni butunlay o'chirib
qo'yadi. Shoshilgandan ko'ra kutgan yaxshi.
"""

import json
import signal
import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from moysklad.client import MoySkladClient
from sales import selftest
from sales.models import MoySkladCheck, Sale
from sales.writer import SaleWriter, SumMismatch, WriteError

BACKOFF_MINUTES = [1, 2, 4, 8, 15, 30, 60]

#: `--loop` da ikki heartbeat oralig'i (navbat bo'sh bo'lsa ham shu
#: oraliqда bir marta «tirikman» belgisi log'ga chiqadi).
HEARTBEAT_EVERY = 30


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
        parser.add_argument("--selftest", action="store_true",
                            help="MoySklad o'z-o'zini tekshirish (sinov) — bir marta")
        parser.add_argument("--loop", action="store_true",
                            help="To'xtovsiz ishlaydi (har --interval soniyada)")
        parser.add_argument("--interval", type=int, default=20,
                            help="--loop'da tekshiruvlar orasidagi soniya")

    def handle(self, *args, **o):
        if o["stuck"]:
            return self.show_stuck()
        if o["retry_stuck"]:
            return self.retry_stuck()
        if o["selftest"]:
            return self.selftest(MoySkladCheck.MANUAL)

        if o["loop"]:
            return self.run_forever(o)

        return self.run_once(o)

    # ------------------------------------------------------------------ loop

    def run_forever(self, o):
        """To'xtovsiz xizmat: har `--interval` soniyada navbatni yuboradi.

        - Har aylanish `run_once` xatoni ushlaydi (bitta xato xizmatni
          yiqitmaydi).
        - Har aylanishда eski DB ulanishlari yopiladi (uzoq jarayonда
          ulanish uzilishi mumkin).
        - SIGTERM/SIGINT (Railway qayta deploy qilganда) — toza to'xtash.
        """
        interval = max(5, int(o.get("interval") or 20))
        stop = {"now": False}

        def _stop(signum, frame):  # pragma: no cover - signal
            stop["now"] = True
            self.stdout.write(f"\nSignal {signum} — to'xtatilyapti...")
            self.stdout.flush()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        self.stdout.write(
            f"sync_sales --loop boshlandi (har {interval} soniyada). "
            f"To'xtatish: SIGTERM."
        )
        self.stdout.flush()

        last_beat = 0.0
        # MoySklad o'z-o'zini tekshirish: xizmat ishga tushganda (deploy)
        # bir marta, keyin har 3 soatda. FAQAT navbat bo'sh bo'lganda —
        # haqiqiy cheklar har doim birinchi (sales/selftest.py).
        first_selftest = True
        while not stop["now"]:
            close_old_connections()
            try:
                self.run_once(o, quiet_when_empty=True)
            except Exception as e:  # pragma: no cover - himoya
                # Kutilmagan xato (DB uzildi, MoySklad tushdi...) — log'ga
                # yozamiz va davom etamiz. Xizmat yiqilmaydi.
                self.stderr.write(self.style.ERROR(f"Sikl xatosi: {e}"))
                self.stderr.flush()

            try:
                if self.queue_empty() and (first_selftest or selftest.is_due()):
                    trigger = MoySkladCheck.DEPLOY if first_selftest else MoySkladCheck.PERIODIC
                    first_selftest = False
                    self.selftest(trigger)
            except Exception as e:  # pragma: no cover - himoya
                self.stderr.write(self.style.ERROR(f"Sinov xatosi: {e}"))
                self.stderr.flush()

            # Yurak urishi — bo'sh bo'lsa ham vaqti-vaqti bilan «tirikman».
            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_EVERY:
                self.stdout.write(
                    f"[{timezone.now():%H:%M:%S}] tirikman, navbat kuzatilyapti."
                )
                self.stdout.flush()
                last_beat = now

            # Uyquni bo'laklab uxlaymiz — signal kelsa tez uyg'onish uchun.
            slept = 0
            while slept < interval and not stop["now"]:
                time.sleep(min(1, interval - slept))
                slept += 1

        self.stdout.write("sync_sales --loop to'xtadi.")
        self.stdout.flush()

    # ------------------------------------------------------------------ sinov

    def queue_empty(self) -> bool:
        return not Sale.objects.filter(sync_status__in=[Sale.NEW, Sale.FAILED]).exists()

    def selftest(self, trigger: str) -> None:
        if not settings.MOYSKLAD_TOKEN:
            return
        self.stdout.write(f"[{timezone.now():%H:%M:%S}] MoySklad sinovi boshlandi ({trigger})…")
        self.stdout.flush()
        try:
            check = selftest.run_selftest(trigger)
        except selftest.SelfTestBusy:
            return
        if check.ok:
            self.stdout.write(self.style.SUCCESS(
                f"  ✓ Sinov o'tdi ({len(check.steps)} bosqich)"))
        else:
            self.stderr.write(self.style.ERROR("  ✗ SINOV O'TMADI:"))
            for step in check.failed_steps:
                self.stderr.write(self.style.ERROR(f"     - {step['name']}: {step['detail']}"))
        self.stdout.flush()
        self.stderr.flush()

    # ------------------------------------------------------------------ bir marta

    def run_once(self, o, quiet_when_empty=False):
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
            if not quiet_when_empty:
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
        self.stdout.flush()

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
