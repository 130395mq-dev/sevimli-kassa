"""Tovarning oxirgi kirimi (MoySklad priyomkasi) — zakazni kimga berish kerak.

Egasi (2026-11-12): «tugayotgan tovarlarni kim kimga zakaz berishi kerak —
oxirgi marta kim tomonidan kirim qilingan, shuni olib ko'rsatsin».

MoySklad'dan FAQAT O'QILADI (rasmiy JSON API 1.2 hujjati bo'yicha). Hamma
so'rov bitta ko'rinishda — priyomkalar ro'yxati, qatorlari bilan birga:

    entity/supply?filter=…&order=…&limit=…&expand=agent,store,positions

(«Expand разрешен только на размере выборки не более 100»; qatorlar ichida
kelmasa yoki to'liq bo'lmasa — entity/supply/{id}/positions.)

So'rovlar SONI muhim: MoySklad limiti bitta foydalanuvchiga 3 soniyada 15 ta
so'rov (2026-12-01 dan 11 ta) va shu limitdan kassalar cheklari ham
foydalanadi. Shuning uchun tovarma-tovar emas, hujjatlar sahifalab o'qiladi:

  1. TARIX — bir marta: oxirgi 120 kunlik priyomkalar, eng yangisidan orqaga
     qarab (order=moment,desc). Tovar birinchi uchragan hujjat — uning oxirgi
     kirimi. 50 ta hujjat = 1 ta so'rov.
  2. YANGI — 15 daqiqada bir: o'zgargan priyomkalar (filter=updated>=…).
     Tovar yana kelsa yoki priyomka tuzatilsa — ma'lumot o'zi yangilanadi.
  3. RO'YXAT — tarixda topilmagan (120 kundan eski) yoki qayta aniqlanishi
     kerak bo'lgan tovarlar bittalab:
         filter=assortment=<tovar href>;applicable=true&order=moment,desc&limit=1
     («filter=assortment=<href> позволяет фильтровать документы по наличию
     позиций с указанными сущностями ассортимента»). Faqat «Tugayotganlar»
     ro'yxatidagi tovarlar uchun, so'rovlar orasida tanaffus bilan. Avval bu
     filtr priyomkasi ANIQ ma'lum tovarda sinab ko'riladi: kutilgandek
     ishlamasa, bosqich ishlamaydi — noto'g'ri «topilmadi» yozilmaydi.

Hammasi qoldiq sinxronizatsiyasi navbatidan keyin (cron, 5 daqiqada bir).
Xavfsizlik: hech qachon xato tashlamaydi; o'z vaqt chegarasi va qulfi bor;
«Tugayotganlar» sahifasi ishlatilmayotgan bo'lsa MoySklad'ga so'rov yubormaydi.

O'zi moslashadi (qayta chiqarishsiz): sahifa og'irlik qilib javob kelmasa —
keyingi safar ikki barobar kichik sahifa so'raladi; MoySklad qatorlarni
ichida berishni (expand=positions) qabul qilmasa — qatorlar har hujjat uchun
alohida so'raladi (sekinroq, lekin ishlaydi).
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.db import OperationalError, ProgrammingError, transaction
from django.db.models import Q
from django.utils import timezone

from catalog.models import Product, SyncState
from config.work_locks import work_lock
from kpi import ms
from moysklad.client import MoySkladClient, MoySkladError

from .models import OxirgiKirim

logger = logging.getLogger(__name__)

BUDGET_SECONDS = 40.0        # bitta navbatda MoySklad'ga sarflanadigan vaqt
SAHIFA = 50                  # bitta so'rovda nechta priyomka (expand — ko'pi bilan 100)
SAHIFA_MIN = 5               # og'irlik qilsa — shungacha kichrayadi
SAHIFA_QATORSIZ = 20         # qatorlar alohida so'ralganda (har hujjat — yana bitta so'rov)
MOSLASHUV_KESH = 6 * 3600    # moslashgan sozlama necha soniya eslab qolinadi
TARIX_KUN = 120              # tarix necha kun orqaga o'qiladi
YANGI_DAQIQA = 15            # o'zgargan priyomkalar necha daqiqada bir so'raladi
OVERLAP = timedelta(minutes=10)
QAYTA_KUN = 7                # priyomkasi topilmagan tovar shuncha kunda bir qayta so'raladi
TEKSHIR_KUN = 14             # topilgani shuncha kunda bir qayta tekshiriladi (o'chirilgan hujjat va h.k.)
BIR_NAVBATDA = 30            # bitta navbatda bittalab so'raladigan tovarlar (ko'pi bilan)
ORALIQ = 0.3                 # bittalab so'rovlar orasidagi tanaffus, soniya (limitni kassalarga qoldirish)
XATO_CHEGARA = 5             # bitta navbatda shuncha tovarda xato — to'xtash
ROYXAT_KESH = 30 * 60        # ro'yxatdagi tovarlar necha soniya eslab qolinadi

TARIX, YANGI, ROYXAT = "kirim:tarix", "kirim:yangi", "kirim:royxat"
DAVOMI = "davomi keyingi navbatda"
EXPAND, EXPAND_QATORSIZ = "agent,store,positions", "agent,store"
K_SAHIFA, K_EXPAND = "kirim:sahifa", "kirim:expand"

KUTILMOQDA, YOQ, BOR = "kutilmoqda", "yoq", "bor"

MAYDONLAR = ["topildi", "agent_ms_id", "agent_name", "store_ms_id", "store_name", "moment",
             "quantity", "price", "doc_ms_id", "doc_number", "checked_at"]


class FiltrXato(Exception):
    """`filter=assortment=` kutilgandek ishlamadi — natijaga ishonib bo'lmaydi."""


class Budget:
    """Vaqt chegarasi: fon ishi cron'ni uzoq ushlab turmasin."""

    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds

    def left(self) -> bool:
        return time.monotonic() < self.end


def _client():
    token = getattr(settings, "MOYSKLAD_TOKEN", "")
    if not token:
        return None
    # Qatorlari bilan kelgan sahifa og'ir — javobni uzoqroq kutamiz; uzilsa
    # qayta so'ramaymiz (keyingi navbat o'sha joydan davom etadi).
    return MoySkladClient(token=token, timeout=45, network_retries=0, max_retries=3,
                          user_agent="SevimliKassa-Kirim/0.1")


def _state(entity: str) -> SyncState:
    st, _ = SyncState.objects.get_or_create(entity=entity)
    return st


def _mark(entity: str, now, error: str = "", rows: int = 0) -> None:
    st = _state(entity)
    st.last_run_at = now
    if error:
        st.last_error = error[:2000]
    else:
        st.last_success_at = now
        st.last_error = ""
        st.rows_synced = rows
    st.save()


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _uuid(value) -> str | None:
    """To'g'ri UUID yoki None (bazaga so'rov xato bermasligi uchun)."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def _nom(obj) -> str:
    return str(obj.get("name") or "") if isinstance(obj, dict) else ""


def _agent_nomi(agent) -> str:
    """Postavshik nomi: `expand` bilan kelgani; kelmagan bo'lsa — bazadagi kontragentdan."""
    name = _nom(agent)
    mid = _uuid(ms.ms_id(agent)) if not name else None
    if mid:
        from catalog.models import Customer

        name = Customer.objects.filter(ms_id=mid).values_list("name", flat=True).first() or ""
    return name[:255]


def _ombor_nomi(store) -> str:
    name = _nom(store)
    mid = _uuid(ms.ms_id(store)) if not name else None
    if mid:
        from catalog.models import Warehouse

        name = Warehouse.objects.filter(ms_id=mid).values_list("name", flat=True).first() or ""
    return name[:255]


# ------------------------------------------------------- priyomkani hisobga olish

def _kesh():
    from . import kochirish

    return kochirish._cache()


def joriy_expand() -> str:
    """Qatorlar ro'yxat ichida so'raladimi (odatda — ha)."""
    return EXPAND_QATORSIZ if _kesh().get(K_EXPAND) == EXPAND_QATORSIZ else EXPAND


class Sahifalar:
    """Priyomkalar ro'yxati sahifama-sahifa (qatorlari bilan). Olingan sahifa
    to'liq qayta ishlanadi; vaqt tugasa — keyingisi so'ralmaydi (`tugadi`
    False qoladi, keyingi navbat davom etadi)."""

    def __init__(self, client, budget: Budget, **params):
        self.client, self.budget, self.params = client, budget, params
        self.sorovlar = 0            # nechta sahifa so'raldi
        self.tugadi = False          # ro'yxat oxirigacha o'qildi
        self.expand = joriy_expand()
        try:
            hajm = int(_kesh().get(K_SAHIFA) or SAHIFA)
        except (TypeError, ValueError):
            hajm = SAHIFA
        if self.expand == EXPAND_QATORSIZ:
            hajm = min(hajm, SAHIFA_QATORSIZ)
        self.hajm = min(SAHIFA, max(SAHIFA_MIN, hajm))

    def _kichraytir(self, sabab) -> None:
        """Sahifa og'irlik qildi — keyingi safar ikki barobar kichigi so'raladi."""
        yangi = max(SAHIFA_MIN, self.hajm // 2)
        if yangi < self.hajm:
            _kesh().set(K_SAHIFA, yangi, MOSLASHUV_KESH)
            logger.warning("Oxirgi kirim: %s ta hujjatli sahifa kelmadi (%s) — keyingi safar %s tadan",
                           self.hajm, sabab, yangi)

    def _sora(self, offset: int):
        def sorov():
            return self.client.get("entity/supply", limit=self.hajm, offset=offset, expand=self.expand,
                                   **self.params)
        try:
            return sorov()
        except (requests.Timeout, requests.exceptions.ChunkedEncodingError) as e:
            self._kichraytir(type(e).__name__)
            raise
        except MoySkladError as e:
            if e.status == 504:
                self._kichraytir("MoySklad 504")
            if e.is_retryable or e.is_auth_error or self.expand != EXPAND:
                raise
            # So'rov qabul qilinmadi. Sabab qatorlarni ichida so'rash bo'lsa —
            # ularsiz o'tadi: shunda eslab qolinadi va qatorlar alohida so'raladi.
            self.expand = EXPAND_QATORSIZ
            self.hajm = min(self.hajm, SAHIFA_QATORSIZ)
            payload = sorov()            # bu ham o'tmasa — sabab boshqa: xato yuqoriga chiqadi
            _kesh().set(K_EXPAND, EXPAND_QATORSIZ, MOSLASHUV_KESH)
            logger.warning("Oxirgi kirim: MoySklad qatorlarni ro'yxat ichida bermadi (%s) — "
                           "qatorlar har hujjat uchun alohida so'raladi", e)
            return payload

    def __iter__(self):
        offset = 0
        while self.budget.left():
            hajm = self.hajm
            payload = self._sora(offset)
            self.sorovlar += 1
            rows = (payload.get("rows") if isinstance(payload, dict) else None) or []
            if rows:
                yield rows
            offset += len(rows)
            if len(rows) < min(hajm, self.hajm):
                self.tugadi = True
                return


def _chet_valyutada(row: dict) -> bool:
    """Hujjat boshqa valyutadami. MoySklad kursni (`rate.value`) faqat 1 dan
    farq qilganda beradi; bunday hujjatda narxlar o'sha valyutada."""
    kurs = (row.get("rate") or {}).get("value") if isinstance(row.get("rate"), dict) else None
    return kurs is not None and _dec(kurs) != 1


def _qatorlar(client, row: dict) -> dict:
    """Priyomka qatorlari: {tovar_ms_id: (miqdor, narx tiyinda)}. Chet
    valyutadagi hujjatda narx yozilmaydi (None): uni so'm deb ko'rsatib
    qo'ymaslik uchun."""
    pos = row.get("positions")
    rows = pos.get("rows") if isinstance(pos, dict) else None
    size = (pos.get("meta") or {}).get("size") if isinstance(pos, dict) else None
    if rows is None or (isinstance(size, int) and size > len(rows)):
        # `expand` ishlamadi yoki qatorlar sig'madi — alohida, to'liq so'raladi
        rows = client.iter_list(f"entity/supply/{row['id']}/positions")
    chet = _chet_valyutada(row)
    out: dict = {}
    for p in rows:
        pid = ms.ms_id(p.get("assortment"))
        if not pid:
            continue
        qty, price = _dec(p.get("quantity")), None if chet else int(_dec(p.get("price")))
        out[pid] = (out[pid][0] + qty, out[pid][1]) if pid in out else (qty, price)
    return out


def _umumiy(row: dict, now) -> dict:
    """Priyomkaning undagi hamma tovar uchun bir xil maydonlari."""
    return {
        "topildi": True,
        "agent_ms_id": _uuid(ms.ms_id(row.get("agent"))), "agent_name": _agent_nomi(row.get("agent")),
        "store_ms_id": _uuid(ms.ms_id(row.get("store"))), "store_name": _ombor_nomi(row.get("store")),
        "moment": ms.parse(row.get("moment")),
        "doc_ms_id": row["id"], "doc_number": str(row.get("name") or "")[:255],
        "checked_at": now,
    }


def _hujjat(client, row: dict, now) -> int:
    """Bitta priyomkani hisobga oladi (tarix va yangilar uchun bir xil qoida).
    Qaytaradi: nechta tovarga yozildi.

    Yozuv «aniq» (`checked_at` to'ldirilgan) — faqat shu hujjat tovarning
    ANIQ oxirgi kirimi bo'lganda:
      * yozuvi yo'q yoki «priyomkasi topilmadi» — yoziladi;
      * boshqa, yangiroq sanali hujjat — yoziladi; eskirog'i — tegilmaydi;
      * o'sha hujjatning o'zi (tuzatilgan) — yangilanadi;
      * hujjat o'chirilgan / o'tkazilmagan, sanasi orqaga surilgan yoki tovar
        undan olib tashlangan — endi oxirgisi qaysi ekani noma'lum: yozuv
        «qayta aniqlash kerak» bo'ladi (`checked_at` bo'sh) va tovar bittalab
        so'raladi. Bunday yozuvni faqat avvalgisidan eski bo'lmagan hujjat
        qayta to'ldira oladi;
      * tarix oralig'idan (TARIX_KUN) eski hujjat tuzatilgan bo'lsa — undan
        keyingi, biz ko'rmagan priyomka bo'lishi mumkin: ma'lumot yoziladi,
        lekin «qayta aniqlash kerak» bo'lib qoladi (bittalab so'rov tasdiqlaydi).
    """
    doc_id = _uuid(row.get("id"))
    if not doc_id:
        return 0
    shu_hujjat = OxirgiKirim.objects.filter(doc_ms_id=doc_id)
    moment = ms.parse(row.get("moment"))
    if row.get("deleted") or not row.get("applicable", True) or moment is None:
        shu_hujjat.update(checked_at=None)
        return 0
    qatorlar = _qatorlar(client, row)
    tovarlar = {str(m): pk for m, pk in
                Product.objects.filter(ms_id__in=[u for u in map(_uuid, qatorlar) if u])
                .values_list("ms_id", "pk")}
    mavjud = {k.product_id: k for k in OxirgiKirim.objects.filter(product_id__in=tovarlar.values())}
    umumiy = _umumiy({**row, "id": doc_id}, now)
    # Tarix oralig'idagi hujjatdan yangiroq hamma priyomka ko'rilgan — u aniq
    # oxirgisi. Undan eski hujjat faqat ma'lum oxirgisidan yangi bo'lsa aniq.
    oraliqda = now if moment >= now - timedelta(days=TARIX_KUN) else None
    yangi, ozgargan, eskirgan = [], [], []
    for ms_uid, pk in tovarlar.items():
        qty, price = qatorlar[ms_uid]
        k = mavjud.get(pk)
        if k is None:
            yangi.append(OxirgiKirim(product_id=pk, quantity=qty, price=price,
                                     **{**umumiy, "checked_at": oraliqda}))
            continue
        belgi = oraliqda
        if k.checked_at is None:
            if k.moment is not None and moment < k.moment:
                continue                 # qayta aniqlanmoqda — eskiroq hujjatga ishonilmaydi
        elif k.topildi and k.moment is not None:
            if str(k.doc_ms_id) == doc_id:
                if moment < k.moment:
                    eskirgan.append(k.pk)    # sanasi orqaga surilgan — orada boshqa priyomka bo'lishi mumkin
                    continue
            elif moment <= k.moment:
                continue                 # bundan yangirog'i allaqachon yozilgan
            belgi = now                  # ma'lum oxirgisining o'zi yoki undan yangirog'i — aniq
        for field, value in umumiy.items():
            setattr(k, field, value)
        k.quantity, k.price, k.checked_at = qty, price, belgi
        ozgargan.append(k)
    with transaction.atomic():
        if yangi:
            OxirgiKirim.objects.bulk_create(yangi, batch_size=500)
        if ozgargan:
            OxirgiKirim.objects.bulk_update(ozgargan, MAYDONLAR, batch_size=200)
        if eskirgan:
            OxirgiKirim.objects.filter(pk__in=eskirgan).update(checked_at=None)
        # Shu hujjatga ishora qilgan, lekin endi unda yo'q tovarlar — qayta aniqlanadi
        shu_hujjat.exclude(product_id__in=list(tovarlar.values())).update(checked_at=None)
    return len(yangi) + len(ozgargan)


# ------------------------------------------------------------- 1) tarix

def tarix_tugadimi() -> bool:
    st = SyncState.objects.filter(entity=TARIX).first()
    return bool(st and st.last_success_at)


def tarixni_ol(client, budget: Budget, now) -> int:
    """Oxirgi TARIX_KUN kunlik priyomkalar — eng yangisidan orqaga qarab.
    Bir necha navbatga bo'linadi: qayerga yetgani `cursor` da (hujjat sanasi).
    Qaytaradi: nechta hujjat o'qildi."""
    st = _state(TARIX)
    shartlar = [f"moment>={ms.fmt(now - timedelta(days=TARIX_KUN))}"]
    if st.cursor is not None:
        # +1 soniya: filtr soniyagacha aniq, shu soniyadagi hujjatlar qolib ketmasin
        shartlar.append(f"moment<={ms.fmt(st.cursor + timedelta(seconds=1))}")
    sahifalar = Sahifalar(client, budget, filter=";".join(shartlar), order="moment,desc")
    n = 0
    for rows in sahifalar:
        for row in rows:
            _hujjat(client, row, now)
        n += len(rows)
        st.cursor = ms.parse(rows[-1].get("moment")) or st.cursor
        st.last_run_at, st.last_error, st.rows_synced = now, DAVOMI, (st.rows_synced or 0) + len(rows)
        st.save()                        # har sahifadan keyin: uzilsa — shu joydan davom etadi
    if sahifalar.tugadi:
        st.last_run_at, st.last_success_at, st.last_error = now, now, ""
        st.save()
    return n


# ----------------------------------------------------- 2) o'zgargan priyomkalar

def yangilarni_ol(client, budget: Budget, now) -> int:
    """Oxirgi tekshiruvdan beri yaratilgan/o'zgargan priyomkalar. Qaytaradi: hujjatlar soni."""
    st = _state(YANGI)
    if st.cursor is None:
        # Birinchi navbat: shu paytgacha bo'lganini tarix o'qiydi — kuzatuv hozirdan boshlanadi
        st.cursor, st.last_run_at, st.last_success_at, st.last_error, st.rows_synced = now, now, now, "", 0
        st.save()
        return 0
    # To'liq o'qilgan navbatdan keyin — ustma-ust 10 daqiqa (soat farqi uchun);
    # yarim yo'lda to'xtagan bo'lsa — aynan to'xtagan hujjatdan davom etadi.
    since = st.cursor if st.last_error == DAVOMI else st.cursor - OVERLAP
    since = max(since, now - timedelta(days=TARIX_KUN))
    sahifalar = Sahifalar(client, budget, filter=f"updated>={ms.fmt(since)}", order="updated,asc")
    n = 0
    for rows in sahifalar:
        for row in rows:
            _hujjat(client, row, now)
        n += len(rows)
        st.cursor = ms.parse(rows[-1].get("updated")) or st.cursor
        st.last_run_at, st.last_error, st.rows_synced = now, DAVOMI, n
        st.save()
    if sahifalar.tugadi:
        st.cursor, st.last_run_at, st.last_success_at, st.last_error, st.rows_synced = now, now, now, "", n
        st.save()
    return n


# ------------------------------------------- 3) ro'yxatdagi tovarlar bittalab

def royxatdagi_tovarlar() -> list[int]:
    """«Tugayotganlar» ro'yxatidagi tovarlar — ro'yxat tartibida, takrorsiz.
    Ro'yxat sahifa ochilganda hisoblanib, umumiy keshda 7 kun turadi. Katta
    natijani har navbatda o'qimaslik uchun tovarlar ro'yxati alohida, kichik
    yozuvda 30 daqiqa eslab qolinadi."""
    from . import kochirish

    cache = kochirish._cache()
    kalit = f"{kochirish.CACHE_KEY}:kirim-tovarlar"
    saved = cache.get(kalit)
    if saved is not None:
        return saved
    data = cache.get(f"{kochirish.CACHE_KEY}:stale") or {}
    out, seen = [], set()
    for r in data.get("short") or []:
        pid = r.get("product_id")
        if pid and pid not in seen:
            seen.add(pid)
            out.append(pid)
    cache.set(kalit, out, ROYXAT_KESH)
    return out


def tovar_href(client, product: Product) -> str:
    return f"{client.base_url}/entity/{product.kind}/{product.ms_id}"


def oxirgi_priyomka(client, product: Product):
    """Tovarning oxirgi o'tkazilgan priyomkasi: (hujjat, (miqdor, narx)) yoki
    None (priyomkasi yo'q). Qaytgan hujjat qatorlarida shu tovarning o'zi
    bo'lmasa — FiltrXato: begona priyomkaning postavshigi tovarga yozilmasin."""
    payload = client.get(
        "entity/supply", filter=f"assortment={tovar_href(client, product)};applicable=true",
        order="moment,desc", limit=1, expand=joriy_expand(),
    )
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not rows:
        return None
    row = rows[0]
    qator = _qatorlar(client, row).get(str(product.ms_id))
    if qator is None or not _uuid(row.get("id")):
        raise FiltrXato(f"«{product.name}» uchun qaytgan priyomka {row.get('name')} da bu tovar yo'q")
    return row, qator


def filtrni_sina(client) -> None:
    """Bittalab so'rashdan oldin filtrni priyomkasi ANIQ ma'lum tovarda sinaydi
    (tarixdan eng oxirgi kelgan tovar). So'rov o'sha yoki undan yangiroq
    priyomkani qaytarishi kerak; aks holda — FiltrXato."""
    k = (OxirgiKirim.objects.filter(topildi=True, checked_at__isnull=False, moment__isnull=False)
         .select_related("product").order_by("-moment", "pk").first())
    if k is None:
        raise FiltrXato(f"oxirgi {TARIX_KUN} kunda birorta priyomka topilmadi — so'rovni sinab bo'lmadi")
    topilgan = oxirgi_priyomka(client, k.product)
    moment = ms.parse(topilgan[0].get("moment")) if topilgan else None
    if moment is None or moment < k.moment:
        raise FiltrXato(f"«{k.product.name}» ning {timezone.localtime(k.moment):%d.%m.%Y} dagi priyomkasi "
                        f"bor, lekin so'rov uni qaytarmadi")


def _yoz(product_id: int, row: dict, qator, now) -> None:
    """Bittalab so'rov natijasi — MoySklad'ning o'z javobi: shartsiz yoziladi."""
    OxirgiKirim.objects.update_or_create(product_id=product_id, defaults={
        **_umumiy({**row, "id": _uuid(row.get("id"))}, now), "quantity": qator[0], "price": qator[1]})


def _yoq(product_id: int, now) -> None:
    """So'raldi — o'tkazilgan priyomkasi yo'q."""
    OxirgiKirim.objects.update_or_create(product_id=product_id, defaults={
        "topildi": False, "agent_ms_id": None, "agent_name": "", "store_ms_id": None, "store_name": "",
        "moment": None, "quantity": None, "price": None, "doc_ms_id": None, "doc_number": "",
        "checked_at": now,
    })


def _navbat(pids: list[int], now) -> list[int]:
    """Kimni so'rash kerak — muhimligi bo'yicha: hali aniqlanmaganlar, keyin
    eskirgan «topilmadi»lar, keyin tekshiruv muddati kelganlar (har guruh
    ro'yxat tartibida)."""
    mavjud = {k.product_id: k for k in OxirgiKirim.objects.filter(product_id__in=pids)}
    yangi, yoq, eski = [], [], []
    for pid in pids:
        k = mavjud.get(pid)
        if k is None or k.checked_at is None:
            yangi.append(pid)
        elif not k.topildi:
            if now - k.checked_at >= timedelta(days=QAYTA_KUN):
                yoq.append(pid)
        elif now - k.checked_at >= timedelta(days=TEKSHIR_KUN):
            eski.append(pid)
    return (yangi + yoq + eski)[:BIR_NAVBATDA]


def _tanaffus() -> None:
    if ORALIQ > 0:
        time.sleep(ORALIQ)


def royxatni_toldir(client, budget: Budget, now, pids: list[int] | None = None) -> int:
    """Ro'yxatdagi, tarixda topilmagan yoki qayta aniqlanishi kerak bo'lgan
    tovarlarni bittalab so'raydi. Qaytaradi: nechta tovar aniqlandi."""
    pids = royxatdagi_tovarlar() if pids is None else pids
    kerak = _navbat(pids, now) if pids else []
    if not kerak:
        return 0
    st = _state(ROYXAT)
    if st.cursor is None:                # filtr hali sinalmagan (cursor — sinovdan o'tgan vaqt)
        filtrni_sina(client)             # o'tmasa — FiltrXato: keyingi navbatda yana sinaladi (1 ta so'rov)
        st.cursor = now
        st.save(update_fields=["cursor"])
        _tanaffus()
    tovarlar = Product.objects.in_bulk(kerak)
    n = xato = 0
    for pid in kerak:
        if not budget.left() or xato >= XATO_CHEGARA:
            break
        p = tovarlar.get(pid)
        if p is None:
            continue                     # tovar bazadan o'chgan
        if p.kind in (Product.KIND_SERVICE, Product.KIND_BUNDLE):
            _yoq(pid, now)               # xizmat va komplekt priyomka qilinmaydi
            n += 1
            continue
        if n:
            _tanaffus()
        try:
            topilgan = oxirgi_priyomka(client, p)
        except (MoySkladError, FiltrXato) as e:
            if isinstance(e, MoySkladError) and (e.is_retryable or e.is_auth_error):
                raise                    # MoySklad ishlamayapti — hech narsa belgilanmaydi
            xato += 1
            logger.warning("Oxirgi kirim: «%s» so'ralmadi: %s", p.name, e)
            # Aniq ma'lumoti bor tovar (muddatli tekshiruv) — ma'lumot saqlanadi,
            # tekshiruv keyingi muddatga qoladi; yo'q bo'lsa — «topilmadi»
            if not OxirgiKirim.objects.filter(product_id=pid, topildi=True, checked_at__isnull=False
                                              ).update(checked_at=now):
                _yoq(pid, now)
        else:
            if topilgan is None:
                _yoq(pid, now)
            else:
                _yoz(pid, topilgan[0], topilgan[1], now)
        n += 1
    return n


# ---------------------------------------------------------------- fon ishi

def _due(entity: str, minutes: int, now) -> bool:
    st = SyncState.objects.filter(entity=entity).first()
    if st is None or st.last_success_at is None or st.last_error == DAVOMI:
        return True
    return now - st.last_success_at >= timedelta(minutes=minutes)


def run(client=None, now=None, budget_seconds: float = BUDGET_SECONDS) -> dict:
    now = now or timezone.now()
    pids = royxatdagi_tovarlar()
    if not pids:
        return {}                        # sahifa ishlatilmayapti — MoySklad'ga tegmaymiz
    out: dict = {}
    with work_lock("kirim:run") as acquired:
        if not acquired:
            return {}
        client = client or _client()
        if client is None:
            return {}
        budget = Budget(budget_seconds)
        ishlar = []
        if _due(YANGI, YANGI_DAQIQA, now):
            ishlar.append((YANGI, lambda: yangilarni_ol(client, budget, now)))
        if tarix_tugadimi():
            ishlar.append((ROYXAT, lambda: royxatni_toldir(client, budget, now, pids)))
        else:
            ishlar.append((TARIX, lambda: tarixni_ol(client, budget, now)))
        for nom, ish in ishlar:
            try:
                out[nom] = ish()
            except (MoySkladError, requests.RequestException, FiltrXato) as e:
                logger.warning("Oxirgi kirim: %s bajarilmadi: %s: %s", nom, type(e).__name__, e)
                _mark(nom, now, error=f"{type(e).__name__}: {e}")
            else:
                if nom == ROYXAT:
                    _mark(ROYXAT, now, rows=out[nom])
    if any(out.values()):
        logger.info("Oxirgi kirim: tarix %s, yangi %s ta priyomka; bittalab %s ta tovar",
                    out.get(TARIX, 0), out.get(YANGI, 0), out.get(ROYXAT, 0))
    return out


def after_stock_sync() -> None:
    """Qoldiq sinxronizatsiyasi navbatidan keyin. HECH QACHON xato tashlamaydi."""
    try:
        run()
    except (ProgrammingError, OperationalError) as e:
        # Deploy payti: migratsiya hali o'tmagan bo'lishi mumkin — keyingi navbatda o'zi tuzaladi
        logger.warning("Oxirgi kirim o'tkazib yuborildi (baza tayyor emas): %s", type(e).__name__)
    except Exception:
        logger.exception("Oxirgi kirim fon ishi bajarilmadi (qoldiq sinxronizatsiyasiga ta'siri yo'q)")


# ------------------------------------------------------------------ sahifa

def attach(rows: list[dict]) -> tuple[list[dict], dict]:
    """«Tugayotganlar» qatorlariga oxirgi kirimni qo'shadi (keshdagi asl
    qatorlarga tegmaydi — nusxa qaytaradi). Ikkinchi qiymat — nechta tovar
    aniqlangani: {"jami", "aniq"}."""
    pids = {r["product_id"] for r in rows}
    mavjud = {k.product_id: k for k in OxirgiKirim.objects.filter(product_id__in=pids)}
    out = []
    for r in rows:
        k = mavjud.get(r["product_id"])
        if k is None or k.checked_at is None:
            kirim = {"holat": KUTILMOQDA, "agent_id": KUTILMOQDA}
        elif not k.topildi:
            kirim = {"holat": YOQ, "agent_id": YOQ}
        else:
            kirim = {
                "holat": BOR,
                "agent_id": str(k.agent_ms_id) if k.agent_ms_id else YOQ,
                "agent": k.agent_name or "nomi yozilmagan postavshik",
                "sana": k.moment, "miqdor": k.quantity, "narx": k.price,
                "ombor": k.store_name, "hujjat": k.doc_number,
                "boshqa_ombor": bool(k.store_ms_id) and str(k.store_ms_id) != str(r["wh_id"]),
            }
        out.append({**r, "kirim": kirim})
    aniq = sum(1 for pid in pids if pid in mavjud and mavjud[pid].checked_at is not None)
    return out, {"jami": len(pids), "aniq": aniq}


def postavshiklar(rows: list[dict]) -> list[dict]:
    """Ro'yxatdagi postavshiklar — tovari ko'pi birinchi (filtr uchun)."""
    soni: dict = {}
    for r in rows:
        k = r.get("kirim") or {}
        if k.get("holat") == BOR and k["agent_id"] != YOQ:
            item = soni.setdefault(k["agent_id"], {"id": k["agent_id"], "name": k["agent"], "soni": 0})
            item["soni"] += 1
    return sorted(soni.values(), key=lambda x: (-x["soni"], x["name"].casefold()))


def filtr(rows: list[dict], postavshik: str) -> list[dict]:
    """?postavshik=<id> — shu postavshikdan kelgan tovarlar; «yoq» — priyomkasi
    topilmaganlar; «kutilmoqda» — hali aniqlanmaganlar."""
    if not postavshik:
        return rows
    return [r for r in rows if (r.get("kirim") or {}).get("agent_id") == postavshik]


def masullar(today=None) -> dict[str, str]:
    """{ombor_ms_id: tovaroved ismi} — do'konga bugun biriktirilgan tovaroved
    (zakazni u beradi); bir nechta bo'lsa — hammasi, vergul bilan.
    Biriktirish — «Tovarovedlar · Bog'lash» sahifasida."""
    from kpi.models import Biriktirish

    today = today or timezone.localdate()
    ismlar: dict[str, list[str]] = {}
    qs = (Biriktirish.objects.filter(boshlanish__lte=today, tovaroved__active=True)
          .filter(Q(tugash__isnull=True) | Q(tugash__gte=today))
          .select_related("tovaroved").order_by("boshlanish", "pk"))
    for b in qs:
        royxat = ismlar.setdefault(str(b.warehouse_ms_id), [])
        if b.tovaroved.name not in royxat:
            royxat.append(b.tovaroved.name)
    return {wh: ", ".join(royxat) for wh, royxat in ismlar.items()}


def holat() -> dict:
    """Fon ishining holati (sahifada egasiga ko'rsatish uchun): oxirgi xato va
    tarix to'liq o'qilganmi."""
    st = {s.entity: s for s in SyncState.objects.filter(entity__in=[TARIX, YANGI, ROYXAT])}
    xato = ""
    for entity in (TARIX, ROYXAT, YANGI):
        s = st.get(entity)
        if s and s.last_error and s.last_error != DAVOMI:
            xato = s.last_error
            break
    return {"xato": xato, "tarix": bool(st.get(TARIX) and st[TARIX].last_success_at)}
