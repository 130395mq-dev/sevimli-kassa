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

import functools
import secrets

from django.http import JsonResponse
from django.utils import timezone

from sales.models import Register


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

        return view(request, *args, **kwargs)

    return wrapper
