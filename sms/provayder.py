"""SMS xizmati (provayder) bilan gaplashish.

Hozircha bitta haqiqiy xizmat — Eskiz (notify.eskiz.uz). `sinov` — tarmoqqa
chiqmaydigan soxta xizmat (testlar va lokal ishga tushirish uchun).

Sozlash — Railway Variables (egasi o'zi qo'yadi; kodda va logda ko'rinmaydi):

    ESKIZ_EMAIL     — Eskiz kabinetidagi email
    ESKIZ_PASSWORD  — Eskiz kabinetidagi API uchun maxfiy kalit
    ESKIZ_FROM      — jo'natuvchi (standart 4546; o'z nomi olingan bo'lsa — o'sha)

EMAIL va PASSWORD bo'lmasa — SMS xizmati «ulanmagan»: hech narsa yuborilmaydi.

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

import logging
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
    email = getattr(settings, "ESKIZ_EMAIL", "") or ""
    password = getattr(settings, "ESKIZ_PASSWORD", "") or ""
    if email and password:
        return Eskiz(email, password, sender=getattr(settings, "ESKIZ_FROM", "") or "4546",
                     base=getattr(settings, "ESKIZ_URL", "") or "https://notify.eskiz.uz/api",
                     **({"timeout": timeout} if timeout else {}))
    return None
