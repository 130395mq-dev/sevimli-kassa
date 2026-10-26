"""
Sevimli HR — davomat, FAQAT O'QISH.

HR'dagi maxsus API (api/kpi.py, HR repozitoriyasida):
  * GET /api/kpi/xodimlar/ — faol xodimlar (ID, ism, lavozim, filial, smena);
  * GET /api/kpi/davomat/?dan=…&gacha=…&xodim=1,2 — har kun: amaldagi smena
    rejasi, kirish/chiqish, dam kuni, kechirilgan kechikish/erta ketish.
Kirish: `Authorization: Bearer <kalit>` (HR'da KPI_API_TOKEN, bu yerda
SEVIMLI_HR_TOKEN — bir xil qiymat). Maosh, jarima, telefon, rasm — so'ralmaydi
va HR ularni bermaydi.

Xavfsizlik:
  * kalit sozlanmaguncha HR'ga birorta ham so'rov yuborilmaydi;
  * kalit hech qachon xato matniga yoki logga yozilmaydi;
  * javob mahalliy jadvalga (HrDavomat) nusxalanadi — KPI shu nusxadan
    hisoblanadi, HR vaqtincha ishlamasa ham eski kunlar yo'qolmaydi.
"""

from __future__ import annotations

from datetime import date, timedelta

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import ms
from .models import HrDavomat, HrXodim, Tovaroved

TIMEOUT = (5, 20)
OYNA_KUN = 7          # har safar oxirgi 7 kun qayta olinadi (HR'da kechroq tuzatiladi)
BOSHLANG_KUN = 31     # yangi bog'langan xodim uchun bir martalik orqaga to'ldirish


class HrError(Exception):
    """HR bilan aloqa xatosi — matnida kalit bo'lmaydi."""


def configured() -> bool:
    return bool(getattr(settings, "SEVIMLI_HR_TOKEN", "") and getattr(settings, "SEVIMLI_HR_URL", ""))


def _get(path: str, **params) -> dict:
    url = settings.SEVIMLI_HR_URL.rstrip("/") + path
    headers = {"Authorization": f"Bearer {settings.SEVIMLI_HR_TOKEN}", "Accept": "application/json",
               "User-Agent": "SevimliKassa-KPI/0.1"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise HrError(f"Sevimli HR javob bermadi ({type(exc).__name__})") from None
    if r.status_code == 401:
        raise HrError("Sevimli HR kalitni qabul qilmadi (401) — ikki tomondagi kalit bir xilmi?")
    if r.status_code == 404:
        raise HrError("Sevimli HR'da bu API yo'q yoki yoqilmagan (404) — HR'da KPI_API_TOKEN sozlanganmi?")
    if r.status_code != 200:
        raise HrError(f"Sevimli HR xatosi: {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise HrError("Sevimli HR noto'g'ri javob berdi (JSON emas)") from None
    if not isinstance(data, dict):
        raise HrError("Sevimli HR noto'g'ri javob berdi")
    return data


def _dt(value):
    if not value:
        return None
    d = parse_datetime(str(value))
    if d is None:
        return None
    if timezone.is_naive(d):
        d = d.replace(tzinfo=ms.TASHKENT)
    return d


def _s(value, n: int = 255) -> str:
    return str(value or "").strip()[:n]


def sync_xodimlar() -> int:
    """HR'dagi faol xodimlar ro'yxati (bog'lash sahifasidagi tanlov uchun)."""
    data = _get("/api/kpi/xodimlar/")
    seen = set()
    for x in data.get("xodimlar") or []:
        hid = _s(x.get("id"), 64)
        if not hid:
            continue
        HrXodim.objects.update_or_create(hr_id=hid, defaults={
            "ism": _s(x.get("ism")) or hid, "lavozim": _s(x.get("lavozim")),
            "filial": _s(x.get("filial")), "smena": _s(x.get("smena")), "faol": True,
        })
        seen.add(hid)
    HrXodim.objects.exclude(hr_id__in=seen).update(faol=False)
    return len(seen)


def linked_ids() -> list[str]:
    ids = {(t.hr_xodim_id or "").strip()
           for t in Tovaroved.objects.filter(active=True).exclude(hr_xodim_id="")}
    return sorted(i for i in ids if i.isdigit())


_FIELDS = ("smena", "reja_boshi", "reja_oxiri", "kirish", "chiqish", "dam_kuni",
           "kechikish_kechirilgan", "erta_ketish_kechirilgan", "holat")


def _save(rows) -> set[date]:
    changed = set()
    for row in rows or []:
        hid = _s(row.get("xodim"), 64)
        try:
            sana = date.fromisoformat(str(row.get("sana")))
        except ValueError:
            continue
        if not hid:
            continue
        vals = {
            "smena": _s(row.get("smena")),
            "reja_boshi": _dt(row.get("reja_boshi")), "reja_oxiri": _dt(row.get("reja_oxiri")),
            "kirish": _dt(row.get("kirish")), "chiqish": _dt(row.get("chiqish")),
            "dam_kuni": bool(row.get("dam_kuni")),
            "kechikish_kechirilgan": bool(row.get("kechikish_kechirilgan")),
            "erta_ketish_kechirilgan": bool(row.get("erta_ketish_kechirilgan")),
            "holat": _s(row.get("holat"), 20),
        }
        obj = HrDavomat.objects.filter(hr_xodim_id=hid, sana=sana).first()
        if obj is None:
            HrDavomat.objects.create(hr_xodim_id=hid, sana=sana, **vals)
            changed.add(sana)
        elif any(getattr(obj, k) != vals[k] for k in _FIELDS):
            for k in _FIELDS:
                setattr(obj, k, vals[k])
            obj.save()
            changed.add(sana)
    return changed


def sync_davomat(now=None) -> set[date]:
    """Bog'langan tovarovedlar davomati. O'zgargan kunlar qaytariladi
    (ular KPI'da qayta hisoblanadi)."""
    now = now or timezone.now()
    ids = linked_ids()
    if not ids:
        return set()
    today = ms.local_date(now)
    have = set(HrDavomat.objects.filter(hr_xodim_id__in=ids).values_list("hr_xodim_id", flat=True).distinct())
    changed = set()
    for group, days in ((sorted(set(ids) - have), BOSHLANG_KUN), (sorted(set(ids) & have), OYNA_KUN)):
        if not group:
            continue
        dan = today - timedelta(days=days - 1)
        data = _get("/api/kpi/davomat/", dan=dan.isoformat(), gacha=today.isoformat(), xodim=",".join(group))
        changed |= _save(data.get("davomat"))
    return changed
