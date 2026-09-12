"""O'z-o'zini davolash (zaxira yo'llar).

Do'kon egasining talabi: qizil/sariq yonsa tizim muammoni o'zi hal qilsin,
odam faqat o'zi hal qila olmaydigan holatda aralashsin.

Panel serveri (hub) kassalardan har 15 soniyada `hello` oladi — u hech
qachon «uxlamaydi». Shuning uchun zaxira ishlar shu yerda, `hello` va
`/aloqa.json` kelganda, fon oqimida bajariladi (so'rovni kutdirmaydi):

  1. **Zaxira yozuvchi.** `sales-sync` xizmati (cheklarni MoySklad'ga
     yozuvchi) har 30 soniyada bazaga «tirikman» yozadi. U 3 daqiqa jim
     qolsa (o'lgan, Railway'da to'xtagan) va navbatda cheklar bo'lsa —
     hub cheklarni o'zi yozadi. Kod bitta (`sales/sender.py`), shuning
     uchun natija bir xil, ikki marta yozilmaydi (syncId).

  2. **Zaxira katalog.** Katalog sinxroni (`sync` xizmati, 5 daqiqada bir)
     15 daqiqa muvaffaqiyatsiz bo'lsa — hub tovar va qoldiqni o'zi tortadi
     (15 daqiqada bir martadan ko'p emas).

  3. **Kassa versiyalari** — GitHub Release'da yangi ZIP paydo bo'lsa
     hub uni o'zi olib, panelning «Versiyalar» ro'yxatiga qo'shadi
     (sales/releases.py, 10 daqiqada bir).

  4. **Tiqilgan cheklar** — `selftest` o'tganda navbatga qaytariladi
     (sales/selftest.py), bu yerda emas.

Cheklash: bitta jarayonda bir vaqtda bitta davolash; 60 soniyada bir
martadan ko'p emas (kesh). MoySklad limitiga zarar yo'q.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils import timezone

from catalog.models import SyncState

from . import sender

logger = logging.getLogger(__name__)

WRITER_ENTITY = "writer"
#: Yozuvchi shuncha jim qolsa — o'lgan deb hisoblaymiz
WRITER_STALE = timedelta(minutes=3)
#: Katalog shuncha yangilanmasa — o'zimiz tortamiz
CATALOG_STALE = timedelta(minutes=15)
#: Davolash urinishi oralig'i (soniya)
TICK_EVERY = 60
CATALOG_EVERY = 15 * 60
#: Bir davolashda nechta chek (hub'ni band qilmaslik uchun)
SEND_LIMIT = 20

_lock = threading.Lock()


# ------------------------------------------------------------- yurak urishi


def writer_heartbeat(now=None) -> None:
    """`sales-sync` sikli chaqiradi: «men tirikman»."""
    now = now or timezone.now()
    SyncState.objects.update_or_create(
        entity=WRITER_ENTITY, defaults={"last_run_at": now, "last_success_at": now},
    )


def writer_seen(now=None):
    """Yozuvchi oxirgi marta qachon ko'ringan (None — hali hech qachon)."""
    state = SyncState.objects.filter(entity=WRITER_ENTITY).first()
    return state.last_run_at if state else None


def writer_alive(now=None) -> bool | None:
    """True — tirik, False — jim, None — hali hech qachon ko'rinmagan."""
    now = now or timezone.now()
    seen = writer_seen(now)
    if seen is None:
        return None
    return now - seen < WRITER_STALE


# ------------------------------------------------------------------ davolash


def tick(background: bool = True) -> bool:
    """`hello` / `/aloqa.json` dan chaqiriladi. 60 soniyada bir marta ishlaydi.

    Qaytaradi: bu safar davolash boshlandimi.
    """
    if not getattr(settings, "MOYSKLAD_TOKEN", ""):
        return False
    if background and not getattr(settings, "HEALER_ENABLED", True):
        return False
    try:
        if not cache.add("healer:tick", 1, TICK_EVERY):
            return False
    except Exception:  # kesh ishlamasa — davolashsiz davom etamiz
        return False
    if background:
        threading.Thread(target=_safe_heal, name="healer", daemon=True).start()
    else:
        _safe_heal(close_connection=False)
    return True


def _safe_heal(close_connection: bool = True) -> None:
    if not _lock.acquire(blocking=False):
        return
    try:
        heal()
    except Exception:
        logger.exception("Davolash kutilmagan xato bilan to'xtadi")
    finally:
        _lock.release()
        try:
            if close_connection:
                connection.close()
        except Exception:
            pass


def heal(now=None) -> dict:
    """Nima kerak bo'lsa shuni qiladi. Natija — nima qilingani (log/test uchun)."""
    now = now or timezone.now()
    done: dict = {}

    # 1. Zaxira yozuvchi
    if writer_alive(now) is False and sender.due_exists(now):
        result = sender.send_due(limit=SEND_LIMIT, now=now)
        done["sent"] = result["sent"]
        done["failed"] = result["failed"] + result["stuck"]
        logger.warning(
            "Zaxira yozuvchi (sales-sync jim): %s ta chek yozildi, %s ta xato",
            result["sent"], done["failed"],
        )

    # 2. Zaxira katalog
    st = SyncState.objects.filter(entity="assortment").first()
    stale = not st or not st.last_success_at or (now - st.last_success_at) > CATALOG_STALE
    if stale and st is not None:  # umuman sinxron bo'lmagan (yangi baza) — tegmaymiz
        try:
            if cache.add("healer:catalog", 1, CATALOG_EVERY):
                done["catalog"] = _pull_catalog()
        except Exception as e:
            logger.warning("Zaxira katalog tortilmadi: %s", e)
            done["catalog_error"] = str(e)[:200]

    # 3. Kassa versiyalari — GitHub Release'dan (10 daqiqada bir).
    #    Egasi hech narsa yuklamaydi: GitHub yig'adi, hub o'zi olib keladi.
    try:
        from . import releases

        rel = releases.check()
        if rel is not None:
            done["release"] = rel.version
    except Exception as e:
        logger.warning("GitHub versiya tekshiruvi: %s", e)

    return done


def _pull_catalog() -> dict:
    from catalog.sync import CatalogSync
    from moysklad.client import MoySkladClient

    sync = CatalogSync(MoySkladClient(token=settings.MOYSKLAD_TOKEN))
    products = sync.sync_products()
    stock = sync.sync_stock()
    logger.warning("Zaxira katalog (sync xizmati jim): %s tovar, %s qoldiq", products, stock)
    return {"products": products, "stock": stock}
