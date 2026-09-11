"""MoySklad o'z-o'zini tekshirish (sinov).

Nima uchun: 2026-09 da qaytarish cheklari MoySklad'da rad etilib («expenseItem
majburiy»), 1,5 kun sezilmay tiqilib turdi. Kod jonli hisobda sinalmagan
edi. Endi server o'zi, haqiqiy chek yozilishidan OLDIN, kassa yozadigan
HAMMA hujjat turini shu hisobda sinab ko'radi:

    Отгрузка (demand)        → har bir to'lov turi bilan kirim (cashin/paymentin)
    Возврат (salesreturn)    → har bir to'lov turi bilan chiqim (cashout/paymentout)

va bularni kassa yozadigan AYNAN o'sha kod (`SaleWriter`) bilan yozadi —
sinov alohida nusxa emas, shuning uchun yozuvchi o'zgarsa sinov ham o'sha
yo'ldan yuradi.

SAVDOGA TA'SIR QILMASLIK KAFOLATI (do'kon egasining talabi):
  1. Sinov hujjatlari «проведён» QILINMAYDI (applicable=false). MoySklad
     bunday hujjatni qoldiqqa ham, kassadagi pulga ham, hisobotlarga ham
     qo'shmaydi — o'chmay qolsa ham zarar yo'q.
  2. Nomi «SINOV-…» — ro'yxatda darrov tanilyadi va MoySklad'ning avtomatik
     raqamlarini (03411, 03412…) sarflamaydi.
  3. Yozilgan zahoti o'chiriladi. O'chmay qolgani `leftovers` da eslab
     qolinadi va keyingi sinovda yana o'chiriladi; panelda ko'rinib turadi.
  4. Sinov faqat navbat bo'sh paytda ishlaydi (sync_sales sikli shunday
     chaqiradi) — haqiqiy cheklar har doim birinchi.
  5. Kassa sinovga bog'liq emas: sinov faqat chiroq rangini beradi.

Natija `MoySkladCheck` jadvaliga yoziladi: panel bosh sahifasi va aloqa
chiroqlari (panel tepasi, kassa pastki qatori) shundan o'qiydi.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from catalog.models import Product, Stock
from moysklad.client import MoySkladClient, MoySkladError

from .models import MoySkladCheck, PaymentMethod, Register, Sale
from .writer import SaleWriter, WriteError

logger = logging.getLogger(__name__)

NAME_PREFIX = "SINOV-"
#: Har to'lov turiga sinov summasi — 10 so'm (tiyinda)
AMOUNT_PER_METHOD = 10_00
#: Bazada nechta oxirgi sinov saqlanadi
KEEP_RUNS = 30
#: Vaqti-vaqti bilan sinov oralig'i
PERIODIC_EVERY = timedelta(hours=3)

# Bir vaqtda ikkita sinov yurmasin (panel tugmasi + sikl)
_lock = threading.Lock()


class SelfTestBusy(Exception):
    """Sinov allaqachon ketmoqda."""


# ------------------------------------------------------------ soxta chek
#
# Yozuvchi (`SaleWriter`) haqiqiy `Sale` bilan ishlaydi. Bazaga sinov cheki
# YOZMAYMIZ (savdo tarixi iflos bo'lmasin) — shuning uchun yozuvchiga
# xuddi Sale kabi «ko'rinadigan», lekin `save()` hech narsa qilmaydigan
# obyektlar beramiz. Yozuvchi hujjat ID'larini shu obyektlarga yozadi —
# biz ularni o'chirish uchun o'qib olamiz.


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def select_related(self, *args, **kwargs):
        return list(self._rows)


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def save(self, *args, **kwargs):  # bazaga yozilmaydi
        pass


def _fake_sale(register: Register, product: Product, methods: list[PaymentMethod],
               *, kind: str = Sale.SALE, origin=None) -> _Obj:
    total = AMOUNT_PER_METHOD * max(1, len(methods))
    item = _Obj(
        name=product.name, quantity="1.000", total=total,
        ms_product_id=str(product.ms_id),
    )
    payments = [
        _Obj(local_uuid=uuid.uuid4(), method=m, amount=AMOUNT_PER_METHOD, ms_payment_id=None)
        for m in methods
    ]
    return _Obj(
        kind=kind,
        number=0,
        local_uuid=uuid.uuid4(),
        created_at=timezone.now(),
        points_spent=0,
        points_earned=0,
        net_total=total,
        customer_id=None,
        customer=None,
        shift=_Obj(register=register, number=0),
        items=_Rows([item]),
        payments=_Rows(payments),
        ms_demand_id=None,
        origin_id=getattr(origin, "id", None),
        origin=origin,
    )


# ------------------------------------------------------------- sinov o'zi


class SelfTest:
    def __init__(self, client: MoySkladClient, trigger: str = MoySkladCheck.MANUAL):
        self.client = client
        self.trigger = trigger
        self.steps: list[dict] = []
        self.leftovers: list[dict] = []
        self.writer = SaleWriter(client, applicable=False, name_prefix=NAME_PREFIX)

    # -- bosqich yozuvi
    def _ok(self, name: str, detail: str = "") -> None:
        self.steps.append({"name": name, "ok": True, "detail": detail})

    def _fail(self, name: str, detail: str) -> None:
        self.steps.append({"name": name, "ok": False, "detail": str(detail)[:400]})

    def _skip(self, name: str, detail: str) -> None:
        self.steps.append({"name": name, "ok": True, "skipped": True, "detail": detail})

    # -- asosiy
    def run(self) -> MoySkladCheck:
        check = MoySkladCheck.objects.create(trigger=self.trigger)
        try:
            self._run_all()
        except Exception as e:  # kutilmagan xato — sinov emas, kod yiqildi
            logger.exception("Sinov kutilmagan xato bilan to'xtadi")
            self._fail("Sinov", f"kutilmagan xato: {e}")
        finally:
            check.steps = self.steps
            check.leftovers = self.leftovers
            check.ok = all(s.get("ok") for s in self.steps) and not self.leftovers
            check.finished_at = timezone.now()
            check.save()
            _prune()
        return check

    def _run_all(self) -> None:
        # 1. Ulanish
        try:
            info = self.client.check_connection()
            self._ok("MoySklad ulanish", f"foydalanuvchi: {info.get('employee') or '—'}")
        except MoySkladError as e:
            self._fail("MoySklad ulanish", f"token rad etildi yoki server javob bermadi: {e}")
            return
        except Exception as e:
            self._fail("MoySklad ulanish", f"ulanib bo'lmadi: {e}")
            return

        # 2. Eski sinov hujjatlari (o'tgan safar o'chmay qolgan)
        self._cleanup_old_leftovers()

        # 3. Mijozsiz savdo uchun kontragent
        try:
            SaleWriter._retail_id_cache = None
            rid = self.writer._retail_agent_id()
            self._ok("Roznichniy mijoz", f"topildi ({rid[:8]}…)")
        except Exception as e:
            self._fail("Roznichniy mijoz", f"«Розничный покупатель» topilmadi/yaratilmadi: {e}")

        # 4. Qaytarish uchun xarajat moddasi
        try:
            SaleWriter._expense_item_cache = None
            eid = self.writer._expense_item_id()
            self._ok("Xarajat moddasi", f"topildi ({eid[:8]}…)")
        except Exception as e:
            self._fail("Xarajat moddasi", str(e))

        # 5. Har (tashkilot, ombor) juftligi uchun to'liq aylanish
        methods = list(PaymentMethod.objects.filter(active=True))
        if not methods:
            self._skip("To'lov turlari", "faol to'lov turi yo'q — hujjat sinovi o'tkazib yuborildi")
            return

        combos: dict[tuple, Register] = {}
        for reg in Register.objects.filter(active=True, archived=False).select_related("store"):
            key = (str(reg.organization_ms_id or ""), str(reg.warehouse_ms_id or ""))
            combos.setdefault(key, reg)
        if not combos:
            self._skip("Kassalar", "faol kassa yo'q — hujjat sinovi o'tkazib yuborildi")
            return

        for (org, wh), reg in combos.items():
            if not org or not wh:
                self._fail(
                    f"{reg.name}: sozlama",
                    "tashkilot yoki ombor tanlanmagan — bu kassaning cheklari "
                    "MoySklad'ga yozilmaydi (Panel → Kassalar → Sozlash)",
                )
                continue
            self._round_trip(reg, methods)

    # -- bitta kassa uchun: Отгрузка → to'lovlar → Возврат → qaytarishlar → o'chirish
    def _round_trip(self, reg: Register, methods: list[PaymentMethod]) -> None:
        product = _pick_product(reg)
        if product is None:
            self._skip(f"{reg.name}: hujjatlar", "katalogda tovar yo'q — sinov o'tkazib yuborildi")
            return

        created: list[tuple[str, str]] = []  # (entity, id) — o'chirish tartibida teskari
        label = reg.name
        names = ", ".join(m.name for m in methods)

        sale = _fake_sale(reg, product, methods)
        # Отгрузка
        try:
            demand = self.writer._write_demand(sale)
            self.writer._check_sum(sale, demand)
            created.append(("demand", demand["id"]))
            self._ok(f"{label}: Отгрузка", f"tovar «{product.name}»"[:120])
        except WriteError as e:
            self._fail(f"{label}: Отгрузка", str(e))
            self._delete_all(created, label)
            return

        # Kirim — har to'lov turi bilan
        bad = []
        for payment in sale.payments.all():
            try:
                self.writer._write_payment(sale, payment, demand)
                created.append(("cashin" if payment.method.is_cash else "paymentin", payment.ms_payment_id))
            except WriteError as e:
                bad.append(f"{payment.method.name}: {e}")
        if bad:
            self._fail(f"{label}: kirim ({names})", " | ".join(bad))
        else:
            self._ok(f"{label}: kirim ({names})")

        # Возврат — asl Отгрузка'ga bog'langan
        ret = _fake_sale(reg, product, methods, kind=Sale.RETURN,
                         origin=_Obj(id=1, ms_demand_id=demand["id"]))
        try:
            salesreturn = self.writer._write_salesreturn(ret)
            self.writer._check_sum(ret, salesreturn)
            created.append(("salesreturn", salesreturn["id"]))
            self._ok(f"{label}: Возврат")
        except WriteError as e:
            self._fail(f"{label}: Возврат", str(e))
            self._delete_all(created, label)
            return

        # Chiqim — har to'lov turi bilan (2026-09 dagi xato aynan shu yerda edi)
        bad = []
        for payment in ret.payments.all():
            try:
                self.writer._write_refund(ret, payment, salesreturn)
                created.append(("cashout" if payment.method.is_cash else "paymentout", payment.ms_payment_id))
            except WriteError as e:
                bad.append(f"{payment.method.name}: {e}")
        if bad:
            self._fail(f"{label}: pul qaytarish ({names})", " | ".join(bad))
        else:
            self._ok(f"{label}: pul qaytarish ({names})")

        self._delete_all(created, label)

    # -- o'chirish
    def _delete_all(self, created: list[tuple[str, str]], label: str) -> None:
        if not created:
            return
        failed = []
        # Bog'langanlar avval (to'lovlar), keyin hujjatlar
        for entity, ms_id in reversed(created):
            if not ms_id:
                continue
            try:
                self.client.delete(f"entity/{entity}/{ms_id}")
            except Exception as e:
                failed.append({"entity": entity, "id": str(ms_id), "error": str(e)[:200]})
        if failed:
            self.leftovers.extend(failed)
            self._fail(
                f"{label}: sinov hujjatlarini o'chirish",
                f"{len(failed)} ta hujjat o'chmadi (keyingi sinovda yana uriniladi): "
                + ", ".join(f"{f['entity']} {f['id'][:8]}" for f in failed),
            )
        else:
            self._ok(f"{label}: sinov hujjatlari o'chirildi", f"{len(created)} ta")

    def _cleanup_old_leftovers(self) -> None:
        old: dict[str, dict] = {}
        for check in MoySkladCheck.objects.exclude(leftovers=[]).order_by("-started_at")[:KEEP_RUNS]:
            for item in check.leftovers or []:
                if item.get("id"):
                    old[item["id"]] = item
        if not old:
            return
        still = []
        for item in old.values():
            try:
                self.client.delete(f"entity/{item['entity']}/{item['id']}")
            except MoySkladError as e:
                if e.status == 404:
                    continue  # allaqachon yo'q — yaxshi
                still.append({**item, "error": str(e)[:200]})
            except Exception as e:
                still.append({**item, "error": str(e)[:200]})
        # Tozalanganlarni eski yozuvlardan ham olib tashlaymiz
        MoySkladCheck.objects.exclude(leftovers=[]).update(leftovers=[])
        if still:
            self.leftovers.extend(still)
            self._fail(
                "Eski sinov hujjatlari",
                f"{len(still)} ta hali o'chmadi — MoySklad'da «SINOV-» deb qidirib, "
                "qo'lda o'chirish mumkin",
            )
        else:
            self._ok("Eski sinov hujjatlari", f"{len(old)} ta tozalandi")


def _pick_product(reg: Register) -> Product | None:
    """Sinov uchun tovar: shu omborda qoldig'i bor bo'lsa — o'sha, bo'lmasa har qanday."""
    wh = reg.warehouse_ms_id
    if wh:
        stock = (
            Stock.objects.filter(store_ms_id=wh, quantity__gt=0, product__archived=False)
            .select_related("product").order_by("-quantity").first()
        )
        if stock:
            return stock.product
    return Product.objects.filter(archived=False).order_by("pk").first()


def _prune() -> None:
    keep = MoySkladCheck.objects.order_by("-started_at").values_list("pk", flat=True)[:KEEP_RUNS]
    MoySkladCheck.objects.exclude(pk__in=list(keep)).delete()


# --------------------------------------------------------------- kirish nuqtasi


def run_selftest(trigger: str = MoySkladCheck.MANUAL, client: MoySkladClient | None = None) -> MoySkladCheck:
    """Sinovni yurgizadi va natijani qaytaradi. Bir vaqtda faqat bittasi."""
    if not _lock.acquire(blocking=False):
        raise SelfTestBusy("Sinov allaqachon ketmoqda")
    try:
        token = getattr(settings, "MOYSKLAD_TOKEN", "")
        if client is None and not token:
            check = MoySkladCheck.objects.create(trigger=trigger)
            check.steps = [{"name": "MoySklad ulanish", "ok": False,
                            "detail": "MOYSKLAD_TOKEN sozlanmagan"}]
            check.finished_at = timezone.now()
            check.save()
            return check
        client = client or MoySkladClient(token=token)
        return SelfTest(client, trigger).run()
    finally:
        _lock.release()


def is_due(now=None, every: timedelta = PERIODIC_EVERY) -> bool:
    """Navbatdagi vaqti-vaqti bilan sinov vaqti keldimi."""
    now = now or timezone.now()
    last = MoySkladCheck.latest()
    if last is None:
        return True
    if last.finished_at is None:
        # Tugallanmagan yozuv (jarayon o'lgan bo'lsa) — 10 daqiqadan keyin qayta
        return now - last.started_at > timedelta(minutes=10)
    return now - last.started_at >= every
