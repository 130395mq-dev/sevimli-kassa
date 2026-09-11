"""Aloqa chiroqlari — server ↔ MoySklad va kassa ↔ server.

Panel tepasidagi (har sahifada) va kassa dasturi pastidagi dumaloq
belgilar shu yerdan oziqlanadi. To'rt holat:

    ok       — yashil, tinch: hammasi joyida
    warn     — sariq, sekin yonib-o'chadi: kechikish / navbat, hali xavfli emas
    bad      — qizil, tez yonib-o'chadi: aloqa yo'q yoki xato, qarash kerak
    unknown  — kulrang: kassa o'chirilgan (smena yopiq) — muammo emas

Har muammo bilan birga **`fix`** — «nima qilish kerak» matni keladi.
Tizim o'zi hal qila oladiganini o'zi qiladi (sales/healer.py,
sales/selftest.py); odam kerak bo'lsa `fix` aniq qadamni aytadi.

Nega alohida modul: xuddi shu hisob uchta joyda kerak — kassaga `hello`
javobida, panel sahifasida va panelning 15 soniyalik JSON so'rovida.

MoySklad holati (server MoySklad'ga o'zi ulanadi, kassa emas):
  * katalog sinxroni («assortment») oxirgi marta qachon MUVAFFAQIYATLI
    o'tgan — 5 daqiqada bir bo'ladi; 15 daqiqa o'tsa sariq, 30 — qizil;
  * oxirgi urinish xato bilan tugaganmi (`last_error`);
  * cheklar MoySklad'ga yozilyaptimi — tiqilgan (stuck) chek bo'lsa qizil,
    qayta urinilayotgan yoki 5 daqiqadan beri navbatda turgan chek bo'lsa
    sariq;
  * yozuvchi xizmat (`sales-sync`) tirikmi — 3 daqiqa jim bo'lsa sariq
    (panel serveri zaxira yo'l bilan yozib turadi);
  * o'z-o'zini tekshirish (sinov) natijasi.

Kassa holati — `last_seen_at`: kassa har 15 soniyada serverga «tirikman»
deydi, server buni 60 soniyada bir yozadi. Smena ochiq turib 2 daqiqa jim
qolsa sariq, 5 daqiqa — qizil. Smena yopiq bo'lsa jimlik — kulrang
(«o'chirilgan»): bu muammo emas, bekorga qizil yonmaydi.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from catalog.models import SyncState
from sales.models import MoySkladCheck, Register, Sale, Shift

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

# ---- «Nima qilish kerak» matnlari (odam uchun, panelda chiqadi)
FIX_TOKEN = (
    "MoySklad tokeni ishlamayapti. Railway → hub → Variables → MOYSKLAD_TOKEN ga "
    "alohida integratsiya foydalanuvchisining yangi tokenini yozing. "
    "Ungacha cheklar navbatda turadi, yo'qolmaydi."
)
FIX_MS_DOWN = (
    "MoySklad javob bermayapti. online.moysklad.ru ochilyaptimi — tekshiring. "
    "Ochilsa — token bekor bo'lgan bo'lishi mumkin (MOYSKLAD_TOKEN). "
    "Cheklar navbatda turadi, aloqa qaytgach o'zi yoziladi."
)
FIX_WAIT = "Hech narsa qilish shart emas — tizim o'zi qayta urinadi."
FIX_STUCK = (
    "Tizim o'zi hal qiladi: MoySklad sinovi o'tishi bilan (eng ko'pi 30 daqiqa) "
    "tiqilganlar qayta yuboriladi. Shoshilsangiz — «Qayta yuborish». "
    "Sabab «Yozilmagan cheklar» jadvalida."
)
FIX_WRITER = (
    "Cheklarni yozuvchi xizmat (sales-sync) jim. Panel serveri zaxira yo'l bilan "
    "o'zi yozib turibdi. Railway → sales-sync → Restart bosilsa tiklanadi."
)
FIX_EXPENSE = (
    "MoySklad → Настройки → Справочники → Статьи расходов bo'limida "
    "«Возврат» nomli modda yarating. Keyingi sinov (30 daqiqa) o'zi tekshiradi."
)
FIX_REGISTER_SETUP = "Panel → Kassalar → Sozlash: ombor (sklad) va tashkilotni tanlang."
FIX_RETAIL = "MoySklad'da «Розничный покупатель» nomli kontragent yarating (Контрагенты)."
FIX_LEFTOVER = (
    "MoySklad'da «SINOV-» deb qidirib, qolgan sinov hujjatlarini o'chiring "
    "(keyingi sinov o'zi ham urinadi). Ular «проведён» emas — hisobga ta'sir yo'q."
)
FIX_DOC_REJECTED = (
    "MoySklad hisobi hujjatni rad etdi. Xato matnini o'qing: majburiy maydon "
    "bo'lsa MoySklad sozlamasida to'ldiring; tushunarsiz bo'lsa panel "
    "skrinshotini menga yuboring."
)
FIX_KASSA_OFFLINE = (
    "Kassadagi internetni tekshiring (Wi-Fi / kabel / router). Kassa ishlayveradi: "
    "cheklar kassada saqlanadi, aloqa qaytgach o'zi yuboriladi."
)


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _fix_for_step(step: dict) -> str:
    """Sinov bosqichi nomiga qarab — nima qilish kerak."""
    name = (step.get("name") or "").lower()
    detail = (step.get("detail") or "").lower()
    if "ulanish" in name:
        return FIX_TOKEN if ("401" in detail or "rad" in detail or "token" in detail) else FIX_MS_DOWN
    if "xarajat" in name:
        return FIX_EXPENSE
    if "sozlama" in name:
        return FIX_REGISTER_SETUP
    if "roznichniy" in name:
        return FIX_RETAIL
    if "o'chirish" in name or "eski sinov" in name:
        return FIX_LEFTOVER
    return FIX_DOC_REJECTED


def moysklad_health(now=None, use_cache: bool = True) -> dict:
    """Server ↔ MoySklad: {"state", "text", "fix", "last_ok"}."""
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
        return {"state": "bad", "text": "MoySklad tokeni sozlanmagan", "fix": FIX_TOKEN, "last_ok": None}

    st = SyncState.objects.filter(entity="assortment").first()
    last_ok = st.last_success_at if st else None

    # Eng yomonidan boshlab tekshiramiz — birinchi topilgani javob.
    problems: list[tuple[str, str, str]] = []  # (state, text, fix)

    if not last_ok:
        problems.append(("bad", "katalog hali sinxron bo'lmagan",
                         "Railway'da `sync` xizmati ishlayaptimi — tekshiring; token to'g'ri bo'lsa 5 daqiqada o'zi tortadi."))
    else:
        age = now - last_ok
        if age > CATALOG_BAD:
            problems.append(("bad", f"{_minutes(age)} daqiqadan beri MoySklad javob bermayapti", FIX_MS_DOWN))
        elif age > CATALOG_WARN:
            problems.append(("warn", f"sinxron {_minutes(age)} daqiqa kechikyapti",
                             FIX_WAIT + " (server o'zi ham tortishga urinadi)"))

    # Oxirgi urinish xato bilan tugagan (muvaffaqiyatdan keyin)
    if st and st.last_error and st.last_run_at and (not last_ok or st.last_run_at > last_ok):
        err = st.last_error.strip()
        fix = FIX_TOKEN if "401" in err else (
            "MoySklad limiti — kutish, o'zi tiklanadi." if ("429" in err or "1073" in err) else FIX_MS_DOWN
        )
        problems.append(("warn", "oxirgi sinxronda xato: " + err[:80], fix))

    # Cheklarni MoySklad'ga yozish
    stuck = Sale.objects.filter(sync_status=Sale.STUCK).count()
    if stuck:
        problems.append(("bad", f"{stuck} ta chek MoySklad'ga yozilmay tiqilib qoldi", FIX_STUCK))
    failed = Sale.objects.filter(sync_status=Sale.FAILED).count()
    if failed:
        problems.append(("warn", f"{failed} ta chek xato bilan qayta urinilmoqda",
                         FIX_WAIT + " (1, 2, 4… daqiqada)."))
    lagging = Sale.objects.filter(
        sync_status=Sale.NEW, created_at__lt=now - WRITE_LAG
    ).count()
    if lagging:
        problems.append(("warn", f"{lagging} ta chek {_minutes(WRITE_LAG)} daqiqadan beri navbatda",
                         "Yozuvchi kechikmoqda — panel serveri zaxira yo'l bilan o'zi yozadi. 15 daqiqadan oshsa menga yozing."))

    # Yozuvchi xizmat (sales-sync) tirikmi
    from . import healer  # aylanma import bo'lmasin

    alive = healer.writer_alive(now)
    if alive is False:
        seen = healer.writer_seen(now)
        problems.append(("warn", f"yozuvchi xizmat {_minutes(now - seen)} daqiqadan beri jim — server o'zi yozmoqda",
                         FIX_WRITER))

    # O'z-o'zini tekshirish (sinov): hisob sozlamasi biror hujjatni rad
    # etsa — haqiqiy chek tiqilmasdan OLDIN qizil yonadi.
    check = MoySkladCheck.latest()
    if check is not None and check.finished_at is not None:
        if check.failed_steps:
            problems.append(("bad", "sinov o'tmadi — " + check.summary, _fix_for_step(check.failed_steps[0])))
        elif check.leftovers:
            problems.append(("warn", f"{len(check.leftovers)} ta sinov hujjati MoySklad'da o'chmay qoldi", FIX_LEFTOVER))

    if not problems:
        return {"state": "ok", "text": "aloqa yaxshi", "fix": "", "last_ok": _iso(last_ok)}
    bad = [p for p in problems if p[0] == "bad"]
    state, text, fix = (bad or problems)[0]
    return {"state": state, "text": text, "fix": fix, "last_ok": _iso(last_ok)}


def register_state(reg: Register, now=None, shift_open: bool | None = None) -> dict:
    """Kassa ↔ server: {"id", "name", "state", "text", "fix", "seen", "shift_open"}.

    Smena ochiq bo'lsa jimlik — muammo (sariq/qizil). Smena yopiq bo'lsa
    kassa shunchaki o'chirilgan — kulrang, ogohlantirish yo'q.
    """
    now = now or timezone.now()
    if shift_open is None:
        shift_open = reg.shifts.filter(status=Shift.OPEN).exists()
    seen = reg.last_seen_at
    fix = ""
    if not seen:
        if shift_open:
            state, text, fix = "bad", "smena ochiq, kassa hali ulanmagan", FIX_KASSA_OFFLINE
        else:
            state, text = "unknown", "hali ulanmagan"
    else:
        age = now - seen
        if age > KASSA_BAD:
            if shift_open:
                state, text, fix = "bad", f"smena ochiq, {_minutes(age)} daqiqadan beri aloqa yo'q", FIX_KASSA_OFFLINE
            else:
                state, text = "unknown", f"o'chirilgan (smena yopiq, {_minutes(age)} daqiqa)"
        elif age > KASSA_WARN:
            if shift_open:
                state, text = "warn", f"{_minutes(age)} daqiqadan beri jim"
            else:
                state, text = "unknown", "o'chirilgan (smena yopiq)"
        else:
            state, text = "ok", "ulangan"
    return {
        "id": reg.pk, "name": reg.name, "state": state, "text": text, "fix": fix,
        "seen": _iso(seen), "shift_open": bool(shift_open),
    }


def registers_health(now=None) -> list[dict]:
    """Ro'yxatdagi (arxivlanmagan, faol) kassalar holati."""
    now = now or timezone.now()
    regs = Register.objects.filter(active=True, archived=False).order_by("name")
    open_ids = set(
        Shift.objects.filter(status=Shift.OPEN, register__in=regs).values_list("register_id", flat=True)
    )
    return [register_state(r, now, shift_open=r.pk in open_ids) for r in regs]


def snapshot(now=None) -> dict:
    """Panel uchun to'liq surat — tepadagi chiroqlar qatori shundan chiziladi."""
    now = now or timezone.now()
    regs = registers_health(now)
    ms = moysklad_health(now)
    states = [ms["state"]] + [r["state"] for r in regs]
    overall = "bad" if "bad" in states else ("warn" if "warn" in states else "ok")
    return {"at": now.isoformat(), "overall": overall, "moysklad": ms, "registers": regs}
