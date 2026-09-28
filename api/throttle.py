"""Kirish urinishlarini cheklash (audit I17, 2026-09-28).

Qayerda: kassa ulanishi (/api/v1/connect), kassir kirishi (/api/v1/login —
bu yo'lga kassa tokenisiz kirib bo'lmaydi) va panel (/kirish/).

IKKI HISOBLAGICH, ikkalasi ham bazada (api.LoginThrottle) — shuning uchun
hamma gunicorn jarayoni va thread'i bitta hisobni ko'radi:

1. «login + IP» — PAIR_LIMIT xato urinishdan keyin shu login shu IP'dan
   WINDOW davomida yopiladi. Do'kondagi hamma kassa bitta tashqi IP'dan
   chiqadi (NAT): begona IP'dan kelgan hujum do'kondagi xodimni yopmaydi.
2. «login, hamma IP» — GLOBAL_LIMIT xato urinishdan keyin shu login
   hamma joydan WINDOW davomida yopiladi. Bu — ko'p IP'dan (botnet) parol
   terishning yuqori chegarasi. Narxi: kimdir ataylab GLOBAL_LIMIT marta
   xato tersa, xodim 15 daqiqaga kira olmaydi (lockout-DoS). Shu sababli
   chegara ancha baland va muddat qisqa; ochish yo'llari pastda.

Login yozilishi: NFKC + casefold + bo'shliqsiz. «Kassa1», «KASSA1»,
«ｋａｓｓａ１» (to'liq kenglik) — bitta hisob. Bu tekshiruvdagi qidiruvdan
yo'g'onroq (bir xil yoki ko'proq variant bir kalitga tushadi), ya'ni
yozilishni o'zgartirib hisobni aylanib o'tib bo'lmaydi.

IP: Railway chekkasi qo'yadigan X-Real-IP (docs.railway.com, «Specs &
Limits»). X-Forwarded-For'ning birinchi qiymatini mijoz o'zi yozishi
mumkin — unga ishonilmaydi. IP soxtalashtirilsa ham 2-hisoblagich
IP'ga bog'liq emas.

OCHISH (tiklash) yo'llari:
  * o'zi: WINDOW (15 daqiqa) o'tgach;
  * to'g'ri parol: shu login+IP hisobini nolga tushiradi;
  * panel → Admin → «Kirish cheklovlari»: yozuvni o'chirish;
  * buyruq: python manage.py kirish_ochish <login>   (yoki --hammasi).
"""
from __future__ import annotations

import logging
import unicodedata
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger("api")

PAIR_LIMIT = 10          # login + IP: shuncha xato ...
GLOBAL_LIMIT = 50        # login, hamma IP: shuncha xato ...
WINDOW = 15 * 60         # ... shuncha soniya ichida — keyin shuncha vaqt yopiq
LIMIT = PAIR_LIMIT       # eski nom (testlar va chaqiruvchilar uchun)
MESSAGE = "Juda ko'p noto'g'ri urinish. 15 daqiqadan keyin qayta urinib ko'ring."
KEEP = timedelta(days=2)  # eski yozuvlar shuncha vaqtdan keyin tozalanadi


def normalize(ident: str) -> str:
    text = unicodedata.normalize("NFKC", str(ident or ""))
    return "".join(text.casefold().split())[:150]


def _keys(kind: str, ident: str, ip: str) -> list[tuple[str, int]]:
    who = f"{kind}:{normalize(ident)}"
    return [(f"{who}|ip:{(ip or '-')[:60]}", PAIR_LIMIT), (who, GLOBAL_LIMIT)]


def _model():
    from api.models import LoginThrottle
    return LoginThrottle


def blocked(kind: str, ident: str, ip: str = "") -> bool:
    keys = [k for k, _ in _keys(kind, ident, ip)]
    return _model().objects.filter(key__in=keys, blocked_until__gt=timezone.now()).exists()


def _bump(key: str, limit: int, now) -> int:
    model = _model()
    window = timedelta(seconds=WINDOW)
    for attempt in range(2):
        try:
            with transaction.atomic():
                row, created = model.objects.select_for_update().get_or_create(
                    key=key, defaults={"window_start": now, "failures": 0})
                expired_block = row.blocked_until and row.blocked_until <= now
                if expired_block or (not row.blocked_until and row.window_start <= now - window):
                    row.failures, row.window_start, row.blocked_until = 0, now, None
                row.failures += 1
                if row.failures >= limit and not row.blocked_until:
                    row.blocked_until = now + window
                row.save()
                if created:
                    model.objects.filter(updated_at__lt=now - KEEP).delete()
                return row.failures
        except IntegrityError:
            if attempt:
                raise
    return 0


def failed(kind: str, ident: str, ip: str = "") -> None:
    now = timezone.now()
    for key, limit in _keys(kind, ident, ip):
        n = _bump(key, limit, now)
        if n == limit:
            # Login ko'rinadi (kim yopilganini bilish uchun), parol — hech qachon.
            logger.warning("Kirish vaqtincha yopildi: %s (%s ta xato urinish, ip %s)",
                           key[:120], n, (ip or "-")[:60])


def succeeded(kind: str, ident: str, ip: str = "") -> None:
    """Faqat «login + IP» hisobi tozalanadi. Umumiy hisob o'z muddati bilan
    tushadi — aks holda hujumchi xodim kirishi bilan yangi urinishlar olardi."""
    pair_key = _keys(kind, ident, ip)[0][0]
    _model().objects.filter(key=pair_key).delete()


def clear(ident: str | None = None) -> int:
    """Admin/buyruq uchun: login bo'yicha (hamma turi va IP) yoki hammasini ochadi."""
    qs = _model().objects.all()
    if ident is not None:
        who = normalize(ident)
        rows = []
        for r in qs.only("pk", "key"):
            # kalit: «tur:login» yoki «tur:login|ip:...»; kassir kirishida
            # login «kassa_id:login» ko'rinishida
            name = r.key.split("|ip:", 1)[0].split(":", 1)[-1]
            if name == who or name.endswith(":" + who):
                rows.append(r.pk)
        qs = qs.filter(pk__in=rows)
    return qs.delete()[0]


def client_ip(request) -> str:
    real = (request.META.get("HTTP_X_REAL_IP") or "").strip()
    return (real or request.META.get("REMOTE_ADDR", ""))[:60]
