"""SMS xizmati (provayder) bilan gaplashish.

Ikki haqiqiy xizmat: usms.uz (egasi tanlagan, 2026-11) va Eskiz (notify.eskiz.uz).
`sinov` — tarmoqqa chiqmaydigan soxta xizmat (testlar va lokal ishga tushirish uchun).

Sozlash — Railway Variables (egasi o'zi qo'yadi; kodda va logda ko'rinmaydi):

    USMS_LOGIN      — usms kabineti → Профиль → «Доступ для интеграции» → «Логин для интеграции»
    USMS_SECRET     — o'sha joydagi «Секретный ключ»
    USMS_FROM       — jo'natuvchi nomi (ixtiyoriy; bo'sh bo'lsa usms o'zi tanlaydi)

    ESKIZ_EMAIL     — Eskiz kabinetidagi email
    ESKIZ_PASSWORD  — Eskiz kabinetidagi API uchun maxfiy kalit
    ESKIZ_FROM      — jo'natuvchi (standart 4546; o'z nomi olingan bo'lsa — o'sha)

Ikkalasi ham qo'yilgan bo'lsa usms ishlaydi (SMS_PROVAYDER=eskiz — Eskizni majburlaydi).
Hech biri bo'lmasa — SMS xizmati «ulanmagan»: hech narsa yuborilmaydi.

Eskiz API (rasmiy hujjatdan yig'ilgan OpenAPI klienti bo'yicha, 2026-11):

    POST  /api/auth/login                     email, password → data.token        (multipart/form-data)
    POST  /api/message/sms/send               mobile_phone, message, from → id, status   (multipart/form-data)
    GET   /api/message/sms/status_by_id/{id}  → data.status, data.is_ad, data.total_price
    GET   /api/user/get-limit                 → data.balance
    GET   /api/user/templates                 → result[]: template, original_text, status

DIQQAT: bu kod haqiqiy Eskiz hisobi bilan hali SINALMAGAN (hisob yo'q edi) —
faqat soxta javoblar bilan sinovdan o'tgan. Hisob ochilgach birinchi ish —
panel orqali sinov SMS yuborib tekshirish.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class ProvayderXato(Exception):
    """SMS xizmatiga kirib bo'lmadi — egasiga ko'rsatiladigan matn bilan."""


@dataclass(frozen=True)
class Natija:
    """Bitta SMS yuborish natijasi."""

    ok: bool
    provayder_id: str = ""
    holat: str = ""
    xato: str = ""
    #: Xizmat SMS'ni aniq QABUL QILMAGAN — keyinroq qayta urinish xavfsiz.
    qayta: bool = False
    #: Javob kelmadi — qabul qilingani noma'lum. Ikki marta ketmasligi uchun
    #: bunday SMS avtomatik qayta yuborilmaydi.
    nomalum: bool = False


@dataclass(frozen=True)
class Yetkazish:
    """SMS yetkazilish holati (xizmatdan so'ralgan)."""

    holat: str                       # "yetkazildi" / "yetmadi" / "" (hali aniq emas)
    xom: str = ""                    # xizmatning o'z holat matni (DELIVRD, ...)
    narx: Decimal | None = None      # so'm
    reklama: bool | None = None      # xizmat «reklama» deb hisobladimi
    qismlar: int | None = None


@dataclass(frozen=True)
class Shablon:
    """SMS xizmatida tasdiqlashga berilgan matn (shablon)."""

    matn: str
    asl: str
    holat: str                       # service / reklama / moderation / inproccess / rejected
    id: int | None = None            # xizmatdagi raqami (usms: shu raqam bilan yuboriladi)
    umumiy: bool = False             # xizmatning tayyor matni (usms «База шаблонов»)


YETKAZILDI, YETMADI = "yetkazildi", "yetmadi"
_YETDI = {"DELIVRD", "DELIVERED", "DLVRD"}
_YETMADI = {"REJECTD", "REJECTED", "UNDELIV", "UNDELIVERABLE", "UNDELIVERED", "EXPIRED",
            "FAILED", "DELETED", "NOT_DELIVERED", "NOTDELIVERED"}


def _kesh():
    """Jarayonlar o'rtasida umumiy kesh (bazada) — token har safar qayta
    olinmasin. Bo'lmasa — oddiy kesh."""
    from django.core.cache import caches

    try:
        return caches["shared"]
    except Exception:  # noqa: BLE001 — «shared» sozlanmagan muhit (masalan test)
        return caches["default"]


def _json(response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _xabar(data: dict) -> str:
    """Xizmat qaytargan xato matni — qanday ko'rinishda bo'lsa ham bitta qator."""
    msg = data.get("message")
    if isinstance(msg, dict):
        parts = []
        for value in msg.values():
            parts.extend(value if isinstance(value, list) else [value])
        msg = "; ".join(str(p) for p in parts if p)
    elif isinstance(msg, list):
        msg = "; ".join(str(p) for p in msg if p)
    return str(msg or "").strip()


def _ulanmagan(exc: Exception) -> bool:
    """Ulanishning o'zi bo'lmadi (so'rov xizmatga yetib bormagan) — bunday
    holda keyinroq qayta urinish xavfsiz: SMS ikki marta ketmaydi."""
    if isinstance(exc, requests.ConnectTimeout):
        return True
    if isinstance(exc, requests.ConnectionError):
        from urllib3.exceptions import NewConnectionError

        reason = getattr(exc.args[0], "reason", None) if exc.args else None
        return isinstance(reason, NewConnectionError)
    return False


def _forma(maydonlar: dict) -> dict:
    """Eskiz hujjatidagidek multipart/form-data maydonlari (`requests` files=)."""
    return {nom: (None, str(qiymat)) for nom, qiymat in maydonlar.items()}


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


_APOSTROF = dict.fromkeys(map(ord, "\u2019\u2018\u02bb\u02bc`\u00b4\u2032"), "'")


def _tekis(matn: str) -> str:
    """Solishtirish uchun: bo'shliqlar bitta, har xil apostrof («‘», «ʻ», «’») bir xil."""
    return " ".join((matn or "").translate(_APOSTROF).split())


def shablonga_mos(shablon_matni: str, matn: str, raqamli=()) -> dict[str, str] | None:
    """Tayyor `matn` shablonga to'g'ri keladimi.

    Shablondagi `{nom}` — o'zgaruvchi: matnda uning o'rnida istalgan yozuv turishi mumkin.
    Mos kelsa — o'zgaruvchilar qiymati (o'zgaruvchisiz shablonda `{}`), kelmasa — None.
    Qiymat bo'sh yoki 80 belgidan uzun bo'lsa — mos emas (usms qoidasi); `raqamli`
    ro'yxatidagi o'zgaruvchi faqat raqamdan iborat bo'lishi shart."""
    t, m = _tekis(shablon_matni), _tekis(matn)
    if not t or not m:
        return None
    bolaklar = re.split(r"\{(\w+)\}", t)
    if len(bolaklar) == 1:
        return {} if t == m else None
    naqsh, nomlar = [], []
    for i, b in enumerate(bolaklar):
        if i % 2 == 0:
            naqsh.append(re.escape(b))
        elif b in nomlar:                                # o'sha o'zgaruvchi yana — o'sha qiymat
            naqsh.append(f"(?P=g{nomlar.index(b)})")
        else:
            nomlar.append(b)
            naqsh.append(f"(?P<g{len(nomlar) - 1}>.+?)")
    mos = re.fullmatch("".join(naqsh), m)
    if mos is None:
        return None
    qiymat = {nom: mos.group(f"g{i}").strip() for i, nom in enumerate(nomlar)}
    for nom, v in qiymat.items():
        if not v or len(v) > 80 or (nom in raqamli and not v.isdigit()):
            return None
    return qiymat


class Eskiz:
    nom = "eskiz"
    korinish = "Eskiz"
    #: Shartnoma tuzilgunicha (sinov holatida) Eskiz faqat shu matnni yuboradi.
    sinov_matni = "Bu Eskiz dan test"

    TOKEN_KALIT = "sms:eskiz:token"
    XATO_KALIT = "sms:eskiz:kirish-xato"
    TOKEN_MUDDAT = 20 * 24 * 3600        # Eskiz tokeni 30 kun yashaydi
    XATO_MUDDAT = 300                    # noto'g'ri parol bilan har daqiqa urinmaslik

    def __init__(self, email: str, password: str, sender: str = "4546",
                 base: str = "https://notify.eskiz.uz/api", http=None, kesh=None,
                 timeout: tuple[int, int] = (5, 15)):
        self.email = email
        self.password = password
        self.sender = sender or "4546"
        self.base = (base or "https://notify.eskiz.uz/api").rstrip("/")
        self.http = http or requests
        self.kesh = kesh if kesh is not None else _kesh()
        self.timeout = timeout

    # ------------------------------------------------------------- kirish

    def _kirish(self) -> str:
        blok = self.kesh.get(self.XATO_KALIT)
        if blok:
            raise ProvayderXato(blok)
        try:
            r = self.http.request(
                "POST", f"{self.base}/auth/login",
                files=_forma({"email": self.email, "password": self.password}), timeout=self.timeout,
            )
        except requests.RequestException:
            # Sababni zanjirga qo'shmaymiz: xato matniga so'rov ma'lumoti tushmasin.
            raise ProvayderXato("Eskiz bilan aloqa yo'q") from None
        token = (_json(r).get("data") or {}).get("token") if r.status_code < 400 else ""
        if not token:
            if r.status_code in (400, 401, 403, 422):
                msg = "Eskiz login yoki maxfiy kaliti noto'g'ri (ESKIZ_EMAIL / ESKIZ_PASSWORD)"
            else:
                msg = f"Eskiz kirishda xato berdi (HTTP {r.status_code})"
            self.kesh.set(self.XATO_KALIT, msg, self.XATO_MUDDAT)
            raise ProvayderXato(msg)
        self.kesh.set(self.TOKEN_KALIT, token, self.TOKEN_MUDDAT)
        return token

    def _sorov(self, method: str, path: str, **kw):
        """Token bilan so'rov. Token eskirgan bo'lsa (401) — bir marta qayta
        kirib, so'rovni takrorlaydi (401 — so'rov bajarilmagan degani)."""
        token = self.kesh.get(self.TOKEN_KALIT) or self._kirish()
        kw.setdefault("timeout", self.timeout)
        for attempt in (1, 2):
            r = self.http.request(
                method, self.base + path,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, **kw,
            )
            if r.status_code != 401 or attempt == 2:
                return r
            self.kesh.delete(self.TOKEN_KALIT)
            token = self._kirish()
        return r

    # ------------------------------------------------------------ amallar

    def yubor(self, telefon: str, matn: str) -> Natija:
        try:
            r = self._sorov("POST", "/message/sms/send", files=_forma(
                {"mobile_phone": telefon, "message": matn, "from": self.sender}))
        except ProvayderXato as e:
            return Natija(False, xato=str(e), qayta=True)
        except requests.RequestException as e:
            if _ulanmagan(e):
                return Natija(False, xato="SMS xizmatiga ulanib bo'lmadi", qayta=True)
            return Natija(False, xato="SMS xizmati javob bermadi — yuborilgani noma'lum", nomalum=True)
        data = _json(r)
        status = str(data.get("status") or "").lower()
        if r.status_code < 300 and data.get("id") and status not in ("error", "failed"):
            return Natija(True, provayder_id=str(data["id"])[:64], holat=status[:32])
        msg = (_xabar(data) or f"HTTP {r.status_code}")[:300]
        if r.status_code >= 500:
            return Natija(False, xato=f"SMS xizmati xato berdi: {msg}"[:300], nomalum=True)
        return Natija(False, xato=msg)

    def holat(self, provayder_id: str) -> Yetkazish | None:
        r = self._sorov("GET", f"/message/sms/status_by_id/{provayder_id}")
        if r.status_code >= 400:
            return None
        d = _json(r).get("data")
        if not isinstance(d, dict):
            return None
        xom = str(d.get("status") or "").strip().upper()
        holat = YETKAZILDI if xom in _YETDI else YETMADI if xom in _YETMADI else ""
        reklama = d.get("is_ad")
        parts = d.get("parts_count")
        return Yetkazish(
            holat=holat, xom=xom[:32],
            narx=_dec(d.get("total_price") if d.get("total_price") is not None else d.get("price")),
            reklama=bool(reklama) if reklama is not None else None,
            qismlar=int(parts) if isinstance(parts, int) and parts > 0 else None,
        )

    def balans(self) -> Decimal | None:
        r = self._sorov("GET", "/user/get-limit")
        if r.status_code >= 400:
            return None
        return _dec((_json(r).get("data") or {}).get("balance"))

    def shablonlar(self) -> list[Shablon] | None:
        r = self._sorov("GET", "/user/templates")
        if r.status_code >= 400:
            raise ProvayderXato(f"Eskiz shablonlar ro'yxatini bermadi (HTTP {r.status_code})")
        rows = _json(r).get("result")
        if not isinstance(rows, list):
            raise ProvayderXato("Eskiz shablonlar ro'yxatini kutilmagan ko'rinishda berdi")
        out = []
        for row in rows:
            if isinstance(row, dict):
                out.append(Shablon(matn=str(row.get("template") or ""),
                                   asl=str(row.get("original_text") or ""),
                                   holat=str(row.get("status") or "").strip().lower()))
        return out


class Usms:
    """usms.uz — HTTPS API v1 (hujjat: api.usms.uz; 2026-11-17 da o'qilgan):

        Authorization: Bearer LOGIN:SECRET
        GET   /balance                  → {balance}
        GET   /templates?include=base   → {items: [{id, text, kind: service|ad, base, variables: [{key, type}]}]}
        POST  /send                     {phone, template_id + params | text, from?, idem_key?}
                                        → {id, status, parts, price, pending?, repeated?, test?}
        GET   /status/{id}              → {status, status_note, price, parts, test?}

    Holatlar: queued, sent, unknown — hali aniq emas; delivered — yetdi; failed — operator
    yetkazmadi (pul qaytmaydi); rejected — qabul qilinmadi (pul qaytadi).
    Xatolar: {"error": "…"} — 400 (noto'g'ri so'rov; reklama shabloni 18:00–09:00 da),
    401 (kalit), 402 (mablag'), 403 (shablon/jo'natuvchi/qora ro'yxat), 429 (chegara: 120/daq).

    Erkin matn yuborib bo'lmaydi: har SMS tasdiqlangan shablonga mos bo'lishi shart. Shuning
    uchun panel matni shablonlar ichidan qidiriladi — o'zingiz tasdiqlatgan yoki usms
    «Bazasi»dagi tayyor matn (ichidagi {o'zgaruvchi}lar o'rniga yozilgan so'zlar o'zi ajratib
    olinadi) — va SMS shablon raqami bilan yuboriladi. Topilmasa matnning o'zi yuboriladi:
    usms o'zi tekshiradi va mos kelmasa rad etadi.

    TEST kalit (kabinetda «Выпустить тестовый ключ»): pul yechilmaydi, SMS operatorga
    ketmaydi, ~2 soniyada «delivered» bo'ladi; jurnalda «test» deb belgilanadi.

    DIQQAT: bu kod haqiqiy usms hisobi bilan hali SINALMAGAN — faqat hujjatdagi javoblar
    va lokal soxta server bilan. Kalit qo'yilgach birinchi ish — paneldan o'z raqamingizga
    bitta sinov SMS («Xizmat sinov matni»); tabrik yuborish ungacha o'chiq turadi.
    """

    nom = "usms"
    korinish = "usms.uz"
    #: usms hisobga tayinlaydigan tayyor sinov shabloni — ulanishni tekshirish uchun
    sinov_matni = "Testoviy sms ot usms.uz"
    #: `yubor` ga takrorlanmas kalit beriladi (bitta so'rov ikki marta SMS bo'lmasin)
    idempotent = True
    shablonga_mos = staticmethod(shablonga_mos)

    SHABLON_MUDDAT = 600

    def __init__(self, login: str, secret: str, sender: str = "",
                 base: str = "https://usms.uz/api/v1", http=None, kesh=None,
                 timeout: tuple[int, int] = (5, 15)):
        self.login = login
        self.secret = secret
        self.sender = sender or ""
        self.base = (base or "https://usms.uz/api/v1").rstrip("/")
        self.http = http or requests
        self.kesh = kesh if kesh is not None else _kesh()
        self.timeout = timeout
        self._kalit = "sms:usms:shablonlar:" + hashlib.sha1(login.encode()).hexdigest()[:12]

    # ------------------------------------------------------------ so'rov

    def _sorov(self, method: str, path: str, **kw):
        kw.setdefault("timeout", self.timeout)
        return self.http.request(
            method, self.base + path,
            headers={"Authorization": f"Bearer {self.login}:{self.secret}", "Accept": "application/json"}, **kw,
        )

    @staticmethod
    def _xato(r, nima: str = "") -> str:
        """Egasiga ko'rsatiladigan xato matni (kalit hech qachon qo'shilmaydi)."""
        if r.status_code == 401:
            return "usms login yoki maxfiy kaliti noto'g'ri yoki bekor qilingan (USMS_LOGIN / USMS_SECRET)"
        if r.status_code == 402:
            return "usms hisobida mablag' yetarli emas — balansni to'ldiring"
        if r.status_code == 429:
            return "usms so'rovlar chegarasi to'ldi — birozdan keyin o'zi qayta urinadi"
        data = _json(r)
        msg = str(data.get("error") or "").strip() or _xabar(data)
        maydon = data.get("fields")
        if isinstance(maydon, dict):
            maydon = list(maydon)
        if isinstance(maydon, (list, tuple)) and maydon:
            msg = f"{msg} ({', '.join(str(m) for m in maydon)})".strip()
        msg = msg or f"HTTP {r.status_code}"
        return (f"{nima}: {msg}" if nima else msg)[:300]

    # ---------------------------------------------------------- shablonlar

    def _royxat(self, yangi: bool = False) -> list[dict]:
        """Yuborsa bo'ladigan shablonlar (o'zimizniki + usms bazasi). 10 daqiqa eslab qolinadi."""
        if not yangi:
            saved = self.kesh.get(self._kalit)
            if saved is not None:
                return saved
        r = self._sorov("GET", "/templates", params={"include": "base"})
        if r.status_code >= 400:
            raise ProvayderXato(self._xato(r, "usms shablonlar ro'yxatini bermadi"))
        items = _json(r).get("items")
        if not isinstance(items, list):
            raise ProvayderXato("usms shablonlar ro'yxatini kutilmagan ko'rinishda berdi")
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                sid = int(it.get("id"))
            except (TypeError, ValueError):
                continue
            ozg = it.get("variables") if isinstance(it.get("variables"), list) else []
            out.append({
                "id": sid, "matn": str(it.get("text") or ""),
                "tur": str(it.get("kind") or "service").strip().lower(),
                "umumiy": bool(it.get("base")),
                "raqamli": [str(v.get("key")) for v in ozg
                            if isinstance(v, dict) and str(v.get("type") or "").lower() == "digits"],
            })
        self.kesh.set(self._kalit, out, self.SHABLON_MUDDAT)
        return out

    def _top(self, matn: str) -> tuple[dict, dict[str, str]] | None:
        """Matnga mos shablon: (shablon, o'zgaruvchilar). Topilmasa yoki ro'yxat olinmasa — None."""
        try:
            royxat = self._royxat()
        except (ProvayderXato, requests.RequestException):
            return None
        nomzod = []
        for sh in royxat:
            q = shablonga_mos(sh["matn"], matn, raqamli=sh["raqamli"])
            if q is not None:
                # avval «xizmat» turidagisi (reklama qimmatroq va faqat kunduzi ketadi), keyin
                # o'zgaruvchisiz (aynan shu matn), keyin o'zimiz tasdiqlatgani
                nomzod.append((sh["tur"] == "ad", bool(q), sh["umumiy"], sh["id"], sh, q))
        if not nomzod:
            return None
        nomzod.sort(key=lambda n: n[:4])
        return nomzod[0][4], nomzod[0][5]

    def haqiqiy_matn(self, matn: str) -> str:
        """SMS'da aslida ketadigan matn. usms shablonning O'Z yozuvini yuboradi — masalan
        tayyor matnlarida «‘» bor: shu bitta belgi SMS'ni 70 belgilik bo'laklarga bo'ladi va
        narx bir necha baravar oshadi. Hisob paneldagi yozuvga emas, shunga qarab qilinadi."""
        topildi = self._top(matn)
        if not topildi:
            return matn
        sh, qiymat = topildi
        return re.sub(r"\{(\w+)\}", lambda m: qiymat.get(m.group(1), m.group(0)), sh["matn"])

    def shablonlar(self) -> list[Shablon] | None:
        return [Shablon(matn=sh["matn"], asl=sh["matn"],
                        holat="reklama" if sh["tur"] == "ad" else "service",
                        id=sh["id"], umumiy=sh["umumiy"])
                for sh in self._royxat(yangi=True)]

    # ------------------------------------------------------------ amallar

    def yubor(self, telefon: str, matn: str, kalit: str = "") -> Natija:
        tana: dict = {"phone": telefon}
        topildi = self._top(matn)
        if topildi:
            tana["template_id"] = topildi[0]["id"]
            if topildi[1]:
                tana["params"] = topildi[1]
        else:
            tana["text"] = matn
        if self.sender:
            tana["from"] = self.sender
        if kalit:
            tana["idem_key"] = kalit[:64]
        try:
            r = self._sorov("POST", "/send", json=tana)
        except requests.RequestException as e:
            if _ulanmagan(e):
                return Natija(False, xato="SMS xizmatiga ulanib bo'lmadi", qayta=True)
            return Natija(False, xato="SMS xizmati javob bermadi — yuborilgani noma'lum", nomalum=True)
        data = _json(r)
        status = str(data.get("status") or "").strip().lower()
        if r.status_code < 300:
            if status == "rejected":                     # qabul qilinmadi, pul qaytarildi
                sabab = str(data.get("status_note") or data.get("error") or "").strip()
                return Natija(False, xato=f"usms SMS'ni rad etdi{': ' + sabab if sabab else ''}"[:300])
            if data.get("id") is None:
                return Natija(False, xato="usms kutilmagan javob berdi — yuborilgani noma'lum", nomalum=True)
            # queued / sent / unknown(pending) / failed — yozuv bor: natijasi holat so'rovida aniqlanadi
            holat = ("test:" if data.get("test") is True else "") + (status or "sent")
            return Natija(True, provayder_id=str(data["id"])[:64], holat=holat[:32])
        if r.status_code >= 500:
            return Natija(False, xato=f"SMS xizmati xato berdi: {self._xato(r)}"[:300], nomalum=True)
        # 401 (kalit), 402 (mablag'), 429 (chegara): keyingi SMS ham o'tmaydi — navbat to'xtaydi
        return Natija(False, xato=self._xato(r), qayta=r.status_code in (401, 402, 429))

    def holat(self, provayder_id: str) -> Yetkazish | None:
        r = self._sorov("GET", f"/status/{provayder_id}")
        if r.status_code == 401:
            raise ProvayderXato(self._xato(r))
        if r.status_code >= 400:
            return None
        d = _json(r)
        status = str(d.get("status") or "").strip().lower()
        if not status:
            return None
        holat = YETKAZILDI if status == "delivered" else YETMADI if status in ("failed", "rejected") else ""
        izoh = str(d.get("status_note") or "").strip()
        xom = (izoh if izoh and len(izoh) <= 16 and " " not in izoh else status).upper()
        if d.get("test") is True:
            xom = "TEST:" + xom
        try:
            qism = int(d.get("parts"))
        except (TypeError, ValueError):
            qism = 0
        return Yetkazish(holat=holat, xom=xom[:32], narx=_dec(d.get("price")),
                         qismlar=qism if qism > 0 else None)

    def balans(self) -> Decimal | None:
        r = self._sorov("GET", "/balance")
        if r.status_code == 401:
            raise ProvayderXato(self._xato(r))
        if r.status_code >= 400:
            return None
        return _dec(_json(r).get("balance"))


class Sinov:
    """Soxta xizmat: hech narsa yubormaydi, hammasini «yetkazildi» deydi.
    Faqat testlar va lokal ishga tushirish uchun (SMS_PROVAYDER=sinov)."""

    nom = "sinov"
    korinish = "Sinov rejimi (haqiqiy SMS ketmaydi)"
    sinov_matni = ""
    yuborilgan: list[tuple[str, str]] = []

    def yubor(self, telefon: str, matn: str) -> Natija:
        Sinov.yuborilgan.append((telefon, matn))
        return Natija(True, provayder_id="sinov-" + uuid.uuid4().hex[:12], holat="waiting")

    def holat(self, provayder_id: str) -> Yetkazish | None:
        return Yetkazish(holat=YETKAZILDI, xom="DELIVRD")

    def balans(self) -> Decimal | None:
        return None

    def shablonlar(self) -> list[Shablon] | None:
        return None                      # shablon tekshiruvi yo'q


def joriy(timeout: tuple[int, int] | None = None):
    """Sozlangan SMS xizmati yoki None («ulanmagan»). `timeout` — (ulanish,
    javob) soniyalarda; panel sahifasi uchun qisqaroq beriladi."""
    nom = (getattr(settings, "SMS_PROVAYDER", "") or "").strip().lower()
    if nom == "sinov":
        return Sinov()
    kw = {"timeout": timeout} if timeout else {}
    login = getattr(settings, "USMS_LOGIN", "") or ""
    secret = getattr(settings, "USMS_SECRET", "") or ""
    if nom != "eskiz" and login and secret:
        return Usms(login, secret, sender=getattr(settings, "USMS_FROM", "") or "",
                    base=getattr(settings, "USMS_URL", "") or "https://usms.uz/api/v1", **kw)
    email = getattr(settings, "ESKIZ_EMAIL", "") or ""
    password = getattr(settings, "ESKIZ_PASSWORD", "") or ""
    if nom != "usms" and email and password:
        return Eskiz(email, password, sender=getattr(settings, "ESKIZ_FROM", "") or "4546",
                     base=getattr(settings, "ESKIZ_URL", "") or "https://notify.eskiz.uz/api", **kw)
    return None
