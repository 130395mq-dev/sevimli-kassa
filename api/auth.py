"""
Kassa autentifikatsiyasi.

Har bir kassaning o'z tokeni bor. Token `Authorization: Bearer <token>`
sarlavhasida keladi. Bittasi o'g'irlansa — faqat o'sha kassa tokeni
almashtiriladi, qolganlari ishlayveradi.

Tokenlar solishtirishda `secrets.compare_digest` ishlatiladi: oddiy `==`
solishtirish vaqti belgiga qarab o'zgaradi va shu orqali tokenni bitta-bitta
topib olish mumkin.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import secrets
import time

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone

from sales.models import Register


# ---------------------------------------------------------------- sessiya token
#
# Login'da server IMZOLANGAN sessiya tokenini beradi. Unda kim kirgani va
# u manager ekani yozilgan va SECRET_KEY bilan imzolangan. Manager-only
# amallarda (masalan kassaga pul kiritish-chiqarish) mijoz shu tokenni
# yuboradi; server imzoni tekshiradi. Shu tufayli mijoz o'z-o'zicha
# «is_manager» bo'lib q_ola olmaydi — imzoni yasay olmaydi.

def _sign(payload: str) -> str:
    key = settings.SECRET_KEY.encode("utf-8")
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def make_session_token(cashier_id: int, is_manager: bool, ttl_hours: int = 24) -> str:
    """Imzolangan sessiya tokeni. `cashier_id:is_manager:expiry` + imzo."""
    exp = int(time.time()) + ttl_hours * 3600
    payload = f"{int(cashier_id)}:{1 if is_manager else 0}:{exp}"
    b = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{b}.{_sign(payload)}"


def verify_session_token(token: str) -> dict | None:
    """Tokenni tekshiradi. To'g'ri va muddati o'tmagan bo'lsa — ma'lumot,
    aks holda None."""
    if not token or "." not in token:
        return None
    b, sig = token.rsplit(".", 1)
    try:
        pad = "=" * (-len(b) % 4)
        payload = base64.urlsafe_b64decode(b + pad).decode("utf-8")
        cashier_id, is_manager, exp = payload.split(":")
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    if int(exp) < int(time.time()):
        return None
    return {"cashier_id": int(cashier_id), "is_manager": is_manager == "1"}


def manager_required(view):
    """Manager-only amal. `X-Session` sarlavhasidagi imzolangan token
    tekshiriladi — mijozdagi `is_manager`'ga ISHONILMAYDI.

    Orqaga moslik: eski kassalar hali token yubormaydi. Token yo'q bo'lsa —
    `REQUIRE_MANAGER_TOKEN=True` bo'lganda rad etamiz (fleet yangilangach),
    aks holda o'tkazamiz (o'tish davri; kassa logini egasi baribir manager).
    Token BOR-u, lekin yaroqsiz yoki manager emas bo'lsa — HAR DOIM rad.
    """
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        token = (request.headers.get("X-Session") or "").strip()
        strict = getattr(settings, "REQUIRE_MANAGER_TOKEN", False)
        if not token:
            if strict:
                return error("Manager huquqi kerak", status=403)
            return view(request, *args, **kwargs)
        info = verify_session_token(token)
        if not info or not info["is_manager"]:
            return error("Manager huquqi kerak", status=403)
        return view(request, *args, **kwargs)

    return wrapper


def error(message: str, status: int = 400, **extra) -> JsonResponse:
    return JsonResponse({"error": message, **extra}, status=status)


def get_register(request) -> Register | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None

    token = header[7:].strip()
    if not token:
        return None

    # Tokenni bazadan qidiramiz, keyin doimiy vaqtda solishtiramiz
    for reg in Register.objects.filter(active=True).select_related("store"):
        if secrets.compare_digest(reg.api_token, token):
            return reg
    return None


def register_required(view):
    """Kassa tokenisiz kirishni to'xtatadi."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        register = get_register(request)
        if register is None:
            return error("Kassa tokeni noto'g'ri yoki yo'q", status=401)

        request.register = register

        # Oxirgi ko'rinish vaqti — panelda «kassa tirikmi» ni ko'rsatadi.
        # Har so'rovda yozish ortiqcha, daqiqada bir marta yetadi.
        now = timezone.now()
        if not register.last_seen_at or (now - register.last_seen_at).total_seconds() > 60:
            Register.objects.filter(pk=register.pk).update(last_seen_at=now)

        # Kassa o'z versiyasini har so'rovda aytadi — panelda «kim
        # eskirgan» ko'rinadi. Faqat o'zgarganda yozamiz.
        ver = (request.headers.get("X-Kassa-Version") or "").strip()[:32]
        if ver and ver != register.app_version:
            Register.objects.filter(pk=register.pk).update(app_version=ver)
            register.app_version = ver

        return view(request, *args, **kwargs)

    return wrapper
