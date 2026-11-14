"""SMS fon ishi: tabriklarni yuborish va yetkazilganini tekshirish.

Qoldiq sinxronizatsiyasi (cron, bir necha daqiqada bir) tugagach chaqiriladi
— catalog/sync.py. SMS xizmati ulanmagan bo'lsa bazaga ham tegmaydi.
"""

from __future__ import annotations

import logging

from django.db import OperationalError, ProgrammingError

from . import provayder, xizmat

logger = logging.getLogger(__name__)


def run(now=None) -> dict:
    p = provayder.joriy()
    if p is None:
        return {}
    return {"tabrik": xizmat.tabriklarni_yubor(now, p), "holat": xizmat.holatlarni_yangila(now, p)}


def after_stock_sync() -> None:
    """Qoldiq sinxronizatsiyasidan keyin. HECH QACHON xato tashlamaydi."""
    try:
        run()
    except (ProgrammingError, OperationalError) as e:
        # Deploy payti: bu jarayon yangi kod bilan ishga tushgan, migratsiya esa
        # hali o'tmagan bo'lishi mumkin — keyingi navbatda o'zi tuzaladi.
        logger.warning("SMS fon ishi o'tkazib yuborildi (baza tayyor emas): %s", type(e).__name__)
    except Exception:
        logger.exception("SMS fon ishi bajarilmadi (qoldiq sinxronizatsiyasiga ta'siri yo'q)")
