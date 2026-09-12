"""Navbatdagi cheklarni MoySklad'ga yuborish — bitta joyda.

Ilgari bu mantiq `sync_sales` buyrug'ining ichida edi. Endi u shu yerda,
chunki uni ikki joy ishlatadi:

  * `sales-sync` xizmati (`sync_sales --loop`) — asosiy yo'l, 20 soniyada bir;
  * panel serveri (`sales/healer.py`) — ZAXIRA yo'l: `sales-sync` jim qolsa
    (o'lib qolsa, Railway'da to'xtasa) cheklar baribir MoySklad'ga boradi.

Qayta urinish oralig'i o'sib boradi (1, 2, 4, 8… daqiqa): MoySklad bir xil
xatoli so'rov takrorlanaversa API'ni o'chirib qo'yadi. `SYNC_MAX_ATTEMPTS`
dan keyin chek `stuck` bo'ladi — sinov o'tganda (`selftest`) yoki paneldagi
«Qayta yuborish» bilan navbatga qaytadi.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from moysklad.client import MoySkladClient

from .models import Sale
from .writer import SaleWriter, SumMismatch, WriteError

logger = logging.getLogger(__name__)

BACKOFF_MINUTES = [1, 2, 4, 8, 15, 30, 60]


def due_filter(now):
    """Vaqti kelgan cheklar: yangi, yoki kutish muddati o'tgan."""
    return Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)


def due_queue(now=None, limit: int = 100) -> list[Sale]:
    now = now or timezone.now()
    return list(
        Sale.objects.filter(sync_status__in=[Sale.NEW, Sale.FAILED])
        .filter(due_filter(now))
        .select_related("shift__register__store", "customer")
        .order_by("created_at")[:limit]
    )


def due_exists(now=None) -> bool:
    now = now or timezone.now()
    return (
        Sale.objects.filter(sync_status__in=[Sale.NEW, Sale.FAILED])
        .filter(due_filter(now)).exists()
    )


def mark_sent(sale: Sale) -> None:
    sale.sync_status = Sale.SENT
    sale.synced_at = timezone.now()
    sale.sync_error = ""
    sale.next_attempt_at = None
    sale.save(update_fields=["sync_status", "synced_at", "sync_error", "next_attempt_at"])


def mark_failed(sale: Sale, error: str) -> None:
    sale.sync_attempts += 1
    sale.sync_error = error[:2000]
    if sale.sync_attempts >= settings.SYNC_MAX_ATTEMPTS:
        sale.sync_status = Sale.STUCK
        sale.next_attempt_at = None
    else:
        sale.sync_status = Sale.FAILED
        idx = min(sale.sync_attempts - 1, len(BACKOFF_MINUTES) - 1)
        sale.next_attempt_at = timezone.now() + timedelta(minutes=BACKOFF_MINUTES[idx])
    sale.save(update_fields=["sync_attempts", "sync_error", "sync_status", "next_attempt_at"])


def mark_stuck(sale: Sale, error: str) -> None:
    sale.sync_attempts += 1
    sale.sync_error = error[:2000]
    sale.sync_status = Sale.STUCK
    sale.next_attempt_at = None
    sale.save(update_fields=["sync_attempts", "sync_error", "sync_status", "next_attempt_at"])


def send_one(writer: SaleWriter, sale: Sale) -> tuple[str, str]:
    """Bitta chekni yozadi. Qaytaradi: ("sent"|"failed"|"stuck", xato matni)."""
    try:
        writer.send(sale)
    except SumMismatch as e:
        # Hujjat yozildi, lekin raqam mos kelmadi. Qayta yuborish yordam
        # bermaydi — odam ko'rishi kerak.
        mark_stuck(sale, str(e))
        return "stuck", str(e)
    except WriteError as e:
        mark_failed(sale, str(e))
        return "failed", str(e)
    mark_sent(sale)
    return "sent", ""


def send_due(limit: int = 100, writer: SaleWriter | None = None, now=None) -> dict:
    """Navbatni yuboradi. Natija: {"sent", "failed", "stuck", "errors": [(sale, matn)]}."""
    result = {"sent": 0, "failed": 0, "stuck": 0, "errors": []}
    queue = due_queue(now, limit)
    if not queue:
        return result
    if writer is None:
        if not settings.MOYSKLAD_TOKEN:
            return result
        writer = SaleWriter(MoySkladClient(token=settings.MOYSKLAD_TOKEN))
    for sale in queue:
        status, error = send_one(writer, sale)
        result[status] += 1
        if error:
            result["errors"].append((sale, error))
    return result


def requeue_stuck() -> int:
    """Tiqilgan cheklarni navbatga qaytaradi. Qaytaradi: nechta."""
    return Sale.objects.filter(sync_status=Sale.STUCK).update(
        sync_status=Sale.NEW, sync_attempts=0, next_attempt_at=None,
    )
