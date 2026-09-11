"""Aloqa chiroqlari — server ↔ MoySklad va kassa ↔ server.

Panel tepasidagi (har sahifada) va kassa dasturi pastidagi dumaloq
belgilar shu yerdan oziqlanadi. Uchta holat:

    ok    — yashil, tinch: hammasi joyida
    warn  — sariq, sekin yonib-o'chadi: kechikish / navbat, hali xavfli emas
    bad   — qizil, tez yonib-o'chadi: aloqa yo'q yoki xato, qarash kerak

Nega alohida modul: xuddi shu hisob uchta joyda kerak — kassaga `hello`
javobida, panel sahifasida (server tomonda chizish uchun) va panelning
15 soniyalik JSON so'rovida. Bitta joyda hisoblansa, uchta joy bir xil
gapiradi.

MoySklad holati qanday aniqlanadi (server MoySklad'ga o'zi ulanadi,
kassa emas):
  * katalog sinxroni («assortment») oxirgi marta qachon MUVAFFAQIYATLI
    o'tgan — sinxron 5 daqiqada bir, 15 daqiqa o'tsa sariq, 30 — qizil;
  * oxirgi urinish xato bilan tugaganmi (`last_error`);
  * cheklar MoySklad'ga yozilyaptimi — tiqilib qolgan (stuck) chek bo'lsa
    qizil, xato bilan qayta urinilayotgan yoki 5 daqiqadan beri navbatda
    turgan chek bo'lsa sariq.

Kassa holati — `last_seen_at`: kassa har 15 soniyada serverga «tirikman»
deydi, server buni 60 soniyada bir yozadi. 2 daqiqa jim tursa sariq,
5 daqiqa — qizil.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from catalog.models import SyncState
from sales.models import Register, Sale

# Katalog sinxroni (server → MoySklad) uchun chegaralar
CATALOG_WARN = timedelta(minutes=15)
CATALOG_BAD = timedelta(minutes=30)
# Chek 5 daqiqadan beri navbatda tursa — yozuvchi kechikyapti
WRITE_LAG = timedelta(minutes=5)
# Kassa jimligi
KASSA_WARN = timedelta(minutes=2)
KASSA_BAD = timedelta(minutes=5)

# Hisob 10 soniya keshda turadi: 2 ta kassa 15 soniyada bir + panel
# 15 soniyada bir so'rasa ham bazaga bir necha so'rovdan oshmaydi.
CACHE_KEY = "aloqa:moysklad"
CACHE_SEC = 10


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def moysklad_health(now=None, use_cache: bool = True) -> dict:
    """Server ↔ MoySklad: {"state": ok|warn|bad, "text": …, "last_ok": iso|None}."""
    if use_cache:
        cached = cache.get(CACHE_KEY)
        if cached is not None:
            return cached
    result = _moysklad_health(now or timezone.now())
    if use_cache:
        cache.set(CACHE_KEY, result, CACHE_SEC)
    return result


def _moysklad_health(now) -> dict:
    if not getattr(settings, "MOYSKLAD_TOKEN", ""):
        return {"state": "bad", "text": "MoySklad tokeni sozlanmagan", "last_ok": None}

    st = SyncState.objects.filter(entity="assortment").first()
    last_ok = st.last_success_at if st else None

    # Eng yomonidan boshlab tekshiramiz — birinchi topilgani javob.
    problems: list[tuple[str, str]] = []  # (state, text)

    if not last_ok:
        problems.append(("bad", "katalog hali sinxron bo'lmagan"))
    else:
        age = now - last_ok
        if age > CATALOG_BAD:
            problems.append(("bad", f"{_minutes(age)} daqiqadan beri MoySklad javob bermayapti"))
        elif age > CATALOG_WARN:
            problems.append(("warn", f"sinxron {_minutes(age)} daqiqa kechikyapti"))

    # Oxirgi urinish xato bilan tugagan (muvaffaqiyatdan keyin)
    if st and st.last_error and st.last_run_at and (not last_ok or st.last_run_at > last_ok):
        problems.append(("warn", "oxirgi sinxronda xato: " + st.last_error.strip()[:80]))

    # Cheklarni MoySklad'ga yozish
    stuck = Sale.objects.filter(sync_status=Sale.STUCK).count()
    if stuck:
        problems.append(("bad", f"{stuck} ta chek MoySklad'ga yozilmay tiqilib qoldi"))
    failed = Sale.objects.filter(sync_status=Sale.FAILED).count()
    if failed:
        problems.append(("warn", f"{failed} ta chek xato bilan qayta urinilmoqda"))
    lagging = Sale.objects.filter(
        sync_status=Sale.NEW, created_at__lt=now - WRITE_LAG
    ).count()
    if lagging:
        problems.append(("warn", f"{lagging} ta chek {_minutes(WRITE_LAG)} daqiqadan beri navbatda"))

    if not problems:
        return {"state": "ok", "text": "aloqa yaxshi", "last_ok": _iso(last_ok)}
    bad = [p for p in problems if p[0] == "bad"]
    state, text = (bad or problems)[0]
    return {"state": state, "text": text, "last_ok": _iso(last_ok)}


def register_state(reg: Register, now=None) -> dict:
    """Kassa ↔ server: {"id", "name", "state", "text", "seen": iso|None}."""
    now = now or timezone.now()
    seen = reg.last_seen_at
    if not seen:
        state, text = "bad", "hali ulanmagan"
    else:
        age = now - seen
        if age > KASSA_BAD:
            state, text = "bad", f"{_minutes(age)} daqiqadan beri aloqa yo'q"
        elif age > KASSA_WARN:
            state, text = "warn", f"{_minutes(age)} daqiqadan beri jim"
        else:
            state, text = "ok", "ulangan"
    return {"id": reg.pk, "name": reg.name, "state": state, "text": text, "seen": _iso(seen)}


def registers_health(now=None) -> list[dict]:
    """Ro'yxatdagi (arxivlanmagan, faol) kassalar holati."""
    now = now or timezone.now()
    regs = Register.objects.filter(active=True, archived=False).order_by("name")
    return [register_state(r, now) for r in regs]


def snapshot(now=None) -> dict:
    """Panel uchun to'liq surat — tepadagi chiroqlar qatori shundan chiziladi."""
    now = now or timezone.now()
    regs = registers_health(now)
    ms = moysklad_health(now)
    states = [ms["state"]] + [r["state"] for r in regs]
    overall = "bad" if "bad" in states else ("warn" if "warn" in states else "ok")
    return {"at": now.isoformat(), "overall": overall, "moysklad": ms, "registers": regs}
