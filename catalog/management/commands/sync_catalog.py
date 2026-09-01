"""
Katalogni MoySklad'dan sinxronlaydi.

Ishlatilishi:

    python manage.py sync_catalog --check        # faqat ulanishni tekshiradi
    python manage.py sync_catalog --full         # hammasini to'liq tortadi
    python manage.py sync_catalog                # delta (o'zgarganini)
    python manage.py sync_catalog --only stock   # faqat qoldiqni

Railway'da cron sifatida:
    har 3 daqiqada:  python manage.py sync_catalog --only stock
    har 10 daqiqada: python manage.py sync_catalog --only products
    har 15 daqiqada: python manage.py sync_catalog --only customers
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from catalog.sync import CatalogSync
from moysklad.client import MoySkladClient, MoySkladError


class Command(BaseCommand):
    help = "MoySklad katalogini lokal bazaga sinxronlaydi"

    def add_arguments(self, parser):
        parser.add_argument(
            "--full",
            action="store_true",
            help="Delta emas, hammasini to'liq tortadi",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="Faqat ulanishni tekshiradi, hech narsa yozmaydi",
        )
        parser.add_argument(
            "--only",
            choices=["folders", "products", "customers", "stock", "retail_stores"],
            help="Faqat bitta turni sinxronlaydi",
        )

    def handle(self, *args, **options):
        token = getattr(settings, "MOYSKLAD_TOKEN", "")
        if not token:
            raise CommandError(
                "MOYSKLAD_TOKEN topilmadi. Railway'ning Variables bo'limiga qo'shing."
            )

        client = MoySkladClient(token=token)

        # --check: ulanishni tekshiramiz va limitni ko'rsatamiz
        if options["check"]:
            self._check(client)
            return

        sync = CatalogSync(client)
        started = time.monotonic()

        try:
            if options["only"]:
                result = self._sync_one(sync, options["only"], options["full"])
            else:
                result = sync.sync_all(full=options["full"])
        except MoySkladError as exc:
            self._error(f"MoySklad xatosi: {exc}")
            if exc.is_auth_error:
                self._error(
                    "Token ishlamayapti. Ehtimol kimdir yangi token yaratgan — "
                    "MoySklad eski tokenlarni bekor qiladi."
                )
            raise CommandError("Sinxronizatsiya to'xtadi")

        elapsed = time.monotonic() - started
        self._ok(f"Tayyor — {elapsed:.1f} soniya")
        for name, count in result.items():
            self.stdout.write(f"  {name:16} {count:>7}")

        self.stdout.write("")
        self._limits(client)

    # ------------------------------------------------------------------ ichki

    def _sync_one(self, sync: CatalogSync, kind: str, full: bool) -> dict:
        method = {
            "folders": lambda: sync.sync_folders(full),
            "products": lambda: sync.sync_products(full),
            "customers": lambda: sync.sync_customers(full),
            "retail_stores": lambda: sync.sync_retail_stores(full),
            "stock": sync.sync_stock,
        }[kind]
        return {kind: method()}

    def _check(self, client: MoySkladClient) -> None:
        try:
            info = client.check_connection()
        except MoySkladError as exc:
            self._error(f"Ulanmadi: {exc}")
            if exc.is_auth_error:
                self._error("Token noto'g'ri yoki bekor qilingan.")
            raise CommandError("Ulanish tekshiruvi muvaffaqiyatsiz")

        self._ok("MoySklad bilan ulanish bor")
        self.stdout.write(f"  Foydalanuvchi : {info['employee']}")
        self.stdout.write(f"  Administrator : {'ha' if info['is_admin'] else 'YO`Q'}")

        if not info["is_admin"]:
            self._warn(
                "Bu foydalanuvchi administrator emas. Webhook yaratish uchun "
                "administrator huquqi kerak bo'ladi."
            )
        self.stdout.write("")
        self._limits(client)

    def _limits(self, client: MoySkladClient) -> None:
        state = client.state
        if state.limit is None:
            self.stdout.write("Limit haqida ma'lumot yo'q")
            return

        interval = (state.interval_ms or 0) / 1000
        self.stdout.write(
            f"Limit: {state.limit} so'rov / {interval:.0f} soniya "
            f"(qolgani: {state.remaining})"
        )
        # 45 — «решение» tokeni. Undan past bo'lsa foydalanuvchi tokeni.
        if state.limit and state.limit < 45:
            self._warn(
                f"Limit {state.limit} — bu foydalanuvchi tokeni. "
                "«Приватное решение» tokeni bilan 45 bo'ladi."
            )

    def _ok(self, text: str) -> None:
        self.stdout.write(self.style.SUCCESS(f"✓ {text}"))

    def _warn(self, text: str) -> None:
        self.stdout.write(self.style.WARNING(f"! {text}"))

    def _error(self, text: str) -> None:
        self.stdout.write(self.style.ERROR(f"✗ {text}"))
