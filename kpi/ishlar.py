"""
KPI fon ishlari — qoldiq sinxronizatsiyasidan keyin chaqiriladi
(`CatalogSync.sync_stock`). Alohida cron kerak emas.

Xavfsizlik:
  * hech qachon xato tashlamaydi — qoldiq sinxronizatsiyasi va kassalarga
    ta'sir qilmaydi;
  * rahbar hech bo'lmasa bitta tovarovedni do'konga biriktirmaguncha
    MoySklad'ga BIRORTA ham so'rov yubormaydi;
  * o'z MoySklad klienti (kam qayta urinish) va vaqt chegarasi bor;
  * bir vaqtda faqat bitta jarayon ishlaydi (advisory qulf);
  * MoySklad'ga faqat GET so'rovlar.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from catalog.models import SyncState
from config.work_locks import work_lock

logger = logging.getLogger(__name__)

BUDGET_SECONDS = 60
STEPS = [
    # (nomi, necha daqiqada bir)
    ("kpi:minus", 4),
    ("kpi:xodimlar", 360),
    # Audit (chop etish, o'chirilganlar) hujjatlardan OLDIN: hujjatlarni orqaga
    # to'ldirish vaqt chegarasini to'liq yeb qo'ymasin
    ("kpi:chop", 15),
    ("kpi:ochirilgan", 30),
    ("kpi:hujjatlar", 15),
    ("kpi:sabab", 15),
    ("kpi:hisob", 15),
]


def _due(name: str, minutes: int, now) -> bool:
    st = SyncState.objects.filter(entity=name).first()
    return not st or not st.last_success_at or now - st.last_success_at >= timedelta(minutes=minutes)


def _mark(name: str, now, error: str = "", rows: int = 0) -> None:
    st, _ = SyncState.objects.get_or_create(entity=name)
    st.last_run_at = now
    if error:
        st.last_error = error[:2000]
    else:
        st.last_success_at = timezone.now()
        st.last_error = ""
        st.rows_synced = rows
    st.save()


def enabled() -> bool:
    from .models import Biriktirish

    return Biriktirish.objects.exists()


def make_client():
    from moysklad.client import MoySkladClient

    token = getattr(settings, "MOYSKLAD_TOKEN", "")
    if not token:
        return None
    return MoySkladClient(token=token, network_retries=1, max_retries=3, user_agent="SevimliKassa-KPI/0.1")


def run(client=None, now=None, budget_seconds: float = BUDGET_SECONDS) -> dict:
    from . import hisob, hujjatlar, minus, sabab

    now = now or timezone.now()
    if not enabled():
        return {}
    done: dict = {}
    with work_lock("kpi:run") as acquired:
        if not acquired:
            return {}
        client = client or make_client()
        if client is None:
            return {}
        budget = hujjatlar.Budget(budget_seconds)
        work = {
            "kpi:minus": lambda: minus.observe(client, now) and 1,
            "kpi:xodimlar": lambda: hujjatlar.sync_employees(client),
            "kpi:hujjatlar": lambda: sum(hujjatlar.sync_documents(client, budget, now).values()),
            "kpi:ochirilgan": lambda: hujjatlar.sync_deleted(client, budget, now),
            "kpi:chop": lambda: hujjatlar.sync_prints(client, budget, now),
            "kpi:sabab": lambda: sabab.refresh(now),
            "kpi:hisob": lambda: hisob.recompute_recent(now),
        }
        for name, minutes in STEPS:
            if not budget.left() and name not in ("kpi:sabab", "kpi:hisob"):
                continue
            if not _due(name, minutes, now):
                continue
            try:
                rows = work[name]() or 0
            except Exception as exc:  # bitta qadam yiqilsa — qolganlari ishlayveradi
                logger.warning("KPI «%s» bajarilmadi: %s: %s", name, type(exc).__name__, exc)
                _mark(name, now, error=f"{type(exc).__name__}: {exc}")
                continue
            _mark(name, now, rows=int(rows))
            done[name] = rows
    return done


def after_stock_sync() -> None:
    """Qoldiq sinxronizatsiyasidan keyin. HECH QACHON xato tashlamaydi."""
    try:
        run()
    except Exception:
        logger.exception("KPI fon ishi bajarilmadi (qoldiq sinxronizatsiyasiga ta'siri yo'q)")
