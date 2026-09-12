"""
Bir login — bir vaqtda bitta kompyuter.

Kassir kassaga kirganda login o'sha kompyuterga biriktiriladi
(`KassaSession`). Boshqa kompyuter shu login bilan kirmoqchi bo'lsa —
`Busy` ko'tariladi va kassa ekranida «Bu login hozir Kassa-1 · DESKTOP-7
kompyuterida ishlayapti» chiqadi.

Qachon bo'shaydi:
  * kassir «Chiqish» ni bosganda (`release`);
  * kompyuter ALIVE_SECONDS (3 daqiqa) jim qolganda — kassa har 15
    soniyada `hello` yuboradi va shunda `touch` bo'ladi; o'chirilgan yoki
    buzilgan kompyuter shu tufayli boshqalarni abadiy bloklamaydi;
  * panelda «Bo'shatish» bosilganda (`release_all`).

Qurilma ID kassa ilovasi tomonidan yaratiladi va `X-Device` sarlavhasida
keladi. Eski ilovalar (1.15 va undan oldingi) sarlavha yubormaydi — ular
uchun tekshiruv qilinmaydi (fleet 30 daqiqada o'zi yangilanadi).
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models import KassaSession, Register

#: `touch` bazaga shundan tez-tez yozmaydi (kassa 15 s da bir keladi)
TOUCH_EVERY = 30


class Busy(Exception):
    """Login boshqa kompyuterda ishlayapti."""

    def __init__(self, session: KassaSession):
        self.session = session
        super().__init__(message_for(session))


def message_for(session: KassaSession) -> str:
    since = timezone.localtime(session.started_at).strftime("%H:%M")
    who = f" ({session.cashier_name})" if session.cashier_name else ""
    return (
        f"Bu login hozir «{session.holder}» kompyuterida ishlayapti{who}, "
        f"{since} dan beri. Avval o'sha kassada «Chiqish» ni bosing."
    )


def device_of(request) -> tuple[str, str]:
    """So'rovdan qurilma ID va nomi. Yo'q bo'lsa — bo'sh (eski ilova)."""
    device = (request.headers.get("X-Device") or "").strip()[:64]
    name = (request.headers.get("X-Device-Name") or "").strip()[:128]
    return device, name


def acquire(login: str, register: Register, device: str, device_name: str = "",
            cashier_id: int = 0, cashier_name: str = "") -> KassaSession | None:
    """Loginni shu kompyuterga biriktiradi.

    Boshqa kompyuter ushlab turgan va u tirik bo'lsa — `Busy`. O'sha
    kompyuterning o'zi bo'lsa yoki eski egasi jim qolgan bo'lsa — yozuv
    yangilanadi. Qurilma ID bo'lmasa (eski ilova) — hech narsa qilinmaydi.
    """
    login = (login or "").strip().lower()
    if not device or not login:
        return None
    now = timezone.now()
    with transaction.atomic():
        Register.objects.select_for_update().get(pk=register.pk)
        row = (KassaSession.objects.select_for_update()
               .select_related("register").filter(login=login).first())
        if row and row.device != device and row.alive(now):
            raise Busy(row)
        if row is None:
            row = KassaSession(login=login)
        row.register = register
        row.device = device
        row.device_name = device_name
        row.cashier_id = cashier_id
        row.cashier_name = cashier_name
        row.started_at = now
        row.seen_at = now
        row.save()
        return row


def touch(register: Register, device: str, now=None) -> KassaSession | None:
    """Kassa tirik — `seen_at` yangilanadi (30 s da bir martadan ko'p emas).

    Qaytaradi: shu kompyuter ushlab turgan sessiya (bo'lsa)."""
    if not device:
        return None
    now = now or timezone.now()
    row = (KassaSession.objects.select_related("register")
           .filter(register=register, device=device).first())
    if row and (now - row.seen_at).total_seconds() >= TOUCH_EVERY:
        KassaSession.objects.filter(pk=row.pk).update(seen_at=now)
        row.seen_at = now
    return row


def state_for(register: Register, device: str, login: str = "", now=None) -> dict:
    """Kassa uchun holat (hello javobi):

        {"mine": True}                 — sessiya shu kompyuterda
        {"mine": False, "holder": …}   — boshqa kompyuter olib qo'ygan
        {"mine": None}                 — hech kim kirmagan / eski ilova
    """
    if not device:
        return {"mine": None, "holder": ""}
    now = now or timezone.now()
    login = (login or register.login or "").lower()
    row = (KassaSession.objects.select_related("register")
           .filter(login=login).first()) if login else None
    if row is None:
        return {"mine": None, "holder": ""}
    if row.device == device:
        return {"mine": True, "holder": ""}
    if not row.alive(now):
        return {"mine": None, "holder": ""}
    return {"mine": False, "holder": row.holder, "message": message_for(row)}


def release(register: Register, device: str) -> int:
    """«Chiqish»: shu kompyuter ushlab turgan sessiyalarni bo'shatadi."""
    if not device:
        return 0
    n, _ = KassaSession.objects.filter(register=register, device=device).delete()
    return n


def release_all(register: Register) -> int:
    """Paneldagi «Bo'shatish» — kassaning hamma sessiyalari."""
    n, _ = KassaSession.objects.filter(register=register).delete()
    return n


def holders(now=None) -> dict[int, KassaSession]:
    """Panel uchun: register pk → tirik sessiya."""
    now = now or timezone.now()
    out = {}
    for row in KassaSession.objects.select_related("register"):
        if row.alive(now):
            out[row.register_id] = row
    return out
