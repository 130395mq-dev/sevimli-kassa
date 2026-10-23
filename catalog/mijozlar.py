"""SEVIMLI BONUS mijozlari: kassada ro'yxatdan o'tkazish va MoySklad'ga yozish.

Egasining talabi (2026-10-22): «bonus dastur faqat MoySklad'ga bog'lanish
kerak emas». Xato (2026-10-22): «registratsiya bo'lgan odam qayta kelib ursa
chiqmayapti». Sababi: kassada ro'yxatdan o'tgan mijozning kartasini server
MoySklad'ga YUBORMASDI, 5–10 daqiqadan keyin mijozlar sinxroni MoySklad'dagi
bo'sh kartani bizning bazaga yozib, kartani o'chirib yuborardi. Kassa
mijozni faqat karta bo'yicha topadi — shuning uchun topilmasdi.

Endi:
  * mijoz avval BIZDA yaratiladi (ism, telefon, karta) — MoySklad ishlamasa
    ham ro'yxatdan o'tkazish ishlaydi;
  * MoySklad'ga darhol, bo'lmasa fon ishida (sales/healer.py) yoziladi —
    karta raqami bilan birga;
  * sinxron bizdagi kartani/telefonni bo'sh qiymat bilan o'chirmaydi
    (catalog/sync.py);
  * kartasi o'chib ketgan eski mijoz shu telefon bilan qayta ro'yxatdan
    o'tkazilsa — yangi yozuv ochilmaydi, eskisiga karta qaytariladi va
    ballari saqlanadi. Telefon BOSHQA kartaga biriktirilgan bo'lsa — rad
    etiladi (kassir birovning ballini o'z kartasiga olib qo'ymasin).
"""

from __future__ import annotations

import logging
import uuid

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q

from .models import Customer

logger = logging.getLogger(__name__)

#: Kassa kutib turgan paytdagi MoySklad so'rovi (soniya)
PUSH_TIMEOUT = 8
#: MoySklad rad etgan mijozni qayta urinishgacha kutish (soniya)
RETRY_AFTER_FAIL = 30 * 60


class RegistrationError(Exception):
    """Kassaga ko'rsatiladigan rad javobi."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def phone_key(phone: str) -> str:
    """Telefonning oxirgi 9 raqami («+998 90 123-45-67» -> «901234567»).

    9 raqamdan kam bo'lsa — bo'sh (solishtirib bo'lmaydi)."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return digits[-9:] if len(digits) >= 9 else ""


def find_by_phone(phone: str) -> list[Customer]:
    key = phone_key(phone)
    if not key:
        return []
    # MoySklad'da telefon turlicha yozilgan bo'lishi mumkin — oxirgi 2 raqam
    # bo'yicha toraytirib, qolganini shu yerda solishtiramiz.
    qs = Customer.objects.filter(archived=False, phone__contains=key[-2:]).exclude(phone="")
    return [c for c in qs if phone_key(c.phone) == key]


def register(name: str, phone: str, card: str) -> tuple[Customer, bool]:
    """Kassadan kelgan yangi mijoz. Qaytaradi: (mijoz, eski_yozuv_tiklandimi)."""
    name, phone, card = (name or "").strip(), (phone or "").strip(), (card or "").strip()
    with transaction.atomic():
        if card:
            owner = Customer.objects.filter(archived=False, discount_card=card).first()
            if owner:
                raise RegistrationError(
                    f"Bu karta allaqachon «{owner.name}» ga biriktirilgan. "
                    "Kartani «Mijoz» oynasida skanerlang."
                )
            same = find_by_phone(phone)
            if same:
                free = [c for c in same if not c.discount_card]
                if not free:
                    busy = same[0]
                    raise RegistrationError(
                        f"Bu telefon raqami boshqa kartaga biriktirilgan "
                        f"(…{busy.discount_card[-4:]}, {busy.name}). O'sha kartani skanerlang."
                    )
                # Kartasi o'chib ketgan eski mijoz: ballari eng ko'p bo'lgani
                best = max(free, key=lambda c: (c.bonus_points, c.pk))
                old = Customer.objects.select_for_update().get(pk=best.pk)
                old.discount_card = card
                if not old.ms_local:
                    old.ms_card_pending = True
                old.save(update_fields=["discount_card", "ms_card_pending"])
                logger.info("Mijoz kartasi tiklandi: #%s %s (karta …%s)", old.pk, old.name, card[-4:])
                return old, True
        cust = Customer.objects.create(
            ms_id=uuid.uuid4(), ms_local=True,
            name=name, phone=phone, discount_card=card,
        )
    logger.info("Yangi mijoz kassadan: #%s %s", cust.pk, cust.name)
    return cust, False


def _client(timeout: int):
    from moysklad.client import MoySkladClient

    # max_retries=1: POST qayta yuborilsa MoySklad'da ikkita kontragent paydo
    # bo'lishi mumkin. Bo'lmasa — keyingi fon urinishi bor.
    return MoySkladClient(token=settings.MOYSKLAD_TOKEN, timeout=timeout, max_retries=1)


def push_one(pk: int, client=None, timeout: int = PUSH_TIMEOUT) -> str:
    """Bitta mijozni MoySklad'ga yozadi. Qaytaradi: 'created' | 'updated' | ''.

    Qator qulflanadi (skip_locked): kassa so'rovi va fon ishi bir mijozni
    bir vaqtda ikki marta yaratmaydi. Xato bo'lsa istisno otadi.
    """
    if client is None and not getattr(settings, "MOYSKLAD_TOKEN", ""):
        return ""
    with transaction.atomic():
        c = (
            Customer.objects.select_for_update(skip_locked=True)
            .filter(pk=pk).filter(Q(ms_local=True) | Q(ms_card_pending=True))
            .first()
        )
        if c is None:
            return ""
        client = client or _client(timeout)
        if c.ms_local:
            payload = {"name": c.name}
            if c.phone:
                payload["phone"] = c.phone
            if c.discount_card:
                payload["discountCardNumber"] = c.discount_card
            created = client.post("entity/counterparty", payload) or {}
            ms_id = created.get("id")
            if not ms_id:
                raise RuntimeError("MoySklad javobida kontragent id yo'q")
            c.ms_id = ms_id
            c.ms_local = False
            c.ms_card_pending = False
            c.save(update_fields=["ms_id", "ms_local", "ms_card_pending"])
            logger.info("Mijoz MoySklad'ga yozildi: #%s %s -> %s", c.pk, c.name, ms_id)
            return "created"
        payload = {"discountCardNumber": c.discount_card}
        if c.phone:
            payload["phone"] = c.phone
        client.put(f"entity/counterparty/{c.ms_id}", payload)
        c.ms_card_pending = False
        c.save(update_fields=["ms_card_pending"])
        logger.info("Mijoz kartasi MoySklad'ga yozildi: #%s %s", c.pk, c.name)
        return "updated"


def push_pending(limit: int = 10, client=None) -> dict:
    """Fon ishi: MoySklad'ga hali yozilmagan mijozlar. Qaytaradi: hisob."""
    done = {"created": 0, "updated": 0, "failed": 0}
    if client is None and not getattr(settings, "MOYSKLAD_TOKEN", ""):
        return done
    ids = list(
        Customer.objects.filter(Q(ms_local=True) | Q(ms_card_pending=True), archived=False)
        .order_by("pk").values_list("pk", flat=True)[: limit * 3]
    )
    n = 0
    for pk in ids:
        if n >= limit:
            break
        if cache.get(f"mijoz-push:{pk}"):
            continue                       # yaqinda rad etilgan — keyinroq
        n += 1
        try:
            r = push_one(pk, client=client, timeout=30)
            if r:
                done[r] += 1
        except Exception as e:  # noqa: BLE001 — bitta mijoz qolganlarini to'xtatmasin
            done["failed"] += 1
            logger.warning("Mijoz #%s MoySklad'ga yozilmadi (keyin qayta uriniladi): %s", pk, e)
            try:
                cache.set(f"mijoz-push:{pk}", 1, RETRY_AFTER_FAIL)
            except Exception:  # noqa: BLE001
                pass
    return done
