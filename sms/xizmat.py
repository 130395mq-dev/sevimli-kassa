"""SMS qoidalari va yuborish (nega shunday — sms/models.py boshida).

Qoidalar:

  1. SMS faqat rozi bo'lgan mijozga ketadi (`Customer.sms_rozi is True`).
     Rozilik kassada yoki panelda yoziladi, kim va qachon yozgani saqlanadi.
  2. SMS faqat kunduzi ketadi: sozlamadagi soatdan 17:45 gacha
     (qonun: 18:00–09:00 oralig'ida reklama SMS mumkin emas).
  3. Bir mijozga bir yilda bitta tabrik; bitta telefonga bir kunda bitta.
  4. Matn SMS xizmatida TASDIQLANGAN shablon bo'lishi shart: tasdiqlanmagan
     matn bloklanadi yoki bir necha baravar qimmat hisoblanadi. Xizmat
     shablonlar ro'yxatini bersa — tekshiramiz, topilmasa yubormaymiz.
  5. Javobi kelmagan SMS avtomatik qayta yuborilmaydi (ikki marta ketmasin).
"""

from __future__ import annotations

import calendar
import hashlib
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal

import requests
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from catalog.models import Customer

from . import provayder as prov
from .models import SmsSozlama, SmsXabar

logger = logging.getLogger(__name__)

BOSHLANISH_SOAT = 9                  # 09:00 dan oldin yuborilmaydi
TUGASH_DAQIQA = 17 * 60 + 45         # 17:45 — 18:00 dan oldin zaxira bilan to'xtaydi
BIR_NAVBATDA = 20                    # bitta fon ishida eng ko'p SMS
VAQT_BUDJETI = 40.0                  # bitta fon ishi SMS'ga sarflaydigan vaqt, soniya
QAYTA_URINISH = 3                    # xizmat qabul qilmasa — jami necha marta urinish
KETMA_KET_XATO = 3                   # bitta fon ishida ketma-ket shuncha xato — to'xtash
SINOV_SOATIGA = 10                   # bir soatda eng ko'p sinov SMS
UZILGAN_DAQIQA = 10                  # «navbatda» shuncha tursa — jarayon uzilgan

T = SmsXabar.TUGILGAN_KUN


class SmsXato(Exception):
    """Egasiga ko'rsatiladigan xato matni."""


# ------------------------------------------------------------------ matn

_GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_QOSHIMCHA = set("^{}\\[~]|€")          # har biri 2 belgi o'rnini oladi


@dataclass(frozen=True)
class Kod:
    """Matn nechta SMS bo'lib ketadi."""

    unicode: bool        # oddiy (lotin) belgilardan tashqari belgi bor
    belgi: int           # SMS hisobidagi belgilar soni
    qism: int            # nechta SMS
    chegara: int         # bitta SMS'ga sig'adigan belgi (160 yoki 70)


def kodlash(matn: str) -> Kod:
    """Oddiy lotin matni — 160 belgigacha 1 ta SMS (keyin 153 tadan).
    Kirill, «ʻ», «’» kabi belgi bo'lsa — 70 belgigacha 1 ta SMS (keyin 67 tadan)."""
    matn = matn or ""
    if all(ch in _GSM7 or ch in _GSM7_QOSHIMCHA for ch in matn):
        n = sum(2 if ch in _GSM7_QOSHIMCHA else 1 for ch in matn)
        return Kod(False, n, 1 if n <= 160 else math.ceil(n / 153), 160)
    n = len(matn.encode("utf-16-le")) // 2
    return Kod(True, n, 1 if n <= 70 else math.ceil(n / 67), 70)


def _norm(matn: str) -> str:
    return " ".join((matn or "").split())


# --------------------------------------------------------------- telefon

def telefon_998(raw: str) -> str:
    """Har xil yozilgan raqamni 998XXXXXXXXX ko'rinishiga keltiradi;
    tushunib bo'lmasa — «». «+998 (90) 123-45-67», «901234567» — bo'ladi.
    Bitta maydonda vergul yoki «/» bilan bir nechta raqam yozilgan bo'lsa —
    birinchi to'g'risi olinadi."""
    for qism in re.split(r"[,;/\n]+", raw or ""):
        digits = "".join(ch for ch in qism if ch.isdigit())
        if len(digits) == 9:
            digits = "998" + digits
        if len(digits) == 12 and digits.startswith("998") and digits[3] != "0":
            return digits
    return ""


# ------------------------------------------------------------------ vaqt

def vaqt_mumkin(local: datetime, soat: int) -> bool:
    """Hozir SMS yuborsa bo'ladimi: sozlamadagi soatdan 17:45 gacha."""
    boshi = max(BOSHLANISH_SOAT, min(int(soat), SmsSozlama.SOAT_MAX)) * 60
    hozir = local.hour * 60 + local.minute
    return boshi <= hozir < TUGASH_DAQIQA


def vaqt_tugadi(local: datetime) -> bool:
    """Bugungi SMS vaqti tugadimi (17:45 dan keyin)."""
    return local.hour * 60 + local.minute >= TUGASH_DAQIQA


def kun_boshi(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, dtime.min))


def bugungi_kunlar(today: date) -> list[tuple[int, int]]:
    """Bugun tabriklanadigan (kun, oy) juftlari. 29-fevralda tug'ilganlar
    kabisa bo'lmagan yilda 1-martda tabriklanadi (catalog/tugilgan_kun.py)."""
    pairs = [(today.day, today.month)]
    if today.month == 3 and today.day == 1 and not calendar.isleap(today.year):
        pairs.append((29, 2))
    return pairs


def tugilganlar(today: date):
    """Bugun tug'ilgan kuni bo'lgan faol mijozlar."""
    q = Q()
    for day, month in bugungi_kunlar(today):
        q |= Q(birth_day=day, birth_month=month)
    return Customer.objects.filter(q, archived=False)


# --------------------------------------------------------------- shablon

@dataclass(frozen=True)
class ShablonHolati:
    kod: str        # ok / reklama / kutilmoqda / rad / yoq / xato / tekshirilmaydi
    ruxsat: bool    # shu matn bilan SMS yuborsa bo'ladimi
    izoh: str


def _shablon_kalit(p, matn: str) -> str:
    return "sms:shablon:" + hashlib.sha1(f"{p.nom}|{_norm(matn)}".encode()).hexdigest()


def shablon_holati(p, matn: str, yangi: bool = False) -> ShablonHolati:
    """Matn SMS xizmatida tasdiqlanganmi. Natija 10 daqiqa (xato — 2 daqiqa)
    eslab qolinadi; `yangi=True` — qaytadan so'raydi."""
    kesh, kalit = prov._kesh(), _shablon_kalit(p, matn)
    if not yangi:
        saved = kesh.get(kalit)
        if saved:
            return ShablonHolati(*saved)
    muddat = 600
    try:
        rows = p.shablonlar()
    except prov.ProvayderXato as e:
        h, muddat = ShablonHolati("xato", False, str(e)), 120
    except requests.RequestException:
        h, muddat = ShablonHolati("xato", False, "SMS xizmati bilan aloqa yo'q — matn tekshirilmadi"), 120
    else:
        if rows is None:
            h = ShablonHolati("tekshirilmaydi", True, "")
        else:
            want = _norm(matn)
            # Xizmat shablonida {o'zgaruvchi} bo'lsa (usms) — mosligini xizmatning o'zi aniqlaydi
            mos = getattr(p, "shablonga_mos", None)
            holatlar = {r.holat for r in rows
                        if want in (_norm(r.matn), _norm(r.asl)) or (mos is not None and mos(r.matn, matn) is not None)}
            if "service" in holatlar:
                h = ShablonHolati("ok", True, "Matn SMS xizmatida tasdiqlangan.")
            elif "reklama" in holatlar:
                h = ShablonHolati("reklama", True, "Matn tasdiqlangan, lekin SMS xizmati uni REKLAMA deb "
                                                   "belgilagan — har bir SMS qimmatroq turadi.")
            elif holatlar & {"moderation", "inproccess"}:
                h = ShablonHolati("kutilmoqda", False, "Matn SMS xizmatida tekshirilmoqda — "
                                                       "tasdiqlanguncha SMS yuborilmaydi.")
            elif "rejected" in holatlar:
                h = ShablonHolati("rad", False, "SMS xizmati bu matnni rad etgan — matnni o'zgartirib, "
                                                "qayta tasdiqlating.")
            else:
                h = ShablonHolati("yoq", False, "Bu matn SMS xizmatidagi tasdiqlangan matnlar ichida yo'q. "
                                                "Avval SMS xizmati kabinetida aynan shu matnni tasdiqlating — "
                                                "ungacha SMS yuborilmaydi.")
    kesh.set(kalit, (h.kod, h.ruxsat, h.izoh), muddat)
    return h


def haqiqiy_matn(p, matn: str) -> str:
    """SMS'da aslida ketadigan matn — xizmat uni o'z shablonidagi yozuv bilan yuborsa (usms),
    SMS soni va narxi shunga qarab hisoblanadi. Aniqlab bo'lmasa — matnning o'zi."""
    f = getattr(p, "haqiqiy_matn", None)
    if f is None:
        return matn
    try:
        return f(matn) or matn
    except Exception:  # noqa: BLE001 — hisob uchun xolos: xizmat javob bermasa ham sahifa ochilsin
        return matn


def balans(p, yangi: bool = False) -> tuple[Decimal | None, str]:
    """(balans so'mda yoki None, xato matni). 10 daqiqa eslab qolinadi."""
    kesh, kalit = prov._kesh(), f"sms:balans:{p.nom}"
    if not yangi:
        saved = kesh.get(kalit)
        if saved:
            return saved[0], saved[1]
    muddat = 600
    try:
        value, xato = p.balans(), ""
    except prov.ProvayderXato as e:
        value, xato, muddat = None, str(e), 120
    except requests.RequestException:
        value, xato, muddat = None, "SMS xizmati bilan aloqa yo'q", 120
    kesh.set(kalit, (value, xato), muddat)
    return value, xato


# -------------------------------------------------------------- yuborish

def _yubor(x: SmsXabar, p, now, qayta_mumkin: bool = True) -> prov.Natija:
    """Bitta jurnal yozuvini xizmatga yuboradi va natijani yozadi.

    Xizmat SMS'ni QABUL QILMAGAN bo'lsa (rad etdi yoki ulanib bo'lmadi) —
    keyingi fon ishida qayta uriniladi (jami QAYTA_URINISH marta): masalan
    balans tugagan bo'lsa, to'ldirilgach o'zi ketadi. Javob kelmagan bo'lsa
    («noma'lum») — qayta urinilmaydi, aks holda SMS ikki marta ketishi mumkin."""
    x.urinish += 1
    # Takrorlanmas kalit (xizmat qo'llasa): bitta so'rov tarmoqda ikki marta yetib borsa ham
    # bitta SMS bo'ladi. Har urinishga YANGI kalit: xizmat rad etilgan urinishni ham eslab
    # qolishi mumkin — eski kalit bilan qayta urinilsa, SMS umuman ketmay qolardi.
    kw = ({"kalit": f"sevimli-sms-{x.pk}-{uuid.uuid4().hex[:12]}"} if getattr(p, "idempotent", False) else {})
    try:
        n = p.yubor(x.telefon, x.matn, **kw)
    except Exception as e:  # noqa: BLE001 — kutilmagan xato: yuborilgani noma'lum
        logger.exception("SMS #%s yuborishda kutilmagan xato", x.pk)
        n = prov.Natija(False, xato=f"Kutilmagan xato ({type(e).__name__}) — yuborilgani noma'lum", nomalum=True)
    x.provayder = p.nom
    x.holat_at = None                    # «navbatga olingan» belgisi tozalanadi
    if n.ok:
        x.holat, x.xato = SmsXabar.YUBORILDI, ""
        x.provayder_id, x.provayder_holat, x.sent_at = n.provayder_id, n.holat, now
    elif n.nomalum:
        x.holat, x.xato = SmsXabar.NOMALUM, n.xato[:300]
    elif qayta_mumkin and x.urinish < QAYTA_URINISH:
        x.holat, x.xato = SmsXabar.QAYTA, n.xato[:300]
    else:
        x.holat, x.xato = SmsXabar.XATO, n.xato[:300]
    x.save(update_fields=["urinish", "provayder", "holat", "xato", "provayder_id",
                          "provayder_holat", "sent_at", "holat_at"])
    return n


def _yangi_nomzodlar(today: date) -> list[tuple[Customer, str]]:
    """Bugun tabrik oladigan, hali yuborilmagan mijozlar: (mijoz, telefon)."""
    cands = list(tugilganlar(today).filter(sms_rozi=True).exclude(phone="").order_by("pk"))
    if not cands:
        return []
    olgan = set(SmsXabar.objects.filter(tur=T, yil=today.year, customer_id__in=[c.pk for c in cands])
                .values_list("customer_id", flat=True))
    bugungi_tel = set(SmsXabar.objects.filter(tur=T, sana=today).values_list("telefon", flat=True))
    out, seen = [], set()
    for c in cands:
        tel = telefon_998(c.phone)
        if not tel or c.pk in olgan or tel in bugungi_tel or tel in seen:
            continue
        seen.add(tel)
        out.append((c, tel))
    return out


def tabriklarni_yubor(now=None, p=None) -> dict:
    """Bugun tug'ilganlarga tabrik SMS. Qaytaradi: {"yuborildi", "xato", "sabab"}
    («sabab» — nega hech narsa yuborilmagani). Qayta chaqirish xavfsiz."""
    now = now or timezone.now()
    out = {"yuborildi": 0, "xato": 0, "sabab": ""}
    s = SmsSozlama.get()
    if not s.yoqilgan:
        out["sabab"] = "tabrik o'chirilgan"
        return out
    p = p or prov.joriy()
    if p is None:
        out["sabab"] = "SMS xizmati ulanmagan"
        return out
    local = timezone.localtime(now)
    today = local.date()

    # Yarim yo'lda uzilib qolganlar: yuborilgani noma'lum — qayta yuborilmaydi.
    # (`holat_at` — qayta urinish uchun navbatga olingan vaqt.)
    eski = now - timedelta(minutes=UZILGAN_DAQIQA)
    (SmsXabar.objects.filter(holat=SmsXabar.NAVBATDA, created_at__lt=eski)
     .filter(Q(holat_at__isnull=True) | Q(holat_at__lt=eski))
     .update(holat=SmsXabar.NOMALUM, xato="Jarayon uzilib qoldi — yuborilgani noma'lum"))
    # Kechagi kundan qolgan «qayta uriniladi»lar: tug'ilgan kun o'tdi — yopamiz.
    SmsXabar.objects.filter(tur=T, holat=SmsXabar.QAYTA, sana__lt=today).update(
        holat=SmsXabar.XATO, xato="Kun tugadi — yuborilmadi")

    if not vaqt_mumkin(local, s.soat):
        out["sabab"] = "hozir SMS vaqti emas"
        return out
    matn = (s.matn or "").strip()
    if not matn:
        out["sabab"] = "tabrik matni bo'sh"
        return out

    qayta = list(SmsXabar.objects.filter(tur=T, holat=SmsXabar.QAYTA, sana=today)
                 .select_related("customer").order_by("pk"))
    yangi = _yangi_nomzodlar(today)
    if not qayta and not yangi:
        out["sabab"] = "bugun yuboriladigan tabrik yo'q"
        return out

    sh = shablon_holati(p, matn)
    if not sh.ruxsat:
        out["sabab"] = sh.izoh
        return out

    boshlandi = time.monotonic()
    holat = {"navbatda": 0, "ketma_ket_xato": 0}

    def tamom() -> bool:
        return (holat["navbatda"] >= BIR_NAVBATDA or holat["ketma_ket_xato"] >= KETMA_KET_XATO
                or time.monotonic() - boshlandi > VAQT_BUDJETI)

    def hisob(n: prov.Natija) -> bool:
        """Natijani sanaydi; True — xizmat ishlamayapti (ulanib bo'lmadi yoki
        javob bermadi), navbatning qolgani keyingi fon ishiga qoladi."""
        holat["navbatda"] += 1
        if n.ok:
            out["yuborildi"] += 1
            holat["ketma_ket_xato"] = 0
        else:
            out["xato"] += 1
            holat["ketma_ket_xato"] += 1
        return n.qayta or n.nomalum

    qism = kodlash(haqiqiy_matn(p, matn)).qism
    uzildi = False
    for x in qayta:
        if tamom():
            break
        c = x.customer
        if c is None or c.archived or c.sms_rozi is not True:
            x.holat, x.xato = SmsXabar.XATO, "Mijozning roziligi yo'q — yuborilmadi"
            x.save(update_fields=["holat", "xato"])
            continue
        # Ikki fon ishi bir vaqtda ishlab qolsa — yozuvni faqat bittasi oladi
        # (aks holda bitta SMS ikki marta ketardi). Joriy matn bilan ketadi.
        if not SmsXabar.objects.filter(pk=x.pk, holat=SmsXabar.QAYTA).update(
                holat=SmsXabar.NAVBATDA, holat_at=now, matn=matn, qismlar=qism):
            continue
        x.matn, x.qismlar = matn, qism
        if hisob(_yubor(x, p, now)):
            uzildi = True
            break

    bugun_soni = SmsXabar.objects.filter(tur=T, sana=today).count()
    for c, tel in ([] if uzildi else yangi):
        if tamom():
            break
        if bugun_soni >= s.kunlik_chegara:
            out["sabab"] = f"kunlik chegara ({s.kunlik_chegara} ta SMS) to'ldi"
            break
        try:
            with transaction.atomic():
                x = SmsXabar.objects.create(customer=c, tur=T, yil=today.year, sana=today, telefon=tel,
                                            matn=matn, qismlar=qism, provayder=p.nom, created_at=now)
        except IntegrityError:
            continue                     # boshqa jarayon shu mijozga (yoki telefonga) ulgurib yozgan
        bugun_soni += 1
        if hisob(_yubor(x, p, now)):
            break
    if out["yuborildi"] or out["xato"]:
        logger.info("SMS tabrik: %s ta yuborildi, %s ta xato", out["yuborildi"], out["xato"])
    return out


def xatolarni_qayta_navbatga(now=None) -> int:
    """Bugun yuborilmay qolgan tabriklarni qayta navbatga qo'yadi (egasi
    paneldan bosadi — masalan balans to'ldirilgach). Faqat xizmat QABUL
    QILMAGANLARI (holati «yuborilmadi»): «noma'lum»lar tegilmaydi."""
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    return SmsXabar.objects.filter(tur=T, holat=SmsXabar.XATO, sana=today,
                                   customer__sms_rozi=True).update(holat=SmsXabar.QAYTA, urinish=0)


def holatlarni_yangila(now=None, p=None) -> int:
    """Yuborilgan SMS'lar yetib borganini xizmatdan so'raydi (oxirgi 3 kun)."""
    now = now or timezone.now()
    p = p or prov.joriy()
    if p is None:
        return 0
    rows = (SmsXabar.objects.filter(holat=SmsXabar.YUBORILDI, provayder=p.nom,
                                    sent_at__gte=now - timedelta(hours=72))
            .exclude(provayder_id="")
            .filter(Q(holat_at__isnull=True, sent_at__lte=now - timedelta(seconds=60))
                    | Q(holat_at__lte=now - timedelta(minutes=15)))
            .order_by(F("holat_at").asc(nulls_first=True), "pk")[:15])
    boshlandi, done = time.monotonic(), 0
    for x in rows:
        if time.monotonic() - boshlandi > VAQT_BUDJETI / 2:
            break
        try:
            y = p.holat(x.provayder_id)
        except (prov.ProvayderXato, requests.RequestException):
            break                        # xizmat javob bermayapti — keyingi safar
        x.holat_at = now
        fields = ["holat_at"]
        if y is not None:
            x.provayder_holat = y.xom or x.provayder_holat
            fields.append("provayder_holat")
            if y.narx is not None:
                x.narx = y.narx
                fields.append("narx")
            if y.reklama is not None:
                x.reklama = y.reklama
                fields.append("reklama")
            if y.qismlar:
                x.qismlar = y.qismlar
                fields.append("qismlar")
            if y.holat:
                x.holat = y.holat
                fields.append("holat")
        x.save(update_fields=fields)
        done += 1
    return done


def sinov_yubor(raw_telefon: str, kim: str = "", matn: str | None = None, now=None, p=None) -> SmsXabar:
    """Paneldan bitta sinov SMS. `matn` berilmasa — joriy tabrik matni
    (u SMS xizmatida tasdiqlangan bo'lishi shart). Xato — SmsXato."""
    now = now or timezone.now()
    p = p or prov.joriy()
    if p is None:
        raise SmsXato("SMS xizmati hali ulanmagan")
    tel = telefon_998(raw_telefon)
    if not tel:
        raise SmsXato("Telefon raqami noto'g'ri. Masalan: 90 123 45 67")
    if SmsXabar.objects.filter(tur=SmsXabar.SINOV,
                               created_at__gte=now - timedelta(hours=1)).count() >= SINOV_SOATIGA:
        raise SmsXato(f"Bir soatda {SINOV_SOATIGA} tadan ko'p sinov SMS yuborib bo'lmaydi")
    if matn is None:
        matn = (SmsSozlama.get().matn or "").strip()
        if not matn:
            raise SmsXato("Tabrik matni bo'sh")
        sh = shablon_holati(p, matn, yangi=True)
        if not sh.ruxsat:
            raise SmsXato(sh.izoh)
    x = SmsXabar.objects.create(tur=SmsXabar.SINOV, telefon=tel, matn=matn,
                                qismlar=kodlash(haqiqiy_matn(p, matn)).qism,
                                provayder=p.nom, kim=(kim or "")[:150], created_at=now)
    _yubor(x, p, now, qayta_mumkin=False)
    return x


# ----------------------------------------------------------------- panel

def sms_holati(c: Customer) -> tuple[str, str]:
    """Mijozga tabrik SMS keta oladimi: (kod, matn).
    kod: «ok» — oladi; «rad» — rad etgan; «soralmagan» — rozilik so'ralmagan;
    «telefon» — telefon raqami yo'q yoki noto'g'ri."""
    if c.sms_rozi is False:
        return "rad", "SMS olishni rad etgan"
    if c.sms_rozi is None:
        return "soralmagan", "rozilik so'ralmagan"
    if not telefon_998(c.phone):
        return "telefon", "telefon raqami yo'q yoki noto'g'ri"
    return "ok", "SMS oladi"


def bugungi_royxat(today: date) -> list[dict]:
    """Bugun tug'ilgan mijozlar va har biriga SMS nima bo'lgani (panel uchun)."""
    custs = list(tugilganlar(today).order_by("name")[:300])
    xabarlar = {x.customer_id: x for x in
                SmsXabar.objects.filter(tur=T, yil=today.year, customer_id__in=[c.pk for c in custs])}
    out = []
    for c in custs:
        x = xabarlar.get(c.pk)
        kod, matn = sms_holati(c)
        izoh = "" if x else ("navbatda" if kod == "ok" else matn)
        out.append({"mijoz": c, "xabar": x, "izoh": izoh})
    return out


def yaqin_kunlar(today: date, kun: int = 7) -> dict:
    """Keyingi `kun` kunda tug'ilgan kuni bor mijozlar: jami va SMS oladiganlari."""
    q = Q()
    for i in range(1, kun + 1):
        for day, month in bugungi_kunlar(today + timedelta(days=i)):
            q |= Q(birth_day=day, birth_month=month)
    qs = Customer.objects.filter(q, archived=False)
    return {"jami": qs.count(), "sms": qs.filter(sms_rozi=True).exclude(phone="").count()}


def statistika() -> dict:
    """Mijozlar bazasi SMS uchun qanchalik tayyor."""
    faol = Customer.objects.filter(archived=False)
    kartali = faol.exclude(discount_card="")
    return {
        "kartali": kartali.count(),
        "telefonli": kartali.exclude(phone="").count(),
        "tugilgan": kartali.filter(birth_day__isnull=False).count(),
        "rozi": faol.filter(sms_rozi=True).count(),
        "rad": faol.filter(sms_rozi=False).count(),
        "soralmagan": kartali.filter(sms_rozi__isnull=True).count(),
        "tayyor": faol.filter(sms_rozi=True, birth_day__isnull=False).exclude(phone="").count(),
    }


def oy_hisobi(now=None) -> dict:
    """Shu oyda yuborilgan SMS'lar: holat bo'yicha soni va sarflangan pul."""
    local = timezone.localtime(now or timezone.now())
    boshi = kun_boshi(local.date().replace(day=1))
    rows = SmsXabar.objects.filter(created_at__gte=boshi)
    soni = {r["holat"]: r["n"] for r in rows.values("holat").annotate(n=Count("id"))}
    qabul = sum(soni.get(h, 0) for h in SmsXabar.QABUL_QILINGAN)
    return {
        "qabul": qabul,
        "yetkazildi": soni.get(SmsXabar.YETKAZILDI, 0),
        "xato": soni.get(SmsXabar.XATO, 0) + soni.get(SmsXabar.YETMADI, 0) + soni.get(SmsXabar.NOMALUM, 0),
        "pul": rows.aggregate(s=Sum("narx"))["s"],
    }
