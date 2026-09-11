"""Bosh sahifa dashboardi: davr bo'yicha savdo, nuqtalar reytingi, kunlik
ko'rsatkichlar.

Do'kon egasining savoli: «qaysi nuqta yaxshi sotyapti, kunlik ko'rsatkichlar
qanday?» — va o'tgan kunlarni sana bilan ko'rish. Hamma hisob shu yerda,
view faqat so'rovni o'qiydi va shablonga uzatadi.

Davr: `?davr=bugun|kecha|7|30` yoki `?dan=YYYY-MM-DD&gacha=YYYY-MM-DD`.
Kun chegarasi — mahalliy (Asia/Tashkent) vaqt bo'yicha.

Nuqta = kassa qaysi ombordan sotsa o'sha (`Register.point_name`). Bir nuqtada
bir necha kassa bo'lishi mumkin — ular qo'shiladi.

Solishtirish: xuddi shu uzunlikdagi OLDINGI davr (bugun ↔ kecha, 7 kun ↔
undan oldingi 7 kun). Farq foizda; oldingi davr nol bo'lsa farq ko'rsatilmaydi.

Grafik: kunlik jami savdo — bitta qator, brend yashil. Davr 7 kundan qisqa
bo'lsa (masalan «bugun») grafik va kunlik jadval oxirgi 14 kunni ko'rsatadi
(tanlangan kun ajratib), aks holda tanlangan davrni.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_date

from sales.models import Payment, Register, Sale, Shift

#: Bir so'rovda eng ko'pi shuncha kun (jadval o'qiladigan bo'lsin)
MAX_DAYS = 92
#: Grafik uchun kamida shuncha kun ko'rsatiladi
CHART_MIN_DAYS = 14

PRESETS = [("bugun", "Bugun"), ("kecha", "Kecha"), ("7", "7 kun"), ("30", "30 kun")]
#: Hafta kunlari — o'zbekcha qisqa (Django'niki ruscha chiqadi)
DOW = ["Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya"]


# ------------------------------------------------------------------ davr


def parse_range(params) -> tuple[date, date, str]:
    """So'rovdan davrni o'qiydi. Qaytaradi: (boshi, oxiri, preset nomi yoki '')."""
    today = timezone.localdate()
    preset = (params.get("davr") or "").strip()
    if preset == "kecha":
        d = today - timedelta(days=1)
        return d, d, preset
    if preset in ("7", "30"):
        n = int(preset)
        return today - timedelta(days=n - 1), today, preset
    dan = parse_date(params.get("dan") or "") if params.get("dan") else None
    gacha = parse_date(params.get("gacha") or "") if params.get("gacha") else None
    if dan or gacha:
        start = dan or gacha
        end = gacha or dan
        if end < start:
            start, end = end, start
        if (end - start).days >= MAX_DAYS:
            start = end - timedelta(days=MAX_DAYS - 1)
        preset = "bugun" if start == end == today else ""
        return start, end, preset
    return today, today, "bugun"


def _bounds(start: date, end: date):
    tz = timezone.get_current_timezone()
    a = timezone.make_aware(datetime.combine(start, time.min), tz)
    b = timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz)
    return a, b


def period_label(start: date, end: date) -> str:
    if start == end:
        return start.strftime("%d.%m.%Y")
    return f"{start:%d.%m.%Y} — {end:%d.%m.%Y}"


# ----------------------------------------------------------------- hisob


def _delta(cur: float, prev: float):
    """Foizli farq. Oldingi davr nol bo'lsa — None (solishtirib bo'lmaydi)."""
    if not prev:
        return None
    return round((cur - prev) / prev * 100)


def _totals(qs) -> dict:
    """Bitta to'plam uchun: savdo, chek soni, qaytarish, naqd/naqdsiz (so'mda)."""
    sales = qs.filter(kind=Sale.SALE)
    agg = sales.aggregate(n=Count("id"), total=Sum("net_total"))
    ret = qs.filter(kind=Sale.RETURN).aggregate(n=Count("id"), total=Sum("net_total"))
    cash = (
        Payment.objects.filter(sale__in=sales, method__is_cash=True)
        .aggregate(t=Sum("amount"))["t"] or 0
    )
    total = agg["total"] or 0
    n = agg["n"] or 0
    return {
        "total": total / 100,
        "receipts": n,
        "avg": (total / n / 100) if n else 0,
        "cash": cash / 100,
        "cashless": (total - cash) / 100,
        "returns": (ret["total"] or 0) / 100,
        "returns_n": ret["n"] or 0,
    }


def build(params, now=None) -> dict:
    """Bosh sahifa uchun hamma raqamlar."""
    now = now or timezone.now()
    start, end, preset = parse_range(params)
    span = (end - start).days + 1
    a, b = _bounds(start, end)
    prev_start, prev_end = start - timedelta(days=span), start - timedelta(days=1)
    pa, pb = _bounds(prev_start, prev_end)

    period_qs = Sale.objects.filter(created_at__gte=a, created_at__lt=b)
    prev_qs = Sale.objects.filter(created_at__gte=pa, created_at__lt=pb)

    # ---- umumiy
    cur = _totals(period_qs)
    prev = _totals(prev_qs)
    summary = dict(cur)
    summary["delta_total"] = _delta(cur["total"], prev["total"])
    summary["delta_receipts"] = _delta(cur["receipts"], prev["receipts"])
    summary["delta_avg"] = _delta(cur["avg"], prev["avg"])
    summary["prev_total"] = prev["total"]

    # ---- kassalar (davr bo'yicha) va nuqtalar
    registers = list(
        Register.objects.filter(active=True, archived=False).select_related("store")
    )
    open_ids = set(
        Shift.objects.filter(status=Shift.OPEN, register__in=registers)
        .values_list("register_id", flat=True)
    )
    kassas = []
    points: dict[str, dict] = {}
    for reg in registers:
        t = _totals(period_qs.filter(shift__register=reg))
        p = _totals(prev_qs.filter(shift__register=reg))
        pending = period_qs.filter(shift__register=reg).exclude(sync_status=Sale.SENT).count()
        kassas.append({
            "register": reg, "point": reg.point_name, "shift_open": reg.pk in open_ids,
            "pending": pending, **t, "delta_total": _delta(t["total"], p["total"]),
        })
        pt = points.setdefault(reg.point_name, {
            "name": reg.point_name, "total": 0.0, "receipts": 0, "cash": 0.0,
            "cashless": 0.0, "returns": 0.0, "prev_total": 0.0, "kassas": [],
        })
        pt["total"] += t["total"]
        pt["receipts"] += t["receipts"]
        pt["cash"] += t["cash"]
        pt["cashless"] += t["cashless"]
        pt["returns"] += t["returns"]
        pt["prev_total"] += p["total"]
        pt["kassas"].append(reg.name)

    ranking = sorted(points.values(), key=lambda x: (-x["total"], x["name"]))
    grand = sum(x["total"] for x in ranking) or 0
    best = ranking[0]["total"] if ranking else 0
    for i, pt in enumerate(ranking, start=1):
        pt["rank"] = i
        pt["avg"] = (pt["total"] / pt["receipts"]) if pt["receipts"] else 0
        pt["share"] = round(pt["total"] / grand * 100) if grand else 0
        pt["bar"] = round(pt["total"] / best * 100) if best else 0
        pt["delta_total"] = _delta(pt["total"], pt["prev_total"])
        pt["kassas"] = ", ".join(pt["kassas"])

    # ---- to'lov turlari (davr)
    by_method = (
        Payment.objects.filter(sale__in=period_qs.filter(kind=Sale.SALE))
        .values("method__name", "method__is_cash")
        .annotate(total=Sum("amount"), n=Count("id"))
        .order_by("-total")
    )
    methods = [
        {"name": m["method__name"], "is_cash": m["method__is_cash"],
         "total": (m["total"] or 0) / 100, "n": m["n"]}
        for m in by_method
    ]

    # ---- kunlik: davr qisqa bo'lsa — oxirgi 14 kun (tanlangan kun ajratib)
    if span >= 7:
        c_start, c_end = start, end
    else:
        c_end = end
        c_start = end - timedelta(days=CHART_MIN_DAYS - 1)
    daily = _daily(c_start, c_end, ranking, start, end)

    return {
        "start": start, "end": end, "span": span, "preset": preset,
        "label": period_label(start, end),
        "prev_label": period_label(prev_start, prev_end),
        "summary": summary,
        "ranking": ranking,
        "kassas": kassas,
        "methods": methods,
        "daily": daily,
        "presets": PRESETS,
    }


def _daily(c_start: date, c_end: date, ranking: list[dict], sel_start: date, sel_end: date) -> dict:
    """Kunlik jadval va grafik ma'lumoti."""
    a, b = _bounds(c_start, c_end)
    tz = timezone.get_current_timezone()
    rows_qs = (
        Sale.objects.filter(kind=Sale.SALE, created_at__gte=a, created_at__lt=b)
        .annotate(day=TruncDate("created_at", tzinfo=tz))
        .values("day", "shift__register")
        .annotate(total=Sum("net_total"), n=Count("id"))
    )
    reg_point = {
        r.pk: r.point_name
        for r in Register.objects.filter(active=True).select_related("store")
    }
    per_day: dict[date, dict] = defaultdict(lambda: {"total": 0.0, "n": 0, "points": defaultdict(float)})
    for r in rows_qs:
        d = r["day"]
        per_day[d]["total"] += (r["total"] or 0) / 100
        per_day[d]["n"] += r["n"]
        per_day[d]["points"][reg_point.get(r["shift__register"], "—")] += (r["total"] or 0) / 100

    point_names = [p["name"] for p in ranking]
    days = []
    d = c_start
    while d <= c_end:
        info = per_day.get(d, {"total": 0.0, "n": 0, "points": {}})
        days.append({
            "date": d,
            "dow": DOW[d.weekday()],
            "total": info["total"],
            "n": info["n"],
            "avg": (info["total"] / info["n"]) if info["n"] else 0,
            "points": [info["points"].get(name, 0.0) for name in point_names],
            "selected": sel_start <= d <= sel_end,
        })
        d += timedelta(days=1)

    chart = _chart(days)
    best_day = max(days, key=lambda x: x["total"]) if days else None
    return {
        "start": c_start, "end": c_end, "label": period_label(c_start, c_end),
        "point_names": point_names, "days": days, "chart": chart,
        "total": sum(x["total"] for x in days),
        "avg_day": (sum(x["total"] for x in days) / len(days)) if days else 0,
        "best_day": best_day if best_day and best_day["total"] > 0 else None,
        "is_window": not (c_start == sel_start and c_end == sel_end),
    }


# ---------------------------------------------------------------- grafik


def _nice_step(max_value: float) -> float:
    """Toza o'q qadamlari: 1/2/5 × 10^n, 4–6 ta chiziq chiqadigan qilib."""
    if max_value <= 0:
        return 1
    raw = max_value / 4
    mag = 10 ** len(str(int(raw))) / 10 if raw >= 1 else 1
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _chart(days: list[dict]) -> dict:
    """Ustunli grafik (SVG) uchun koordinatalar. Bitta qator — kunlik jami.

    Ustun ≤ 24px, tepasi 4px yumaloq, pastda to'g'ri; ustunlar orasi 2px+.
    Faqat eng katta kun raqam bilan belgilanadi; qolgani — hover va jadval.
    """
    n = len(days)
    # Grafik kengligi ~880px: kun kam bo'lsa ustunlar keng oraliqda, ko'p
    # bo'lsa zich. Ustunning o'zi baribir ≤ 24px.
    slot = max(14, min(80, int(880 / max(n, 1))))
    bar_w = min(24, slot - 6)
    left, right, top, bottom = 56, 12, 22, 34
    h = 200
    plot_h = h - top - bottom
    w = left + right + slot * n

    max_v = max((d["total"] for d in days), default=0)
    step = _nice_step(max_v)
    top_v = step * (int(max_v // step) + 1) if max_v > 0 else step
    scale = plot_h / top_v

    ticks = []
    v = 0
    while v <= top_v + 1e-9:
        ticks.append({"y": round(top + plot_h - v * scale, 1), "label": _short(v)})
        v += step

    bars = []
    best_i = max(range(n), key=lambda i: days[i]["total"]) if n else -1
    for i, d in enumerate(days):
        x = left + i * slot + (slot - bar_w) / 2
        bh = d["total"] * scale
        y = top + plot_h - bh
        r = min(4, bh / 2)
        # Tepasi yumaloq, pastda to'g'ri
        path = (
            f"M{x:.1f},{y + r:.1f} a{r},{r} 0 0 1 {r},-{r} h{bar_w - 2 * r:.1f} "
            f"a{r},{r} 0 0 1 {r},{r} v{bh - r:.1f} h-{bar_w:.1f} z"
        ) if bh > 0 else ""
        bars.append({
            "x": round(x, 1), "y": round(y, 1), "w": bar_w, "h": round(bh, 1),
            "cx": round(x + bar_w / 2, 1), "path": path,
            "date": d["date"], "total": d["total"], "n": d["n"],
            "selected": d["selected"], "is_best": i == best_i and d["total"] > 0,
            # Sana yozuvi: 14 kungacha har kuni; ko'p bo'lsa har 7-kun va
            # oxirgisi (oldingisiga yopishib qolmasa)
            "show_label": (n <= 14) or (i % 7 == 0) or (i == n - 1 and (n - 1) % 7 >= 3),
        })
    return {
        "w": w, "h": h, "left": left, "top": top, "bottom": bottom,
        "plot_w": slot * n, "plot_h": plot_h, "baseline": top + plot_h,
        "ticks": ticks, "bars": bars,
    }


def _short(v: float) -> str:
    """O'q uchun qisqa raqam: 1 250 000 → 1.25 mln, 45 000 → 45 ming."""
    v = float(v)
    if v >= 1_000_000:
        s = f"{v / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return f"{s} mln"
    if v >= 1_000:
        s = f"{v / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{s} ming"
    return f"{v:.0f}"
