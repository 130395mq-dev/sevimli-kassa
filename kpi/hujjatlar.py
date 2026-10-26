"""
MoySklad hujjatlari, xodimlari va audit hodisalari — FAQAT O'QISH.

Ishlatiladigan rasmiy endpointlar (dev.moysklad.ru, JSON API 1.2):
  * entity/{supply,enter,loss,salesreturn,purchasereturn,move} — ro'yxat,
    `filter=updated>=…` (sahifalab);
  * entity/{tur}/{id}/positions — hujjat qatorlari;
  * entity/{tur}/{id}/audit — shu hujjat hodisalari («Получить События по
    Сущности»): haqiqiy yaratuvchi `create` hodisasidan (uid + source)
    olinadi. Hujjat egasi (`owner`) yaratuvchi deb TAXMIN QILINMAYDI;
  * audit?filter=moment>=…;eventType=… — kontekstlar; audit/{id}/events —
    hodisalar. Shundan o'chirilgan hujjatlar va «Распечатан товар»
    (eventType=print) olinadi;
  * entity/employee — xodimlar (login = `uid`).

Takroriy sinxronlash dublikat yaratmaydi: hamma narsa MoySklad ID bo'yicha
yangilanadi. Hujjatni tahrirlash — yangi ish emas: hujjat bir marta
(`created` kuni) sanaladi. Kursor faqat ro'yxat to'liq o'qilgandan keyin
suriladi — uzilishdan keyin o'sha joydan qayta o'qiladi.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from catalog.models import SyncState

from . import ms
from .minus import tracked_warehouses
from .models import ChopEtish, Hujjat, HujjatQator, MsXodim

logger = logging.getLogger(__name__)

KINDS = [k for k, _ in Hujjat.KINDS]
BACKFILL_DAYS = 14          # birinchi ishga tushishda shuncha kun orqaga
OVERLAP = timedelta(minutes=10)
AUDIT_PAGE = 100            # audit uchun MoySklad limiti 1–100
DETAIL_BATCH = 200          # bir ishga tushishda ko'pi bilan shuncha hujjat tafsiloti


class Budget:
    """Vaqt chegarasi: fon ishi cron'ni uzoq ushlab turmasin."""

    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds

    def left(self) -> bool:
        return time.monotonic() < self.end


def _state(entity: str) -> SyncState:
    st, _ = SyncState.objects.get_or_create(entity=entity)
    return st


# ------------------------------------------------------------------ xodimlar

def sync_employees(client) -> int:
    n = 0
    for row in client.iter_list("entity/employee"):
        mid = row.get("id")
        if not mid:
            continue
        MsXodim.objects.update_or_create(ms_id=mid, defaults={
            "uid": row.get("uid") or "",
            "name": row.get("fullName") or row.get("name") or row.get("shortFio") or "",
            "position": row.get("position") or "",
            "archived": bool(row.get("archived")),
        })
        n += 1
    return n


# ------------------------------------------------------------------ hujjatlar

def _doc_fields(kind: str, row: dict) -> dict:
    store = ms.ms_id(row.get("store"))
    target = None
    if kind == Hujjat.MOVE:
        store = ms.ms_id(row.get("sourceStore"))
        target = ms.ms_id(row.get("targetStore"))
    return {
        "kind": kind,
        "number": row.get("name") or "",
        "moment": ms.parse(row.get("moment")),
        "created": ms.parse(row.get("created")),
        "updated": ms.parse(row.get("updated")),
        "applicable": bool(row.get("applicable", True)),
        "store_ms_id": store,
        "target_ms_id": target,
        "sum": int(row.get("sum") or 0),
        "owner_ms_id": ms.ms_id(row.get("owner")),
        "deleted_at": None,
    }


def sync_list(client, kind: str, stores: set[str], now=None) -> int:
    """Bitta tur hujjatlari ro'yxati (o'zgarganlari). Qatorlar keyin olinadi."""
    now = now or timezone.now()
    st = _state(f"kpi:doc:{kind}")
    since = (st.cursor - OVERLAP) if st.cursor else now - timedelta(days=BACKFILL_DAYS)
    n = 0
    for row in client.iter_list(f"entity/{kind}", filter=f"updated>={ms.fmt(since)}"):
        fields = _doc_fields(kind, row)
        if str(fields["store_ms_id"]) not in stores and str(fields["target_ms_id"]) not in stores:
            continue
        Hujjat.objects.update_or_create(ms_id=row["id"], defaults=fields)
        n += 1
    # Ro'yxat oxirigacha o'qildi — endi kursorni suramiz (uzilsa — surilmaydi)
    st.cursor = now
    st.last_success_at = timezone.now()
    st.last_error = ""
    st.rows_synced = n
    st.save()
    return n


def _creator(client, doc: Hujjat) -> None:
    create = None
    for ev in client.iter_list(f"entity/{doc.kind}/{doc.ms_id}/audit", page_size=AUDIT_PAGE):
        if ev.get("eventType") == "create":
            create = ev
            break
    doc.creator_checked = True
    # Audit saqlanish muddatidan oldin yaratilgan bo'lsa — yaratuvchi noma'lum
    doc.creator_uid = (create or {}).get("uid") or ""
    doc.creator_source = (create or {}).get("source") or ""


def _lines(client, doc: Hujjat) -> list[HujjatQator]:
    out = []
    for p in client.iter_list(f"entity/{doc.kind}/{doc.ms_id}/positions"):
        pid = ms.ms_id(p.get("assortment"))
        if not pid:
            continue
        out.append(HujjatQator(
            hujjat=doc, product_ms_id=pid,
            quantity=Decimal(str(p.get("quantity") or 0)),
            price=int(p.get("price") or 0),
            reason=(p.get("reason") or "")[:255],
        ))
    return out


class AuditForbidden(Exception):
    """Token egasida auditni ko'rish huquqi yo'q — yaratuvchi aniqlanmaydi."""


def sync_details(client, budget: Budget) -> int:
    """Yangi/o'zgargan hujjatlarning qatorlari, keyin yaratuvchisi (auditdan).

    Ikki alohida bosqich: auditga ruxsat bo'lmasa ham qatorlar (hajm)
    olinaveradi; yaratuvchi «aniqlanmoqda» bo'lib qoladi va xato holatda
    ko'rsatiladi."""
    from moysklad.client import MoySkladError

    n = 0
    lines_todo = (Hujjat.objects.filter(deleted_at__isnull=True)
                  .filter(Q(lines_for__isnull=True) | ~Q(lines_for=F("updated")))
                  .order_by("-created")[:DETAIL_BATCH])
    for doc in lines_todo:
        if not budget.left():
            return n
        lines = _lines(client, doc)
        with transaction.atomic():
            doc.qatorlar.all().delete()
            HujjatQator.objects.bulk_create(lines)
            doc.positions_count = len(lines)
            doc.products_count = len({ln.product_ms_id for ln in lines})
            doc.quantity = sum((ln.quantity for ln in lines), Decimal(0))
            doc.lines_for = doc.updated or doc.created or timezone.now()
            doc.save(update_fields=["positions_count", "products_count", "quantity", "lines_for"])
        n += 1
    for doc in (Hujjat.objects.filter(deleted_at__isnull=True, creator_checked=False)
                .order_by("-created")[:DETAIL_BATCH]):
        if not budget.left():
            return n
        try:
            _creator(client, doc)
        except MoySkladError as exc:
            if exc.status in (401, 403):
                raise AuditForbidden("MoySklad auditini o'qishga ruxsat yo'q — "
                                     "hujjat yaratuvchisi aniqlanmaydi") from exc
            raise
        doc.save(update_fields=["creator_checked", "creator_uid", "creator_source"])
        n += 1
    return n


WINDOW = timedelta(hours=12)   # audit oynasi: har oyna tugagach kursor suriladi


def _contexts(client, event_type: str, since, until=None):
    flt = f"moment>={ms.fmt(since)}"
    if until is not None:
        flt += f";moment<={ms.fmt(until)}"
    return client.iter_list("audit", page_size=AUDIT_PAGE, filter=f"{flt};eventType={event_type}")


def _windows(st: SyncState, now):
    """Kursordan hozirgacha 12 soatlik oynalar. Har oyna to'liq o'qilgach kursor
    suriladi — katta orqaga to'ldirish vaqt chegarasida bo'linib, keyingi
    safar to'xtagan joyidan davom etadi (boshidan emas)."""
    start = (st.cursor - OVERLAP) if st.cursor else now - timedelta(days=BACKFILL_DAYS)
    while start < now:
        end = min(start + WINDOW, now)
        yield start, end
        start = end


def _advance(st: SyncState, cursor) -> None:
    st.cursor = cursor
    st.last_success_at = timezone.now()
    st.last_error = ""
    st.save()


def sync_deleted(client, budget: Budget, now=None) -> int:
    """O'chirilgan / savatga tashlangan / qaytarilgan hujjatlar (auditdan)."""
    now = now or timezone.now()
    st = _state("kpi:audit:delete")
    n = 0
    for a, b in _windows(st, now):
        for et in ("delete", "puttorecyclebin", "restorefromrecyclebin"):
            for ctx in _contexts(client, et, a, b):
                if ctx.get("entityType") and ctx["entityType"] not in KINDS:
                    continue
                for ev in client.iter_list(f"audit/{ctx['id']}/events", page_size=AUDIT_PAGE):
                    if ev.get("entityType") not in KINDS:
                        continue
                    mid = ms.ms_id(ev.get("entity"))
                    if not mid:
                        continue
                    when = ms.parse(ev.get("moment")) or now
                    n += Hujjat.objects.filter(ms_id=mid).update(
                        deleted_at=None if et == "restorefromrecyclebin" else when,
                    )
        _advance(st, b)          # oyna to'liq o'qildi
        if not budget.left():
            break
    return n


def sync_prints(client, budget: Budget, now=None) -> int:
    """«Распечатан товар» — audit `print` hodisalari (kim, qachon, qaysi tovar)."""
    now = now or timezone.now()
    st = _state("kpi:audit:print")
    n = 0
    for a, b in _windows(st, now):
        for ctx in _contexts(client, "print", a, b):
            for ev in client.iter_list(f"audit/{ctx['id']}/events", page_size=AUDIT_PAGE):
                et = ev.get("entityType") or ""
                if et not in ("product", "variant", "bundle", "service"):
                    continue
                mid = ms.ms_id(ev.get("entity"))
                when = ms.parse(ev.get("moment") or ctx.get("moment"))
                if not mid or not when:
                    continue
                _, created = ChopEtish.objects.get_or_create(
                    audit_id=ctx["id"], product_ms_id=mid,
                    defaults={
                        "product_name": (ev.get("name") or "")[:512], "entity_type": et,
                        "uid": ev.get("uid") or ctx.get("uid") or "",
                        "source": ev.get("source") or ctx.get("source") or "",
                        "moment": when,
                    },
                )
                n += int(created)
        _advance(st, b)
        if not budget.left():
            break
    return n


def sync_documents(client, budget: Budget, now=None) -> dict:
    stores = set(tracked_warehouses())
    if not stores:
        return {}
    out = {}
    for kind in KINDS:
        if not budget.left():
            break
        out[kind] = sync_list(client, kind, stores, now)
    if budget.left():
        out["tafsilot"] = sync_details(client, budget)
    return out
