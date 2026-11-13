"""Egaga ogohlantirish (Telegram) — ixtiyoriy.

12.11.2026: kassa3 besh soat davomida 119 ta chekni serverga o'tkaza
olmadi; server buni bilib turgan, lekin hech kimga aytmagan. Endi kassa
chirog'i qizil yonadi (sales/aloqa.py) va — sozlangan bo'lsa — egasining
Telegramiga xabar boradi.

Yoqish (egasi o'zi qo'yadi, Railway → hub → Variables):

    ALERT_TELEGRAM_TOKEN  — BotFather bergan bot tokeni
    ALERT_TELEGRAM_CHAT   — xabar boradigan chat id

Ikkalasi ham bo'lmasa bu modul hech narsa qilmaydi (xato ham bermaydi).

Qoidalar:
  * so'rovni kutdirmaydi — xabar fon oqimida ketadi;
  * hech qachon xato ko'tarmaydi — ogohlantirish savdoni to'xtatmasin;
  * bir mavzu bo'yicha `cooldown` ichida bitta xabar (barcha jarayonlar
    uchun umumiy — «shared» kesh, baza jadvali);
  * token jurnalga yozilmaydi.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

from django.conf import settings
from django.core.cache import caches

logger = logging.getLogger(__name__)

#: Bir mavzu bo'yicha xabarlar orasidagi eng kam vaqt (soniya)
COOLDOWN = 30 * 60
TIMEOUT = 6


def enabled() -> bool:
    return bool(getattr(settings, "ALERT_TELEGRAM_TOKEN", "")
                and getattr(settings, "ALERT_TELEGRAM_CHAT", ""))


def send(text: str) -> bool:
    """Xabarni yuboradi. Muvaffaqiyatli bo'lsa True. Xato ko'tarmaydi."""
    if not enabled():
        return False
    url = f"https://api.telegram.org/bot{settings.ALERT_TELEGRAM_TOKEN}/sendMessage"
    body = json.dumps({
        "chat_id": settings.ALERT_TELEGRAM_CHAT,
        "text": text[:3500],
        "disable_web_page_preview": True,
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as e:
        # Manzil (ichida token bor) jurnalga YOZILMAYDI — faqat kod.
        logger.warning("Ogohlantirish yuborilmadi: Telegram %s", e.code)
    except Exception as e:  # noqa: BLE001 — tarmoq, DNS, timeout…
        logger.warning("Ogohlantirish yuborilmadi: %s", type(e).__name__)
    return False


def _reserve(key: str, cooldown: int) -> bool:
    """Shu mavzu bo'yicha hozir xabar yuborsa bo'ladimi (va band qiladi)."""
    try:
        return bool(caches["shared"].add(f"ogoh:{key}", 1, cooldown))
    except Exception:  # noqa: BLE001 — kesh jadvali yo'q/band: jim turamiz
        logger.exception("Ogohlantirish keshi ishlamadi")
        return False


def once(key: str, text: str, cooldown: int = COOLDOWN) -> bool:
    """`cooldown` ichida bir marta, fon oqimida yuboradi.

    True — xabar navbatga qo'yildi; False — o'chiq yoki yaqinda yuborilgan.
    """
    if not enabled() or not _reserve(key, cooldown):
        return False
    try:
        threading.Thread(target=send, args=(text,), name="ogoh", daemon=True).start()
    except RuntimeError:
        return False
    return True
