"""Kirish urinishlarini cheklash (audit I17, 2026-09-28).

Nima uchun LOGIN bo'yicha, IP bo'yicha emas: do'kondagi hamma kassa bitta
tashqi IP'dan chiqadi (NAT). IP bo'yicha bloklash bitta xato terilgan
paroldan butun do'konni to'xtatib qo'yardi. Login bo'yicha cheklov esa
parolni terib topishni sekinlashtiradi va boshqa kassalarga tegmaydi.

Hisoblagich Django keshida. Hozir kesh — har gunicorn jarayonida alohida
(LocMem), ya'ni amalda chegara «jarayonlar soni × LIMIT». Bu terib topishni
baribir minglab marta sekinlashtiradi; umumiy kesh (Redis/DB) keyinroq
qo'shilsa, kod o'zgarmaydi.
"""
from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger("api")

LIMIT = 10              # shuncha xato urinish ...
WINDOW = 15 * 60        # ... shuncha soniya ichida — keyin vaqtincha yopiq
MESSAGE = "Juda ko'p noto'g'ri urinish. 15 daqiqadan keyin qayta urinib ko'ring."


def _key(kind: str, ident: str) -> str:
    return f"kirish:{kind}:{(ident or '').strip().lower()[:150]}"


def blocked(kind: str, ident: str) -> bool:
    return int(cache.get(_key(kind, ident)) or 0) >= LIMIT


def failed(kind: str, ident: str, ip: str = "") -> None:
    key = _key(kind, ident)
    if cache.add(key, 1, WINDOW):
        n = 1
    else:
        try:
            n = cache.incr(key)
        except ValueError:          # muddati shu orada tugagan
            cache.set(key, 1, WINDOW)
            n = 1
    if n == LIMIT:
        logger.warning("Kirish vaqtincha yopildi: %s «%s» (%s ta xato urinish, ip %s)",
                       kind, ident, n, ip or "-")


def succeeded(kind: str, ident: str) -> None:
    cache.delete(_key(kind, ident))


def client_ip(request) -> str:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR", ""))[:64]
