"""
Kassa versiyalarini GitHub'dan o'zi olib keladi.

Kassa (frontend) kodi alohida repo'da — `sevimli-kassa-pos`. Kod push
qilinganda GitHub Actions Windows'da EXE yig'ib, ZIP'ni GitHub Release'ga
(`v1.16.0`) qo'yadi. Hub shu yerdan ZIP'ni O'ZI olib, panelning
«Versiyalar» ro'yxatiga qo'shadi — kassalar 30 daqiqada yangilanadi.

Nega hub tortadi, GitHub yuklamaydi: yuklash uchun GitHub'ga maxfiy kalit
berish kerak bo'lardi. Tortishda hech qanday kalit yo'q — repo ochiq,
Release'lar hammaga o'qiladi. Egasi hech narsa qilmaydi.

Qachon: kassalar `hello` yuborganda (`healer.tick` → 10 daqiqada bir).
Faqat `hub` xizmatida ishlaydi — ZIP fayllar uning doimiy diskida
(MEDIA_ROOT) turadi.
"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.files import File

from .models import KassaRelease, version_key

logger = logging.getLogger(__name__)

#: GitHub'ni shunchalik tez-tez so'raymiz (soniya)
CHECK_EVERY = 10 * 60
ASSET_NAME = "SevimliKassa.zip"
#: Dastur ZIP'i shundan kichik bo'lmaydi (yarim yuklangan/xato fayl himoyasi)
MIN_SIZE = 1_000_000
TIMEOUT = 20


def repo() -> str:
    return (getattr(settings, "KASSA_GITHUB_REPO", "") or "").strip().strip("/")


def latest_on_github(session=None) -> dict | None:
    """GitHub'dagi eng so'nggi Release: {"version", "url", "size", "notes"}.
    Release yo'q / repo yopiq / tarmoq yo'q — None."""
    name = repo()
    if not name:
        return None
    http = session or requests
    r = http.get(
        f"https://api.github.com/repos/{name}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "sevimli-kassa-hub"},
        timeout=TIMEOUT,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    tag = (data.get("tag_name") or "").strip()
    m = re.match(r"^v?(\d+\.\d+\.\d+)$", tag)
    if not m:
        return None
    asset = next(
        (a for a in data.get("assets") or [] if a.get("name") == ASSET_NAME), None
    )
    if not asset or not asset.get("browser_download_url"):
        return None
    notes = (data.get("body") or data.get("name") or "").strip().splitlines()
    return {
        "version": m.group(1),
        "url": asset["browser_download_url"],
        "size": int(asset.get("size") or 0),
        "notes": (notes[0] if notes else "")[:500],
    }


def import_latest(session=None) -> KassaRelease | None:
    """GitHub'da panelda yo'q YANGI versiya bo'lsa — yuklab olib, ro'yxatga
    qo'shadi. Qaytaradi: yangi yozuv yoki None (yangi narsa yo'q)."""
    info = latest_on_github(session)
    if not info:
        return None
    version = info["version"]
    if KassaRelease.objects.filter(version=version).exists():
        return None
    latest = KassaRelease.latest()
    if latest and version_key(version) <= latest.key:
        return None
    if info["size"] and info["size"] < MIN_SIZE:
        logger.warning("GitHub'dagi %s ZIP'i juda kichik (%s b) — olinmadi", version, info["size"])
        return None

    http = session or requests
    logger.info("GitHub'dan yangi kassa versiyasi olinmoqda: %s", version)
    digest = hashlib.sha256()
    size = 0
    with tempfile.TemporaryFile() as tmp:
        with http.get(info["url"], stream=True, timeout=TIMEOUT,
                      headers={"User-Agent": "sevimli-kassa-hub"}) as r:
            r.raise_for_status()
            for chunk in r.iter_content(256 * 1024):
                if not chunk:
                    continue
                tmp.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if size < MIN_SIZE:
            logger.warning("GitHub'dan %s: fayl chala (%s b) — olinmadi", version, size)
            return None
        # Yuklab olingandan keyin ham yangi bo'lishi kerak (ikki jarayon
        # bir vaqtda tortgan bo'lishi mumkin)
        if KassaRelease.objects.filter(version=version).exists():
            return None
        tmp.seek(0)
        rel = KassaRelease(
            version=version, notes=info["notes"] or f"GitHub Release v{version}",
            size=size, sha256=digest.hexdigest(),
        )
        rel.file.save(f"SevimliKassa-{version}.zip", File(tmp), save=True)
    logger.warning("Yangi kassa versiyasi GitHub'dan olindi: %s (%s MB)", version, size // 1_000_000)
    return rel


def check(session=None) -> KassaRelease | None:
    """`healer` chaqiradi: 10 daqiqada bir martadan ko'p emas, xatolar
    yutiladi (savdoga ta'sir qilmasin)."""
    if not repo():
        return None
    try:
        if not cache.add("releases:github", 1, CHECK_EVERY):
            return None
    except Exception:
        return None
    try:
        return import_latest(session)
    except Exception as e:
        logger.warning("GitHub Release tekshiruvi bo'lmadi: %s", e)
        return None
