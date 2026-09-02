"""
MoySklad → lokal baza sinxronizatsiyasi.

Darajalangan yondashuv: tez o'zgaradigan narsa tez-tez, sekin o'zgaradigan
narsa kamdan-kam so'raladi. Bu MoySklad limitini tejaydi.

    Qoldiq          — 2-3 daqiqada
    Narx, tovar     — 10 daqiqada
    Mijoz           — 15 daqiqada
    Papka, nuqta    — 60 daqiqada

Delta sync `updated` maydoni bo'yicha ishlaydi: oxirgi muvaffaqiyatli
sinxronizatsiya vaqtidan keyin o'zgargan yozuvlar so'raladi.

DIQQAT: bu modul MoySklad'ga HECH NARSA YOZMAYDI. Faqat o'qiydi.
Yozish moduli alohida bo'ladi va u hozircha yozilmagan — MoySklad javobi
kutilmoqda (Отгрузка yo'lidanmi yoki Розничная продажа yo'lidanmi).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.db import transaction
from django.utils import timezone as dj_timezone

from moysklad.client import MoySkladClient, MoySkladError

from .models import (
    Barcode,
    Customer,
    Product,
    ProductFolder,
    RetailStore,
    Stock,
    SyncState,
)

logger = logging.getLogger(__name__)

# MoySklad `updated` ni "YYYY-MM-DD HH:MM:SS.mmm" formatida kutadi.
MS_TIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

# Delta so'rovda biroz orqaga qaytamiz — soat farqi va bir vaqtda
# o'zgarishlar tufayli yozuv o'tkazib yuborilmasligi uchun.
DELTA_OVERLAP = timedelta(minutes=5)


def _parse_ms_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        naive = datetime.strptime(value, MS_TIME_FORMAT)
    except ValueError:
        try:
            naive = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return naive.replace(tzinfo=timezone.utc)


def _format_ms_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(MS_TIME_FORMAT)[:-3]


def _ms_id(meta: dict | None) -> str | None:
    """Meta obyektidan UUID ni ajratib oladi."""
    if not meta:
        return None
    href = (meta.get("meta") or meta).get("href", "")
    if not href:
        return None
    # `report/stock/bystore` da href ba'zan `?expand=...` bilan keladi —
    # so'rov qismini olib tashlaymiz, aks holda UUID buziladi.
    tail = href.rstrip("/").split("/")[-1]
    return tail.split("?")[0]


class CatalogSync:
    """MoySklad katalogini lokal bazaga ko'chiradi."""

    def __init__(self, client: MoySkladClient):
        self.client = client

    # ------------------------------------------------------------ yordamchi

    def _state(self, entity: str) -> SyncState:
        state, _ = SyncState.objects.get_or_create(entity=entity)
        return state

    def _delta_filter(self, state: SyncState, full: bool) -> dict:
        """Delta uchun filtr qaytaradi. To'liq sync bo'lsa — bo'sh."""
        if full or not state.cursor:
            return {}
        since = state.cursor - DELTA_OVERLAP
        return {"filter": f"updated>={_format_ms_datetime(since)}"}

    def _finish(self, state: SyncState, count: int, started: datetime) -> None:
        state.rows_synced = count
        state.last_run_at = started
        state.last_success_at = dj_timezone.now()
        state.cursor = started
        state.last_error = ""
        state.save()

    def _fail(self, state: SyncState, error: Exception, started: datetime) -> None:
        state.last_run_at = started
        state.last_error = str(error)[:2000]
        state.save()

    def _run(self, entity: str, full: bool, worker) -> int:
        """Umumiy o'rov: holatni yangilaydi, xatoni ushlaydi."""
        state = self._state(entity)
        started = dj_timezone.now()
        try:
            count = worker(state, full)
        except MoySkladError as exc:
            logger.exception("«%s» sinxronizatsiyasi muvaffaqiyatsiz", entity)
            self._fail(state, exc, started)
            raise
        self._finish(state, count, started)
        logger.info("«%s»: %s ta yozuv sinxronlandi", entity, count)
        return count

    # ------------------------------------------------------------ papkalar

    def sync_folders(self, full: bool = False) -> int:
        return self._run("productfolder", full, self._sync_folders)

    def _sync_folders(self, state: SyncState, full: bool) -> int:
        params = self._delta_filter(state, full)
        count = 0
        # Papkalarni ikki bosqichda yozamiz: avval o'zini, keyin parent
        # bog'lanishini — chunki parent hali yaratilmagan bo'lishi mumkin.
        parents: dict[str, str] = {}

        for row in self.client.iter_list("entity/productfolder", **params):
            ms_id = row["id"]
            ProductFolder.objects.update_or_create(
                ms_id=ms_id,
                defaults={
                    "name": row.get("name", ""),
                    "path_name": row.get("pathName", "") or "",
                    "archived": row.get("archived", False),
                    "updated": _parse_ms_datetime(row.get("updated")),
                },
            )
            parent_id = _ms_id(row.get("productFolder"))
            if parent_id:
                parents[ms_id] = parent_id
            count += 1

        for child_id, parent_id in parents.items():
            ProductFolder.objects.filter(ms_id=child_id).update(
                parent=ProductFolder.objects.filter(ms_id=parent_id).first()
            )
        return count

    # ------------------------------------------------------------- tovarlar

    def sync_products(self, full: bool = False) -> int:
        return self._run("assortment", full, self._sync_products)

    def _sync_products(self, state: SyncState, full: bool) -> int:
        """
        `entity/assortment` — tovar, modifikatsiya, xizmat va komplektni
        bitta so'rovda beradi. Alohida-alohida so'rashdan tejamliroq.
        """
        params = self._delta_filter(state, full)
        count = 0

        for row in self.client.iter_list("entity/assortment", **params):
            kind = row.get("meta", {}).get("type", Product.KIND_PRODUCT)
            if kind not in dict(Product.KIND_CHOICES):
                continue

            folder = None
            folder_id = _ms_id(row.get("productFolder"))
            if folder_id:
                folder = ProductFolder.objects.filter(ms_id=folder_id).first()

            uom_name = ((row.get("uom") or {}).get("name") or "").strip()
            is_weight = uom_name.lower() in {"кг", "kg", "г", "gramm", "литр", "l"}

            code = row.get("code", "") or ""
            # Tarozi PLU — vaznli tovar kodining raqamli qismi. Tarozi
            # yorlig'i (29 + PLU + vazn) shu PLU bilan tovarni topadi;
            # MoySklad'da vaznli tovar kodi = tarozidagi PLU raqami.
            plu_val = None
            if is_weight:
                digits = "".join(ch for ch in code if ch.isdigit())
                if digits and len(digits) <= 9:
                    plu_val = int(digits)

            product, _ = Product.objects.update_or_create(
                ms_id=row["id"],
                defaults={
                    "kind": kind,
                    "name": row.get("name", ""),
                    "code": code,
                    "plu": plu_val,
                    "article": row.get("article", "") or "",
                    "folder": folder,
                    "sale_price": self._first_sale_price(row),
                    "uom_name": uom_name,
                    "is_weight": is_weight,
                    "vat": row.get("vat"),
                    "tracked": bool(row.get("trackingType")),
                    "archived": row.get("archived", False),
                    "updated": _parse_ms_datetime(row.get("updated")),
                },
            )
            self._sync_barcodes(product, row.get("barcodes") or [])
            count += 1

        return count

    @staticmethod
    def _first_sale_price(row: dict) -> int:
        """Birinchi sotuv narxini tiyinlarda qaytaradi."""
        prices = row.get("salePrices") or []
        if not prices:
            return 0
        value = prices[0].get("value")
        return int(value) if value is not None else 0

    @staticmethod
    def _sync_barcodes(product: Product, barcodes: list[dict]) -> None:
        """Shtrix-kodlarni qayta yozadi — eskilarini o'chirib, yangisini qo'yadi."""
        values = []
        for item in barcodes:
            for kind, value in item.items():
                if value:
                    values.append((str(value), kind))

        existing = set(product.barcodes.values_list("value", flat=True))
        incoming = {v for v, _ in values}

        if existing == incoming:
            return

        product.barcodes.all().delete()
        Barcode.objects.bulk_create(
            [Barcode(product=product, value=value, kind=kind) for value, kind in values],
            ignore_conflicts=True,
        )

    # ------------------------------------------------------------- qoldiqlar

    def sync_stock(self) -> int:
        return self._run("stock", True, self._sync_stock)

    def _sync_stock(self, state: SyncState, full: bool) -> int:
        """
        Qoldiq — `report/stock/bystore`. Bu hisobot, sushchnost emas,
        shuning uchun unga webhook yo'q va uni doim so'rab turamiz.
        """
        count = 0
        for row in self.client.iter_list("report/stock/bystore"):
            product_id = _ms_id(row.get("meta"))
            if not product_id:
                continue
            product = Product.objects.filter(ms_id=product_id).first()
            if not product:
                continue

            for store_row in row.get("stockByStore") or []:
                store_id = _ms_id(store_row.get("meta"))
                if not store_id:
                    continue
                Stock.objects.update_or_create(
                    product=product,
                    store_ms_id=store_id,
                    defaults={"quantity": Decimal(str(store_row.get("stock") or 0))},
                )
                count += 1
        return count

    # -------------------------------------------------------------- mijozlar

    def sync_customers(self, full: bool = False) -> int:
        return self._run("counterparty", full, self._sync_customers)

    def _sync_customers(self, state: SyncState, full: bool) -> int:
        params = self._delta_filter(state, full)
        count = 0

        for row in self.client.iter_list("entity/counterparty", **params):
            discounts = row.get("discounts") or []
            Customer.objects.update_or_create(
                ms_id=row["id"],
                defaults={
                    "name": row.get("name", ""),
                    "phone": (row.get("phone") or "")[:64],
                    "discount_card": (row.get("discountCardNumber") or "")[:128],
                    "sales_amount": int(row.get("salesAmount") or 0),
                    "bonus_points": int(row.get("bonusPoints") or 0),
                    "accumulation_discount": self._discount_value(
                        discounts, "accumulationDiscount"
                    ),
                    "personal_discount": self._discount_value(
                        discounts, "personalDiscount"
                    ),
                    "archived": row.get("archived", False),
                    "updated": _parse_ms_datetime(row.get("updated")),
                },
            )
            count += 1
        return count

    @staticmethod
    def _discount_value(discounts: list[dict], key: str) -> Decimal:
        for item in discounts:
            value = item.get(key)
            if value is not None:
                try:
                    return Decimal(str(value))
                except (TypeError, ValueError):
                    continue
        return Decimal("0")

    # --------------------------------------------------------- savdo nuqtalari

    def sync_retail_stores(self, full: bool = False) -> int:
        return self._run("retailstore", full, self._sync_retail_stores)

    def _sync_retail_stores(self, state: SyncState, full: bool) -> int:
        count = 0
        for row in self.client.iter_list("entity/retailstore"):
            RetailStore.objects.update_or_create(
                ms_id=row["id"],
                defaults={
                    "name": row.get("name", ""),
                    "store_ms_id": _ms_id(row.get("store")),
                    "organization_ms_id": _ms_id(row.get("organization")),
                    "active": row.get("active", True),
                    "updated": _parse_ms_datetime(row.get("updated")),
                },
            )
            count += 1
        return count

    # ------------------------------------------- o'chirilgan tovarlar (reconcile)

    def reconcile_deleted(self) -> int:
        return self._run("reconcile", True, self._reconcile_deleted)

    def _reconcile_deleted(self, state: SyncState, full: bool) -> int:
        """
        MoySklad'dan BUTUNLAY o'chirilgan (yoki arxivlangan) tovarlarni topadi.

        Delta sync `updated` bo'yicha ishlaydi — lekin o'chirilgan tovar
        ro'yxatda umuman kelmaydi, u haqida hech qanday signal yo'q.
        Shuning uchun vaqti-vaqti bilan MoySklad'dagi jonli ID'lar ro'yxatini
        olib, bizda bor-u u yerda yo'q tovarlarni `archived=True` qilamiz.
        Kassa keyingi delta'da ularni o'chirib tashlaydi.

        Xavfsizlik: MoySklad kutilmaganda juda oz qaytarsa (API nosozligi,
        yarim javob) — hech narsa o'chirmaymiz. Aks holda bir xato bilan
        butun katalog «o'chirilgan» bo'lib qolardi.
        """
        live: set[str] = set()
        for row in self.client.iter_list("entity/assortment"):
            ms_id = row.get("id")
            if ms_id:
                live.add(str(ms_id).lower())

        local = {
            str(u).lower()
            for u in Product.objects.filter(archived=False).values_list("ms_id", flat=True)
        }
        if not local:
            return 0

        # Himoya: MoySklad lokaldagining yarmidan kamini qaytarsa — shubhali.
        if len(live) < len(local) // 2:
            logger.warning(
                "Reconcile to'xtatildi: MoySklad %s ta, lokal %s ta — juda katta farq",
                len(live), len(local),
            )
            return 0

        missing = local - live
        if not missing:
            return 0

        now = dj_timezone.now()
        count = 0
        missing_list = sorted(missing)
        # Katta IN-ro'yxatni bo'lib yuboramiz
        for i in range(0, len(missing_list), 500):
            chunk = missing_list[i:i + 500]
            # queryset.update() auto_now'ni ishlatmaydi — synced_at ni
            # qo'lda qo'yamiz, aks holda kassa delta'si buni ko'rmaydi.
            count += Product.objects.filter(ms_id__in=chunk).update(
                archived=True, synced_at=now
            )
        logger.info("Reconcile: %s ta tovar MoySklad'da yo'q — arxivlandi", count)
        return count

    # ------------------------------------------------------------------ hammasi

    @transaction.atomic
    def sync_all(self, full: bool = False) -> dict[str, int]:
        """Birinchi ishga tushirish uchun — hammasini ketma-ket sinxronlaydi."""
        return {
            "folders": self.sync_folders(full),
            "products": self.sync_products(full),
            "customers": self.sync_customers(full),
            "retail_stores": self.sync_retail_stores(full),
            "stock": self.sync_stock(),
        }
