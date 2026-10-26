"""
Minus sabablarini hujjatlarni solishtirib TAXMIN qilish va minus qanday
yo'qolganini aniqlash.

Hamma «ehtimoliy sabab» — taxmin. U interfeysda taxmin deb yoziladi va
KPI'ga KIRMAYDI. Tasdiqlangan sababni faqat rahbar belgilaydi.

Dastur aniqlay OLMAYDIGAN narsalar (va'da qilinmaydi): hujjatsiz jismoniy
ko'chirish, javondagi haqiqiy qoldiq, kassir boshqa tovarni urganini aniq
isbotlash.
"""

from __future__ import annotations

from datetime import timedelta
from difflib import SequenceMatcher

from django.utils import timezone

from catalog.models import Stock, SyncState, Warehouse

from . import ms
from .models import Hujjat, HujjatQator, MinusIssue

BEFORE = timedelta(days=3)       # minusdan oldin qaraladigan oraliq
NO_SUPPLY_DAYS = 30              # shuncha kunda kirim bo'lmasa — «kiritilmagan bo'lishi mumkin»
MOVE_DAYS = 7
TWIN_RATIO = 0.75                # nom o'xshashligi chegarasi
RESOLVE_MARGIN = timedelta(minutes=10)
REFRESH_LIMIT = 500


def ref(h: Hujjat) -> dict:
    return {
        "id": str(h.ms_id), "kind": h.kind, "kind_name": h.get_kind_display(),
        "number": h.number,
        # Toshkent vaqtida, ko'rsatish uchun
        "created": h.created.astimezone(ms.TASHKENT).strftime("%d.%m.%Y %H:%M") if h.created else "",
        "store": str(h.store_ms_id or ""), "target": str(h.target_ms_id or ""),
        "creator": h.creator_uid, "manual": h.manual,
    }


def _into(h: Hujjat, wh: str) -> bool:
    """Hujjat shu do'konga tovar KIRITADIMI."""
    if h.kind == Hujjat.MOVE:
        return str(h.target_ms_id) == wh
    return h.kind in (Hujjat.SUPPLY, Hujjat.ENTER, Hujjat.SALESRETURN) and str(h.store_ms_id) == wh


def _touches(h: Hujjat, wh: str) -> bool:
    return str(h.store_ms_id) == wh or str(h.target_ms_id) == wh


def _docs(product_ms_id, since) -> list[Hujjat]:
    ids = (HujjatQator.objects.filter(product_ms_id=product_ms_id, hujjat__deleted_at__isnull=True,
                                      hujjat__created__gte=since)
           .values_list("hujjat_id", flat=True))
    return list(Hujjat.objects.filter(pk__in=list(ids)).order_by("created"))


def probable(issue: MinusIssue, now=None) -> tuple[list[dict], list[dict]]:
    """→ (ehtimoliy sabablar, tegishli hujjatlar). Hammasi taxmin."""
    now = now or timezone.now()
    wh, pid, t = str(issue.warehouse_ms_id), issue.product_ms_id, issue.first_seen
    docs = _docs(pid, t - timedelta(days=NO_SUPPLY_DAYS))
    names = dict(Warehouse.objects.values_list("ms_id", "name"))
    out: list[dict] = []

    def add(code, text, found=()):
        out.append({"code": code, "text": text, "docs": [ref(h) for h in found][:5]})

    incoming = [h for h in docs if _into(h, wh) and h.kind in (Hujjat.SUPPLY, Hujjat.MOVE)]
    late = [h for h in incoming if h.created and h.created > t]
    before = [h for h in incoming if h.created and h.created <= t]
    if late:
        add("prixod", "Minus paydo bo'lgandan KEYIN kirim/ko'chirish kiritilgan — kech kiritilgan bo'lishi mumkin", late)
    elif not before and not issue.pre_tracking:
        add("prixod", f"Oxirgi {NO_SUPPLY_DAYS} kunda bu do'konga kirim ham, ko'chirish ham ko'rinmadi — "
                      "prixod kiritilmagan bo'lishi mumkin")

    other = [h for h in docs if h.kind == Hujjat.SUPPLY and str(h.store_ms_id) != wh
             and h.created and t - BEFORE <= h.created <= t + timedelta(days=1)]
    if other:
        where = ", ".join(sorted({names.get(h.store_ms_id, "boshqa ombor") for h in other}))
        add("ombor", f"Shu kunlarda tovar boshqa omborga ({where}) kirim qilingan — "
                     "noto'g'ri omborga kirim bo'lishi mumkin", other)

    moved_in = [h for h in docs if h.kind == Hujjat.MOVE and str(h.target_ms_id) == wh
                and h.created and t - timedelta(days=MOVE_DAYS) <= h.created <= now]
    if not moved_in:
        elsewhere = (Stock.objects.filter(product__ms_id=pid, quantity__gt=0)
                     .exclude(store_ms_id=wh).order_by("-quantity")[:2])
        if elsewhere:
            what = ", ".join(f"{names.get(s.store_ms_id, 'boshqa ombor')}: {s.quantity.normalize():f}"
                             for s in elsewhere)
            add("kochirish", f"Boshqa omborda bor ({what}), bu do'konga ko'chirish ko'rinmadi — "
                             "ko'chirish kiritilmagan bo'lishi mumkin")

    losses = [h for h in docs if h.kind == Hujjat.LOSS and str(h.store_ms_id) == wh
              and h.created and t - BEFORE <= h.created <= t]
    if losses:
        add("spisaniya", "Minusdan oldingi kunlarda shu tovar spisaniya qilingan — "
                         "noto'g'ri spisaniya bo'lishi mumkin", losses)

    twin = _twin(issue)
    if twin:
        add("shtrix", f"O'xshash tovar «{twin[0]}» bu do'konda ortiqcha (+{twin[1]}) — "
                      "kassada boshqa shtrix-kod/tovar bilan sotilgan bo'lishi mumkin")

    related = [ref(h) for h in docs if _touches(h, wh) and h.created and h.created >= t - BEFORE][-15:]
    return out, related


def _twin(issue: MinusIssue):
    p = issue.product
    if not p or not p.folder_id:
        return None
    me = p.name.casefold()
    best = None
    rows = (Stock.objects.filter(store_ms_id=issue.warehouse_ms_id, quantity__gt=0,
                                 product__folder_id=p.folder_id, product__archived=False)
            .exclude(product_id=p.pk).select_related("product")[:300])
    for s in rows:
        r = SequenceMatcher(None, me, s.product.name.casefold()).ratio()
        if r >= TWIN_RATIO and (best is None or r > best[0]):
            best = (r, s.product.name, f"{s.quantity.normalize():f}")
    return best[1:] if best else None


def _docs_synced_until():
    """Hamma tur hujjatlari qaysi vaqtgacha o'qilgan (eng kichigi)."""
    states = dict(SyncState.objects.filter(entity__startswith="kpi:doc:").values_list("entity", "cursor"))
    cursors = [states.get(f"kpi:doc:{k}") for k, _ in Hujjat.KINDS]
    if any(c is None for c in cursors):
        return None
    return min(cursors)


def resolve(issue: MinusIssue) -> bool:
    """Yopilgan minus qanday hujjat bilan yo'qolgan. Hujjatlar hali o'qilmagan
    bo'lsa — kutadi (False)."""
    synced = _docs_synced_until()
    if not issue.closed_at or synced is None or synced < issue.closed_at + RESOLVE_MARGIN:
        return False
    if Hujjat.objects.filter(lines_for__isnull=True, deleted_at__isnull=True,
                             created__lte=issue.closed_at).exists():
        return False  # qatorlari hali olinmagan hujjat bor
    wh = str(issue.warehouse_ms_id)
    start = (issue.closed_prev_scan or issue.last_seen) - RESOLVE_MARGIN
    end = issue.closed_at + RESOLVE_MARGIN
    ids = HujjatQator.objects.filter(product_ms_id=issue.product_ms_id,
                                     hujjat__deleted_at__isnull=True).values_list("hujjat_id", flat=True)
    docs = [h for h in Hujjat.objects.filter(pk__in=list(ids))
            if _into(h, wh) and any(x and start <= x <= end for x in (h.created, h.updated))]
    order = [(Hujjat.SUPPLY, MinusIssue.RES_KIRIM), (Hujjat.MOVE, MinusIssue.RES_KOCHIRISH),
             (Hujjat.SALESRETURN, MinusIssue.RES_QAYTARISH), (Hujjat.ENTER, MinusIssue.RES_OPRIXOD)]
    issue.resolution = MinusIssue.RES_NOMALUM
    for kind, res in order:
        found = [h for h in docs if h.kind == kind]
        if found:
            issue.resolution = res
            issue.resolution_docs = [ref(h) for h in found][:5]
            break
    issue.save(update_fields=["resolution", "resolution_docs"])
    return True


def refresh(now=None) -> int:
    now = now or timezone.now()
    n = 0
    recent = now - timedelta(days=2)
    qs = (MinusIssue.objects.filter(status=MinusIssue.OPEN) | MinusIssue.objects.filter(closed_at__gte=recent))
    for issue in qs.select_related("product").order_by("-first_seen")[:REFRESH_LIMIT]:
        issue.probable, issue.related_docs = probable(issue, now)
        issue.save(update_fields=["probable", "related_docs"])
        n += 1
    for issue in MinusIssue.objects.filter(status=MinusIssue.CLOSED, resolution="").order_by("closed_at")[:REFRESH_LIMIT]:
        resolve(issue)
    return n
