"""
Chekni MoySklad'ga yozish.

Yo'l: **Отгрузка** (`demand`) + to'lov hujjatlari.
  naqd    → `cashin`     (Приходный ордер)
  naqdsiz → `paymentin`  (Входящий платёж)

Nega Отгрузка, Розничная продажа emas — chunki `retaildemand` uchun
MoySklad'da pullik «Точка продаж» opsiyasi kerak, sizning maqsadingiz esa
aynan o'sha to'lovdan qutulish edi.

---

Bu modulda uchta ehtiyot chorasi bor, uchalasi ham bir xil narsadan
qo'rqadi: **bitta savdo ikki marta yozilib qolishi.**

1. Har bir hujjatning `syncId` si bor va u bizning bazadagi `local_uuid`.
   Bir xil `syncId` bilan ikkinchi hujjat yaratilmaydi.

2. Natijasi noma'lum bo'lgan xatolarda (tarmoq uzildi, timeout, 5xx)
   darhol qayta yubormaymiz — avval `syncId` bo'yicha qidiramiz.
   Balki hujjat yozilgan, javob yo'lda yo'qolgan.

3. Yozilgandan keyin hujjat summasi tekshiriladi. MoySklad hisoblagan
   summa bizning summamizga teng bo'lmasa — chek `stuck` holatiga o'tadi
   va panelda ko'rinadi. Jimgina noto'g'ri raqam qolgandan ko'ra,
   ko'rinadigan xato yaxshi.

---

TEKSHIRILMAGAN: bu modul hali jonli MoySklad hisobida sinalmagan.
Birinchi ishga tushirishda `--dry-run` bilan yuboriladigan JSON'ni
ko'ring. Ayniqsa quyidagilar tasdiqlanishi kerak:

  - `cashin` va `paymentin` da `operations` maydoni Отгрузка'ga to'g'ri
    bog'lanadimi (hujjat to'langan deb belgilanadimi);
  - vaznli tovarda (0.750 kg) MoySklad hisoblagan summa bizniki bilan
    tiyingacha mos keladimi;
  - `paymentin` uchun `organizationAccount` majburiymi.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from moysklad.client import BASE_URL, MoySkladClient, MoySkladError

from .models import Payment, Sale

logger = logging.getLogger(__name__)


class WriteError(Exception):
    """Chekni yozib bo'lmadi. Xabar panelda ko'rinadi."""


class SumMismatch(WriteError):
    """MoySklad hisoblagan summa bizniki bilan teng emas — odam ko'rishi kerak."""


def meta(entity_type: str, ms_id) -> dict:
    """MoySklad havolasi. Hamma bog'lanish shu ko'rinishda bo'ladi."""
    return {
        "meta": {
            "href": f"{BASE_URL}/entity/{entity_type}/{ms_id}",
            "type": entity_type,
            "mediaType": "application/json",
        }
    }


def ms_moment(dt: datetime) -> str:
    """MoySklad vaqt formati: 2026-08-31 21:39:00 (mahalliy vaqt)."""
    return timezone.localtime(dt).strftime("%Y-%m-%d %H:%M:%S")


def allocate(reduction: int, amounts: list[int]) -> list[int]:
    """`reduction` ni qatorlarga ulushiga qarab taqsimlaydi.

    Qoldiq oxirgi qatorga tushadi, shuning uchun yig'indi **aniq** to'g'ri
    chiqadi. Yaxlitlash tufayli bir tiyin yo'qolishi mumkin emas.
    """
    if reduction <= 0 or not amounts:
        return [0] * len(amounts)

    total = sum(amounts)
    if total <= 0:
        return [0] * len(amounts)

    out = [amount * reduction // total for amount in amounts]
    out[-1] += reduction - sum(out)
    return out


class SaleWriter:
    """Bitta chekni MoySklad'ga yozadi."""

    def __init__(self, client: MoySkladClient, *, dry_run: bool = False):
        self.client = client
        self.dry_run = dry_run
        self.payloads: list[tuple[str, dict]] = []  # dry-run uchun

    # ------------------------------------------------------------ asosiy

    def send(self, sale: Sale) -> None:
        """Chekni to'liq yozadi: Отгрузка + to'lovlar + ball.

        Xato bo'lsa `WriteError` ko'taradi. Yozilgan qismlar bazada
        belgilanadi, shuning uchun keyingi urinish ularni takrorlamaydi.
        """
        if sale.kind == Sale.RETURN:
            self._send_return(sale)
            return

        demand = self._write_demand(sale)
        self._check_sum(sale, demand)

        for payment in sale.payments.select_related("method"):
            self._write_payment(sale, payment, demand)

        self._write_bonus(sale)

    def _send_return(self, sale: Sale) -> None:
        """Qaytarishni yozadi: Возврат (salesreturn) + pulni qaytarish.

        Savdo teskarisiga: Отгрузка o'rniga Возврат, kirim o'rniga chiqim.
        Naqd qaytarilsa — Расходный ордер (cashout), kartaga qaytarilsa —
        Исходящий платёж (paymentout).

        Iloji bo'lsa asl Отгрузка'ga bog'lanadi — shunda MoySklad'da
        «qaysi savdodan qaytdi» ko'rinib turadi. Bog'lanmasa ham yoziladi:
        eski chek MoySklad'ga hali yetib bormagan bo'lishi mumkin.

        UNKNOWN: qaytarishda ball qaytarilishi hali qo'shilmagan. Sabab —
        buni to'g'ri qilish uchun asl savdoning ball tranzaksiyasi kerak,
        va MoySklad Kassa'ning aynan xatti-harakati tasdiqlanmagan. Hozircha
        qaytarishda ballga tegilmaydi.
        """
        salesreturn = self._write_salesreturn(sale)
        self._check_sum(sale, salesreturn)

        for payment in sale.payments.select_related("method"):
            self._write_refund(sale, payment, salesreturn)

    def _write_salesreturn(self, sale: Sale) -> dict:
        if sale.ms_demand_id:
            return self._fetch("salesreturn", sale.local_uuid) or {
                "id": str(sale.ms_demand_id)
            }

        payload = self._salesreturn_payload(sale)
        doc = self._ensure("salesreturn", sale.local_uuid, payload)
        if self.dry_run:
            return doc

        # ms_demand_id maydonini qayta ishlatamiz — u shunchaki «shu chekning
        # MoySklad'dagi hujjati». Qaytarish uchun salesreturn ID'sini saqlaydi.
        sale.ms_demand_id = doc["id"]
        sale.save(update_fields=["ms_demand_id"])
        return doc

    def _salesreturn_payload(self, sale: Sale) -> dict:
        # Возврат Отгрузка bilan bir xil qatordan iborat — kod takrorlanmasin
        payload = self._demand_payload(sale)

        # Asl savdoga bog'lanish (agar u MoySklad'ga yozilgan bo'lsa)
        if sale.origin_id and sale.origin and sale.origin.ms_demand_id:
            payload["demand"] = meta("demand", sale.origin.ms_demand_id)

        return payload

    def _write_refund(self, sale: Sale, payment: Payment, doc: dict) -> None:
        """Pulni qaytaradi: naqd → cashout, karta → paymentout."""
        if payment.ms_payment_id:
            return

        entity = "cashout" if payment.method.is_cash else "paymentout"
        sync_id = str(payment.local_uuid)

        payload = {
            "syncId": sync_id,
            "moment": ms_moment(sale.created_at),
            "sum": payment.amount,
            "organization": meta(
                "organization", sale.shift.register.store.organization_ms_id
            ),
            "agent": self._agent(sale),
            # Возврат'ni «to'langan» (pul qaytarilgan) qiladi
            "operations": [
                {
                    "meta": meta("salesreturn", doc["id"])["meta"],
                    "linkedSum": payment.amount,
                }
            ],
            "description": (
                f"Qaytarish · {payment.method.name} · "
                f"chek {sale.shift.number}-{sale.number}"
            ),
        }

        if not payment.method.is_cash and payment.method.ms_account_id:
            payload["organizationAccount"] = meta("account", payment.method.ms_account_id)

        # Расходный ордер uchun xarajat moddasi kerak bo'lishi mumkin —
        # UNKNOWN, jonli hisobda tekshiriladi. Kerak bo'lsa shu yerga
        # `expenseItem` qo'shiladi.

        result = self._ensure(entity, sync_id, payload)
        if self.dry_run:
            return

        payment.ms_payment_id = result["id"]
        payment.save(update_fields=["ms_payment_id"])

    # ----------------------------------------------------------- Отгрузка

    def _write_demand(self, sale: Sale) -> dict:
        if sale.ms_demand_id:
            # Allaqachon yozilgan — qayta yubormaymiz
            return self._fetch("demand", sale.local_uuid) or {
                "id": str(sale.ms_demand_id)
            }

        payload = self._demand_payload(sale)
        doc = self._ensure("demand", sale.local_uuid, payload)
        if self.dry_run:
            return doc

        sale.ms_demand_id = doc["id"]
        sale.save(update_fields=["ms_demand_id"])
        return doc

    def _demand_payload(self, sale: Sale) -> dict:
        store = sale.shift.register.store

        if not store.organization_ms_id:
            raise WriteError(
                f"«{store.name}» uchun tashkilot ko'rsatilmagan. "
                "Avval `sync_catalog --only retail_stores` ni ishga tushiring."
            )

        items = list(sale.items.all())
        if not items:
            raise WriteError("Chek bo'sh")

        # Ball bilan to'langan qism qatorlarga taqsimlanadi, chunki
        # Отгрузка'da «ball» degan to'lov turi yo'q — bu chegirma kabi
        # ishlaydi. Haqiqiy pul hujjatlari faqat naqd va kartani qoplaydi.
        reductions = allocate(sale.points_spent * 100, [i.total for i in items])

        positions = []
        for item, cut in zip(items, reductions):
            amount = item.total - cut
            qty = Decimal(item.quantity)
            if qty <= 0:
                raise WriteError(f"«{item.name}» miqdori noto'g'ri: {qty}")

            if not item.ms_product_id:
                raise WriteError(f"«{item.name}» MoySklad'da topilmadi")

            positions.append(
                {
                    "quantity": float(qty),
                    # Narx tiyinda. MoySklad summani o'zi ko'paytiradi,
                    # shuning uchun kasrli kilogrammda tiyin farqi
                    # chiqishi mumkin — `_check_sum` shuni tutadi.
                    "price": round(amount / float(qty)),
                    "assortment": meta("product", item.ms_product_id),
                }
            )

        payload = {
            "syncId": str(sale.local_uuid),
            # Nomni o'zimiz qo'ymaymiz — MoySklad avtomatik NOYOB raqam
            # beradi (03411, 03412...). Ilgari "{smena}-{chek}" qo'yardik,
            # lekin u takrorlanib 412 «name uniqueness» xatosini berardi
            # (smena raqami kassalarда qayta-qayta 1 bo'ladi). syncId baribir
            # dublikatни oldini oladi.
            "moment": ms_moment(sale.created_at),
            "applicable": True,
            "organization": meta("organization", store.organization_ms_id),
            "agent": self._agent(sale),
            "positions": positions,
        }

        if store.store_ms_id:
            payload["store"] = meta("store", store.store_ms_id)

        return payload

    def _agent(self, sale: Sale) -> dict:
        if sale.customer_id and sale.customer.ms_id:
            return meta("counterparty", sale.customer.ms_id)
        return meta("counterparty", self._retail_agent_id())

    # Bitta sync_sales ishida takror qidirmaslik uchun jarayon ichida keshlanadi
    _retail_id_cache: str | None = None

    def _retail_agent_id(self) -> str:
        """Mijozsiz savdo uchun «Розничный покупатель» kontragenti ID'si.

        Tartib: (1) sozlamadagi ID, (2) lokal bazadagi mos kontragent,
        (3) MoySklad'dan qidirish, (4) topilmasa — yaratish. Topilgach
        lokalga yoziladi va keyingi savdolar qayta qidirmaydi. Shu tufayli
        do'kon egasi hech qanday ID qo'lda kiritmaydi.
        """
        if SaleWriter._retail_id_cache:
            return SaleWriter._retail_id_cache

        from catalog.models import Customer

        configured = (getattr(settings, "MOYSKLAD_RETAIL_CUSTOMER_ID", "") or "").strip()
        if configured:
            SaleWriter._retail_id_cache = configured
            return configured

        local = (
            Customer.objects.filter(name__iexact="Розничный покупатель").first()
            or Customer.objects.filter(name__icontains="Розничный покупатель").first()
        )
        if local and local.ms_id:
            SaleWriter._retail_id_cache = str(local.ms_id)
            return SaleWriter._retail_id_cache

        # Lokalda yo'q — MoySklad'dan qidiramiz
        try:
            rows = (
                self.client.get("entity/counterparty", search="Розничный покупатель", limit=5)
                or {}
            ).get("rows") or []
        except MoySkladError:
            rows = []
        for row in rows:
            if "розничн" in (row.get("name", "").lower()):
                Customer.objects.update_or_create(
                    ms_id=row["id"],
                    defaults={"name": row.get("name", "Розничный покупатель")},
                )
                SaleWriter._retail_id_cache = row["id"]
                return row["id"]

        # Umuman yo'q — yaratamiz (bir marta)
        created = self.client.post(
            "entity/counterparty", {"name": "Розничный покупатель"}
        )
        Customer.objects.update_or_create(
            ms_id=created["id"],
            defaults={"name": created.get("name", "Розничный покупатель")},
        )
        SaleWriter._retail_id_cache = created["id"]
        return created["id"]

    # ------------------------------------------------------------ to'lov

    def _write_payment(self, sale: Sale, payment: Payment, demand: dict) -> None:
        if payment.ms_payment_id:
            return

        entity = "cashin" if payment.method.is_cash else "paymentin"
        sync_id = str(payment.local_uuid)

        payload = {
            "syncId": sync_id,
            "moment": ms_moment(sale.created_at),
            "sum": payment.amount,
            "organization": meta(
                "organization", sale.shift.register.store.organization_ms_id
            ),
            "agent": self._agent(sale),
            # Shu qator Отгрузка'ni «to'langan» qiladi
            "operations": [
                {
                    "meta": meta("demand", demand["id"])["meta"],
                    "linkedSum": payment.amount,
                }
            ],
            "description": f"{payment.method.name} · chek {sale.shift.number}-{sale.number}",
        }

        if not payment.method.is_cash and payment.method.ms_account_id:
            payload["organizationAccount"] = meta(
                "account", payment.method.ms_account_id
            )

        doc = self._ensure(entity, sync_id, payload)
        if self.dry_run:
            return

        payment.ms_payment_id = doc["id"]
        payment.save(update_fields=["ms_payment_id"])

    # -------------------------------------------------------------- ball

    def _write_bonus(self, sale: Sale) -> None:
        """Ball berish va yechish.

        MoySklad ballni faqat o'z kassasida avtomatik hisoblaydi. Biz
        Отгрузка bilan ketayotganimiz uchun `bonustransaction` ni o'zimiz
        yozamiz — shunda mijozning kartasidagi balans to'g'ri qoladi.
        """
        if not sale.customer_id or not sale.customer.ms_id:
            return

        agent = meta("counterparty", sale.customer.ms_id)
        moment = ms_moment(sale.created_at)

        for kind, points, suffix in (
            ("EARNING", sale.points_earned, "earn"),
            ("SPENDING", sale.points_spent, "spend"),
        ):
            if points <= 0:
                continue
            sync_id = f"{sale.local_uuid}-{suffix}"
            self._ensure(
                "bonustransaction",
                sync_id,
                {
                    "syncId": sync_id,
                    "transactionType": kind,
                    "bonusValue": points,
                    "moment": moment,
                    "agent": agent,
                    "applicable": True,
                },
            )

    # ---------------------------------------------------------- tekshirish

    def _check_sum(self, sale: Sale, demand: dict) -> None:
        """MoySklad hisoblagan summa bizniki bilan tengmi."""
        if self.dry_run:
            return

        got = demand.get("sum")
        if got is None:
            return

        expected = sale.net_total
        if got != expected:
            raise SumMismatch(
                f"MoySklad summani boshqacha hisobladi: {got} tiyin, "
                f"bizda {expected} tiyin (farq {got - expected}). "
                "Chek yozildi, lekin tekshirish kerak."
            )

    # ------------------------------------------------------ past daraja

    def _ensure(self, entity: str, sync_id, payload: dict) -> dict:
        """Hujjatni yaratadi. Allaqachon bor bo'lsa — borini qaytaradi.

        Ikki marta yozilmasligining kafolati shu yerda.
        """
        if self.dry_run:
            self.payloads.append((entity, payload))
            return {"id": f"dry-run-{entity}", "sum": None}

        # Balki oldingi urinishda yozilgan
        existing = self._fetch(entity, sync_id)
        if existing:
            logger.info("%s allaqachon mavjud (syncId=%s)", entity, sync_id)
            return existing

        try:
            return self.client.post(f"entity/{entity}", payload)
        except MoySkladError as e:
            # Natija noma'lum bo'lishi mumkin — qidirib ko'ramiz.
            # Topilsa, demak hujjat yozilgan, javob yetib kelmagan.
            found = self._fetch(entity, sync_id)
            if found:
                logger.warning("%s xatodan keyin topildi (syncId=%s)", entity, sync_id)
                return found
            raise WriteError(f"{entity}: {e}") from e
        except Exception as e:  # tarmoq uzildi
            found = self._fetch(entity, sync_id)
            if found:
                return found
            raise WriteError(f"{entity}: {e}") from e

    def _fetch(self, entity: str, sync_id) -> dict | None:
        """`syncId` bo'yicha hujjatni qidiradi."""
        try:
            result = self.client.get(f"entity/{entity}", filter=f"syncId={sync_id}")
        except MoySkladError:
            return None
        rows = (result or {}).get("rows") or []
        return rows[0] if rows else None
