"""Fail-closed POS rollout: allow only explicitly selected registers/version."""

from django.conf import settings


def release_allowed(register, version: str) -> bool:
    mode = getattr(settings, "KASSA_UPDATE_MODE", "hold")
    target = getattr(settings, "KASSA_UPDATE_VERSION", "")
    if mode == "all":
        return not target or version == target
    if mode != "pilot" or not target or version != target:
        return False
    allowed = getattr(settings, "KASSA_UPDATE_REGISTER_IDS", "")
    ids = {part.strip() for part in allowed.split(",") if part.strip()}
    return str(register.pk) in ids


def latest_for(register):
    from .models import KassaRelease

    return max(
        (rel for rel in KassaRelease.objects.filter(active=True)
         if release_allowed(register, rel.version)),
        key=lambda rel: rel.key, default=None,
    )
