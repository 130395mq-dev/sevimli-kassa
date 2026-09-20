"""«Batafsil» paneli — KPI kartasi bosilganda o'ngdan ochiladigan oyna.

Har karta uchun alohida mazmun: katta grafik, qisqa xulosalar va kesimlar
(filial, kassa, tovar). Sahifa ochilganda bu hisoblar QILINMAYDI — panel
bosilganda alohida so'rov bilan olinadi, shuning uchun bosh sahifa tez
qoladi.

Jadvallar bitta ko'rinishda: {title, cols, align, rows} — shablon hammasini
bitta sikl bilan chizadi, yangi kesim qo'shish uchun shablonga tegish
shart emas.

CSV: har panelning ASOSIY jadvali `?format=csv` bilan yuklab olinadi.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import timedelta
from io import StringIO

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from dashboard import grafik, savdo
from sales.models import PanelSettings, Payment, Register, Sale, SaleItem

#: Qaysi kartalar ochiladi (URL dagi ?detail= qiymati)
KPIS = ("savdo", "cheklar", "ortacha", "tolov", "qaytarish")

TITLES = {
    "savdo": "Savdo",
    "cheklar": "Cheklar",
    "ortacha": "O'rtacha chek",
    "tolov": "Naqd / naqdsiz",
    "qaytarish": "Qaytarish",
}


def _table(title, cols, rows, align=None, note=""):
    """Jadval: shablon uchun har katak o'z tekislanishini olib yuradi.

    align kodlari: «l» — matn (chapda), «n» — son (o'ngda), «m» — pul
    (o'ngda, so'm formatida). Shablon bitta sikl bilan chizadi.
    """
    align = align or ["l"] * len(cols)
    cells = [
        [{"v": v, "a": align[i] if i < len(align) else "l"}
         for i, v in enumerate(row)]
        for row in rows
    ]
    return {
        "title": title, "note": note,
        "cols": list(zip(cols, align)), "rows": cells,
        # CSV uchun — formatlanmagan holida
        "raw_cols": list(cols), "raw_rows": [list(r) for r in rows],
    }


# ------------------------------------------------------------- umumiy ramka


def _frame(params) -> dict:
    """Hamma panel uchun umumiy: davr, oldingi davr, qatorlar, kesimlar."""
    start, end, preset = savdo.parse_range(params)
    span = (end - start).days + 1
    prev_start = start - timedelta(days=span)
    prev_end = start - timedelta(days=1)

    a, b = savdo._bounds(start, end)
    pa, pb = savdo._bounds(prev_start, prev_end)
    qs = Sale.objects.filter(created_at__gte=a, created_at__lt=b)
    prev_qs = Sale.objects.filter(created_at__gte=pa, created_at__lt=pb)

    cur = savdo._totals(qs)
    prev = savdo._totals(prev_qs)

    # Bir kunlik davr — soatlar bo'yicha; uzunroq — kunlar bo'yicha
    by_hour = span == 1
    if by_hour:
        h_cur = savdo._hourly(start, end)
        h_prev = savdo._hourly(prev_start, prev_end)
        labels = [x["label"] for x in h_cur["hours"]]
        cur_rows = h_cur["hours"]
        prev_rows = h_prev["hours"]
    else:
        cur_rows = savdo._spark(end, days=span)
        prev_rows = savdo._spark(prev_end, days=span)
        labels = [d["date"].strftime("%d.%m") for d in cur_rows]

    return {
        "start": start, "end": end, "span": span, "preset": preset,
        "prev_start": prev_start, "prev_end": prev_end,
        "label": savdo.period_label(start, end),
        "prev_label": savdo.period_label(prev_start, prev_end),
        "qs": qs, "prev_qs": prev_qs, "cur": cur, "prev": prev,
        "by_hour": by_hour, "labels": labels,
        "rows": cur_rows, "prev_rows": prev_rows,
    }


def _registers(f, field: str = "total"):
    """Kassalar kesimi: [{name, point, total, receipts, avg, returns}]."""
    out = []
    for reg in Register.objects.filter(active=True, archived=False).select_related("store"):
        t = savdo._totals(f["qs"].filter(shift__register=reg))
        out.append({"name": reg.name, "point": reg.point_name, **t})
    return sorted(out, key=lambda r: -r[field])


def _points(f, field: str = "total"):
    """Filiallar (nuqtalar) kesimi."""
    acc: dict[str, dict] = {}
    for r in _registers(f):
        p = acc.setdefault(r["point"], {
            "name": r["point"], "total": 0.0, "receipts": 0, "cash": 0.0,
            "cashless": 0.0, "returns": 0.0, "returns_n": 0,
        })
        for k in ("total", "receipts", "cash", "cashless", "returns", "returns_n"):
            p[k] += r[k]
    rows = sorted(acc.values(), key=lambda r: -r[field])
    for r in rows:
        r["avg"] = (r["total"] / r["receipts"]) if r["receipts"] else 0
    return rows


def _compare_chart(f, key: str, money: bool = True):
    """Joriy va oldingi davr bitta grafikda (bir xil o'lchov, bitta o'q)."""
    return grafik.chart_lines(
        [
            {"name": f["label"], "values": [r[key] for r in f["rows"]],
             "area": True, "dash": False},
            {"name": f["prev_label"], "values": [r[key] for r in f["prev_rows"]],
             "area": False, "dash": True},
        ],
        f["labels"], money=money,
    )


# ----------------------------------------------------------------- savdo


def _savdo(f) -> dict:
    peak, low = savdo._peak_low(f["rows"])
    findings = [
        {"label": "Oldingi davr", "value": f["prev"]["total"], "money": True,
         "note": f["prev_label"]},
        {"label": "O'zgarish", "percent": savdo._delta(f["cur"]["total"], f["prev"]["total"])},
    ]
    if peak:
        findings.append({"label": "Eng yuqori kun" if not f["by_hour"] else "Eng yuqori soat",
                         "value": peak["total"], "money": True,
                         "note": peak.get("label") or peak["date"].strftime("%d.%m.%Y")})
    if low:
        findings.append({"label": "Eng past kun" if not f["by_hour"] else "Eng past soat",
                         "value": low["total"], "money": True,
                         "note": low.get("label") or low["date"].strftime("%d.%m.%Y")})

    points = _points(f)
    regs = _registers(f)
    top = savdo.top_products(f["start"], f["end"])
    tables = [
        _table("Filiallar bo'yicha", ["Filial", "Savdo", "Chek", "O'rtacha chek"],
               [[p["name"], p["total"], p["receipts"], p["avg"]] for p in points],
               ["l", "m", "n", "m"]),
        _table("Kassalar bo'yicha", ["Kassa", "Filial", "Savdo", "Chek"],
               [[r["name"], r["point"], r["total"], r["receipts"]] for r in regs],
               ["l", "l", "m", "n"]),
        _table("Top mahsulotlar", ["Mahsulot", "Summa", "Soni", "Chek"],
               [[t["label"], t["value"], round(t["qty"], 3), t["n"]] for t in top],
               ["l", "m", "n", "n"]),
    ]
    return {
        "value": f["cur"]["total"], "money": True,
        "chart": _compare_chart(f, "total"),
        "chart_title": "Savdo dinamikasi" + (" (soatlar)" if f["by_hour"] else " (kunlar)"),
        "unit": "so'm",
        "findings": findings, "tables": tables,
    }


# ---------------------------------------------------------------- cheklar


def _cheklar(f) -> dict:
    hourly = savdo._hourly(f["start"], f["end"])
    busy = savdo._busy_window([h["n"] for h in hourly["hours"]])
    per_item = savdo.items_per_receipt(f["start"], f["end"])
    findings = [
        {"label": "Oldingi davr", "value": f["prev"]["receipts"], "note": f["prev_label"]},
        {"label": "O'zgarish",
         "percent": savdo._delta(f["cur"]["receipts"], f["prev"]["receipts"])},
        {"label": "Bir chekda o'rtacha", "value": round(per_item, 1), "note": "dona mahsulot"},
    ]
    if busy:
        findings.insert(0, {"label": "Eng faol vaqt", "value": busy["n"],
                            "note": busy["label"] + " · chek"})

    regs = _registers(f, "receipts")
    points = _points(f, "receipts")
    last = (
        Sale.objects.filter(kind=Sale.SALE, created_at__gte=savdo._bounds(f["start"], f["end"])[0],
                            created_at__lt=savdo._bounds(f["start"], f["end"])[1])
        .select_related("shift__register__store").order_by("-created_at")[:20]
    )
    tables = [
        _table("Kassalar bo'yicha", ["Kassa", "Filial", "Chek", "Savdo"],
               [[r["name"], r["point"], r["receipts"], r["total"]] for r in regs],
               ["l", "l", "n", "m"]),
        _table("Filiallar bo'yicha", ["Filial", "Chek", "Savdo"],
               [[p["name"], p["receipts"], p["total"]] for p in points],
               ["l", "n", "m"]),
        _table("Oxirgi cheklar", ["№", "Vaqt", "Kassa", "Summa"],
               [[s.number, s.created_at.astimezone().strftime("%d.%m %H:%M"),
                 s.shift.register.name, s.net_total / 100] for s in last],
               ["n", "l", "l", "m"]),
    ]
    return {
        "value": f["cur"]["receipts"], "money": False,
        "chart": grafik.chart_bars([h["n"] for h in hourly["hours"]],
                                   [h["label"] for h in hourly["hours"]], money=False),
        "chart_kind": "bars",
        "chart_title": "Soatlar bo'yicha cheklar", "unit": "dona",
        "findings": findings, "tables": tables,
    }


# ----------------------------------------------------------- o'rtacha chek


def _ortacha(f) -> dict:
    target = PanelSettings.get().avg_receipt_target / 100
    cur = f["cur"]["avg"]
    peak, low = savdo._peak_low(f["rows"], "avg")
    findings = [
        {"label": "Oldingi davr", "value": f["prev"]["avg"], "money": True,
         "note": f["prev_label"]},
        {"label": "O'zgarish", "percent": savdo._delta(cur, f["prev"]["avg"])},
    ]
    if target:
        findings.insert(0, {"label": "Maqsad", "value": target, "money": True,
                            "note": f"bajarilgan {round(cur / target * 100)}%"})
        findings.insert(1, {"label": "Maqsadgacha", "value": max(0.0, target - cur),
                            "money": True,
                            "note": "maqsadga yetdi" if cur >= target else "qoldi"})
    if peak:
        findings.append({"label": "Eng yuqori", "value": peak["avg"], "money": True,
                         "note": peak.get("label") or peak["date"].strftime("%d.%m.%Y")})
    if low:
        findings.append({"label": "Eng past", "value": low["avg"], "money": True,
                         "note": low.get("label") or low["date"].strftime("%d.%m.%Y")})

    series = [
        {"name": f["label"], "values": [r["avg"] for r in f["rows"]], "area": True},
        {"name": f["prev_label"], "values": [r["avg"] for r in f["prev_rows"]], "dash": True},
    ]
    if target:
        series.append({"name": "Maqsad", "values": [target] * len(f["labels"]),
                       "dash": True, "goal": True})
    points = _points(f)
    regs = _registers(f)
    tables = [
        _table("Filiallar bo'yicha", ["Filial", "O'rtacha chek", "Chek", "Savdo"],
               [[p["name"], p["avg"], p["receipts"], p["total"]] for p in points],
               ["l", "m", "n", "m"]),
        _table("Kassalar bo'yicha", ["Kassa", "O'rtacha chek", "Chek"],
               [[r["name"], r["avg"], r["receipts"]] for r in regs],
               ["l", "m", "n"]),
    ]
    return {
        "value": cur, "money": True, "target": target,
        "chart": grafik.chart_lines(series, f["labels"]),
        "chart_title": "O'rtacha chek dinamikasi", "unit": "so'm",
        "findings": findings, "tables": tables, "show_target_form": True,
    }


# ----------------------------------------------------------------- to'lov


def _payments_chart(f):
    """Kunlar bo'yicha naqd va naqdsiz. Bir kunlik davrda grafik ko'rsatilmaydi
    — bitta nuqtadan chiziq chiqmaydi, donut allaqachon hammasini aytadi."""
    if f["by_hour"]:
        return None
    a, b = savdo._bounds(f["start"], f["end"])
    tz = timezone.get_current_timezone()
    rows = (
        Payment.objects.filter(sale__kind=Sale.SALE, sale__created_at__gte=a,
                               sale__created_at__lt=b)
        .annotate(day=TruncDate("sale__created_at", tzinfo=tz))
        .values("day", "method__is_cash")
        .annotate(total=Sum("amount"))
        .order_by()
    )
    cash: dict = defaultdict(float)
    cashless: dict = defaultdict(float)
    for r in rows:
        bucket = cash if r["method__is_cash"] else cashless
        bucket[r["day"]] += (r["total"] or 0) / 100
    days = [x["date"] for x in f["rows"]]
    return grafik.chart_lines(
        [{"name": "Naqd", "values": [cash.get(d, 0.0) for d in days], "area": True},
         {"name": "Naqdsiz", "values": [cashless.get(d, 0.0) for d in days]}],
        f["labels"],
    )


def _tolov(f) -> dict:
    sales = f["qs"].filter(kind=Sale.SALE)
    rows = (
        Payment.objects.filter(sale__in=sales)
        .values("method__name", "method__is_cash")
        .annotate(total=Sum("amount"), n=Count("id"))
        .order_by("-total")
    )
    methods = [
        {"label": r["method__name"], "value": (r["total"] or 0) / 100,
         "is_cash": r["method__is_cash"], "n": r["n"]}
        for r in rows
    ]
    total = sum(m["value"] for m in methods)
    for m in methods:
        m["percent"] = round(m["value"] / total * 100, 1) if total else 0

    cash = sum(m["value"] for m in methods if m["is_cash"])
    findings = [
        {"label": "Naqd", "value": cash, "money": True,
         "note": f"{round(cash / total * 100)}%" if total else "—"},
        {"label": "Naqdsiz", "value": total - cash, "money": True,
         "note": f"{round((total - cash) / total * 100)}%" if total else "—"},
        {"label": "Jami to'lov", "value": total, "money": True},
    ]
    points = _points(f)
    tables = [
        _table("To'lov turlari", ["Tur", "Summa", "Ulush", "Chek"],
               [[m["label"], m["value"], f"{m['percent']}%", m["n"]] for m in methods],
               ["l", "m", "n", "n"]),
        _table("Filiallar bo'yicha", ["Filial", "Naqd", "Naqdsiz", "Jami"],
               [[p["name"], p["cash"], p["cashless"], p["total"]] for p in points],
               ["l", "m", "m", "m"]),
    ]
    return {
        "value": total, "money": True,
        "donut": grafik.donut([{"label": m["label"], "value": m["value"]} for m in methods],
                              size=190, thickness=30),
        "donut_parts": methods,
        "chart": _payments_chart(f),
        "chart_title": "Kunlar bo'yicha to'lov turlari", "unit": "so'm",
        "findings": findings, "tables": tables,
    }


# ------------------------------------------------------------- qaytarish


def _qaytarish(f) -> dict:
    cur = f["cur"]
    share = round(cur["returns"] / cur["total"] * 100, 2) if cur["total"] else 0
    findings = [
        {"label": "Qaytarilgan chek", "value": cur["returns_n"]},
        {"label": "Savdoga nisbatan", "value": share, "percent_value": True},
        {"label": "Oldingi davr", "value": f["prev"]["returns"], "money": True,
         "note": f["prev_label"]},
        {"label": "O'zgarish", "percent": savdo._delta(cur["returns"], f["prev"]["returns"]),
         "worse_is_up": True},
    ]
    top = savdo.top_products(f["start"], f["end"], kind=Sale.RETURN)
    regs = _registers(f, "returns")
    a, b = savdo._bounds(f["start"], f["end"])
    items = (
        Sale.objects.filter(kind=Sale.RETURN, created_at__gte=a, created_at__lt=b)
        .select_related("shift__register__store").order_by("-created_at")[:30]
    )
    tables = [
        _table("Eng ko'p qaytarilgan mahsulotlar", ["Mahsulot", "Summa", "Soni"],
               [[t["label"], t["value"], round(t["qty"], 3)] for t in top],
               ["l", "m", "n"]),
        _table("Kassalar bo'yicha", ["Kassa", "Filial", "Qaytarish", "Chek"],
               [[r["name"], r["point"], r["returns"], r["returns_n"]] for r in regs],
               ["l", "l", "m", "n"]),
        _table("Qaytarish cheklari", ["Sana", "№", "Kassa", "Summa"],
               [[s.created_at.astimezone().strftime("%d.%m %H:%M"), s.number,
                 s.shift.register.name, s.net_total / 100] for s in items],
               ["l", "n", "l", "m"],
               note="Qaytarish sababi saqlanmaydi — kassa dasturi hozir sabab so'ramaydi."),
    ]
    return {
        "value": cur["returns"], "money": True,
        "chart": grafik.chart_bars([r["returns"] for r in f["rows"]], f["labels"]),
        "chart_kind": "bars", "danger": True,
        "chart_title": "Qaytarishlar", "unit": "so'm",
        "findings": findings, "tables": tables,
    }


BUILDERS = {
    "savdo": _savdo, "cheklar": _cheklar, "ortacha": _ortacha,
    "tolov": _tolov, "qaytarish": _qaytarish,
}


def build(kpi: str, params) -> dict:
    """Bitta karta uchun batafsil panel mazmuni."""
    if kpi not in BUILDERS:
        raise KeyError(kpi)
    f = _frame(params)
    data = BUILDERS[kpi](f)
    data.update({
        "kpi": kpi, "title": TITLES[kpi], "label": f["label"],
        "prev_label": f["prev_label"], "presets": savdo.PRESETS,
        "preset": f["preset"], "start": f["start"], "end": f["end"],
        "empty": not f["cur"]["receipts"] and not f["cur"]["returns_n"],
    })
    return data


def csv_response(kpi: str, params) -> tuple[str, str]:
    """(fayl nomi, CSV matni) — panelning asosiy jadvali."""
    data = build(kpi, params)
    table = data["tables"][0] if data["tables"] else _table("", [], [])
    buf = StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([f"{data['title']} — {data['label']}"])
    w.writerow(table["raw_cols"])
    for row in table["raw_rows"]:
        w.writerow([f"{v:.0f}".replace(".", ",") if isinstance(v, float) else v
                    for v in row])
    name = f"sevimli-{kpi}-{data['start']:%Y%m%d}-{data['end']:%Y%m%d}.csv"
    return name, buf.getvalue()
