"""
Qayta minus: tuzatilgan (yo'qolgan) minus belgilangan kun ichida yana
minusga tushgan tovarlar ro'yxati — kim mas'ul, qachon tuzatildi, qachon
qaytdi, hozir qanday.

Hisob reyting sahifasidagi «N tasi … kunda yana minusga tushdi» bilan
AYNAN bir xil: davrda yopilgan, `reopened` belgili, «tovaroved xatosi
emas» deb tasdiqlanmagan minuslar; do'konlar — faol tovarovedlarning
hozirgi do'konlari.
"""

from __future__ import annotations

import csv
from datetime import timedelta
from io import StringIO

from django.utils import timezone

from .models import MinusIssue


def gap_text(delta: timedelta | None) -> str:
    """«5 soatdan keyin», «1 kun 3 soatdan keyin», «40 daqiqadan keyin»."""
    if delta is None:
        return ""
    minutes = max(0, int(delta.total_seconds() // 60))
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    if days:
        text = f"{days} kun" + (f" {hours} soat" if hours else "")
    elif hours:
        text = f"{hours} soat"
    else:
        text = f"{mins} daqiqa"
    return text + "dan keyin"


def rows(stores: dict, s, e, dokon: str = "", tov_id: int | None = None) -> list[dict]:
    """`stores` — {warehouse_ms_id: nomi}; `dokon` ulardan biri bo'lsa
    faqat o'sha do'kon. Eng oxirgi tuzatilgani birinchi."""
    whs = [dokon] if dokon in stores else list(stores)
    qs = (MinusIssue.objects.filter(warehouse_ms_id__in=whs, reopened=True,
                                    closed_at__gte=s, closed_at__lt=e)
          .exclude(confirmed_cause="xato_emas")
          .select_related("tovaroved"))
    if tov_id:
        qs = qs.filter(tovaroved_id=tov_id)
    issues = list(qs.order_by("-closed_at", "-pk"))
    # Qayta paydo bo'lgan minus — avvalgisiga `previous` bilan bog'langan
    again: dict[int, MinusIssue] = {}
    for nxt in MinusIssue.objects.filter(previous_id__in=[i.pk for i in issues]).order_by("first_seen", "pk"):
        again.setdefault(nxt.previous_id, nxt)
    out = []
    for i in issues:
        a = again.get(i.pk)
        out.append({
            "i": i,
            "again": a,
            "gap": gap_text(a.first_seen - i.closed_at) if a and i.closed_at else "",
        })
    return out


def _when(dt) -> str:
    return timezone.localtime(dt).strftime("%d.%m.%Y %H:%M") if dt else ""


def _text(value: str) -> str:
    """Raqamli kod Excel'da «4,78E+12» bo'lib qolmasin, boshidagi nol yo'qolmasin."""
    return f'="{value}"' if value and value.isdigit() else (value or "")


def _qty(value) -> str:
    if value is None:
        return ""
    return f"{value.normalize():f}".replace(".", ",")


def csv_text(data: list[dict], title: str) -> str:
    """Excel ochadigan «;» ajratgichli CSV."""
    buf = StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([title])
    w.writerow(["Do'kon", "Mas'ul", "Kodi", "Shtrix kodi", "Tovar", "Birinchi minus", "Eng past qoldiq",
                "Tuzatildi", "Qanday tuzatildi", "Yana minus", "Oraliq", "Hozir", "Hozirgi qoldiq"])
    for r in data:
        i, a = r["i"], r["again"]
        if a is None:
            hozir = ""
        elif a.status == MinusIssue.OPEN:
            hozir = "hozir ham minusda"
        else:
            hozir = "yana yo'qoldi"
        w.writerow([
            i.warehouse_name, i.tovaroved.name if i.tovaroved else "", _text(i.code), _text(i.barcode), i.name,
            "kuzatuvdan oldin" if i.pre_tracking else _when(i.first_seen), _qty(i.min_qty),
            _when(i.closed_at), i.get_resolution_display() or "aniqlanmoqda",
            _when(a.first_seen) if a else "", r["gap"], hozir, _qty(a.last_qty) if a else "",
        ])
    return buf.getvalue()
