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

from django.db.models import Count, Q, Sum
from django.db.models.functions import ExtractHour, TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_date

from dashboard import grafik
from sales.models import Payment, PanelSettings, Register, Sale, SaleItem, Shift

#: Bir so'rovda eng ko'pi shuncha kun (jadval o'qiladigan bo'lsin)
MAX_DAYS = 92
#: Grafik uchun kamida shuncha kun ko'rsatiladi
CHART_MIN_DAYS = 14
#: Sutkadagi soatlar — soatlik grafik shuncha ustundan iborat
HOURS = 24
#: Karta ichidagi mini-grafik shuncha kunni ko'rsatadi
SPARK_DAYS = 7
#: «Eng faol vaqt» shuncha soatlik oyna bo'yicha qidiriladi
BUSY_WINDOW = 2

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


def queue_snapshot(params) -> dict:
    """Jonli navbat sonlari; kassa belgilari tanlangan davrga mos keladi."""
    start, end, _ = parse_range(params)
    a, b = _bounds(start, end)
    pending = Sale.objects.filter(sync_status__in=[Sale.NEW, Sale.FAILED, Sale.STUCK])
    counts = pending.aggregate(
        queued=Count("pk", filter=Q(sync_status__in=[Sale.NEW, Sale.FAILED])),
        stuck=Count("pk", filter=Q(sync_status=Sale.STUCK)),
    )
    rows = (pending.filter(created_at__gte=a, created_at__lt=b)
            .values("shift__register_id")
            .annotate(n=Count("pk")).order_by())
    counts["registers"] = {str(r["shift__register_id"]): r["n"] for r in rows}
    return counts


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

    # ---- karta ichidagi mini-grafiklar
    hourly = _hourly(start, end)
    spark = _spark(end)
    peak_day, low_day = _peak_low(spark)
    hour_counts = [h["n"] for h in hourly["hours"]]
    target = PanelSettings.get().avg_receipt_target / 100
    cards = {
        "days": spark,
        "peak_day": peak_day,
        "low_day": low_day,
        "busy": _busy_window(hour_counts),
        "sale_area": grafik.spark_area([d["total"] for d in spark]),
        "receipt_bars": grafik.spark_bars([float(n) for n in hour_counts]),
        "avg_area": grafik.spark_area([d["avg"] for d in spark]),
        "returns_bars": grafik.spark_bars([d["returns"] for d in spark]),
        "pay_donut": grafik.donut(
            [{"label": "Naqd", "value": cur["cash"]},
             {"label": "Naqdsiz", "value": cur["cashless"]}],
            size=64, thickness=10,
        ),
        "returns_share": (round(cur["returns"] / cur["total"] * 100, 1)
                          if cur["total"] else 0),
        "target": target,
        "target_left": max(0.0, target - cur["avg"]) if target else 0,
        "target_percent": (round(cur["avg"] / target * 100) if target else None),
    }

    return {
        "hourly": hourly,
        "cards": cards,
        "methods_donut": grafik.donut(
            [{"label": m["name"], "value": m["total"]} for m in methods]
        ),
        "points_bars": grafik.bars_h([
            {"label": p["name"], "value": p["total"], "receipts": p["receipts"],
             "rank": p["rank"], "avg": p["avg"]}
            for p in ranking
        ]),
        "top": grafik.bars_h(top_products(start, end)),
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


# ------------------------------------------------- karta mini-grafiklari


def _spark(end: date, days: int = SPARK_DAYS) -> list[dict]:
    """Oxirgi N kun: har kuni savdo, chek, o'rtacha chek, qaytarish.

    Kartalar ichidagi mayda grafiklar shu qatordan chiziladi. Davr filtridan
    qat'i nazar oxirgi N kunni ko'rsatadi — «hozir qaysi tomonga ketyapti»
    degan savolga javob beradi.
    """
    start = end - timedelta(days=days - 1)
    a, b = _bounds(start, end)
    tz = timezone.get_current_timezone()
    rows = (
        Sale.objects.filter(created_at__gte=a, created_at__lt=b)
        .annotate(day=TruncDate("created_at", tzinfo=tz))
        .values("day", "kind")
        .annotate(total=Sum("net_total"), n=Count("id"))
        .order_by()
    )
    per: dict[date, dict] = defaultdict(
        lambda: {"total": 0.0, "n": 0, "returns": 0.0, "returns_n": 0}
    )
    for r in rows:
        cell = per[r["day"]]
        if r["kind"] == Sale.SALE:
            cell["total"] += (r["total"] or 0) / 100
            cell["n"] += r["n"]
        else:
            cell["returns"] += (r["total"] or 0) / 100
            cell["returns_n"] += r["n"]

    out = []
    d = start
    while d <= end:
        c = per.get(d, {"total": 0.0, "n": 0, "returns": 0.0, "returns_n": 0})
        out.append({
            "date": d, "dow": DOW[d.weekday()],
            "total": c["total"], "n": c["n"],
            "avg": (c["total"] / c["n"]) if c["n"] else 0,
            "returns": c["returns"], "returns_n": c["returns_n"],
        })
        d += timedelta(days=1)
    return out


def _peak_low(days: list[dict], key: str = "total"):
    """Eng baland va eng past kun (ikkalasi ham nol bo'lsa — None)."""
    live = [d for d in days if d[key] > 0]
    if not live:
        return None, None
    return (max(live, key=lambda d: d[key]), min(live, key=lambda d: d[key]))


def _busy_window(cnt: list[int], width: int = BUSY_WINDOW):
    """Eng ko'p chek uriladigan uzluksiz soat oynasi: («09:00–11:00», soni)."""
    if not any(cnt):
        return None
    best_i, best_sum = 0, -1
    for i in range(HOURS - width + 1):
        s = sum(cnt[i:i + width])
        if s > best_sum:
            best_i, best_sum = i, s
    return {
        "label": f"{best_i:02d}:00–{best_i + width:02d}:00",
        "n": best_sum,
        "from": best_i, "to": best_i + width,
    }


def top_products(start: date, end: date, limit: int = 10,
                 kind: str = Sale.SALE) -> list[dict]:
    """Eng ko'p sotilgan (yoki qaytarilgan) tovarlar — summa bo'yicha.

    Nom chek qatoridan olinadi: tovar keyin o'chirilsa ham tarix buzilmaydi.
    """
    a, b = _bounds(start, end)
    rows = (
        SaleItem.objects.filter(
            sale__kind=kind, sale__created_at__gte=a, sale__created_at__lt=b
        )
        .values("name")
        .annotate(total=Sum("total"), qty=Sum("quantity"), n=Count("sale", distinct=True))
        .order_by("-total")[:limit]
    )
    return [
        {"label": r["name"], "value": (r["total"] or 0) / 100,
         "qty": float(r["qty"] or 0), "n": r["n"]}
        for r in rows
    ]


def items_per_receipt(start: date, end: date) -> float:
    """Bir chekdagi o'rtacha mahsulot soni (qator emas, dona)."""
    a, b = _bounds(start, end)
    agg = SaleItem.objects.filter(
        sale__kind=Sale.SALE, sale__created_at__gte=a, sale__created_at__lt=b
    ).aggregate(q=Sum("quantity"))
    n = Sale.objects.filter(
        kind=Sale.SALE, created_at__gte=a, created_at__lt=b
    ).count()
    return (float(agg["q"] or 0) / n) if n else 0


# ----------------------------------------------------------- soatlik kun


def _hourly(start: date, end: date) -> dict:
    """Kun ichidagi savdo: qaysi soatda qancha sotildi va nechta chek.

    Do'kon egasi uchun eng amaliy kesim: xodimni qachon ko'paytirish, aksiyani
    qachon boshlash. Ikki o'lchov bitta vaqt o'qida — summa ustun bilan,
    chek soni ustidan o'tadigan chiziq bilan.
    """
    a, b = _bounds(start, end)
    tz = timezone.get_current_timezone()
    rows = (
        Sale.objects.filter(created_at__gte=a, created_at__lt=b)
        .annotate(hh=ExtractHour("created_at", tzinfo=tz))
        .values("hh", "kind")
        .annotate(total=Sum("net_total"), n=Count("id"))
        .order_by()
    )
    tot = [0.0] * HOURS
    cnt = [0] * HOURS
    ret = [0.0] * HOURS
    ret_n = [0] * HOURS
    for r in rows:
        h = int(r["hh"] or 0) % HOURS
        if r["kind"] == Sale.SALE:
            tot[h] += (r["total"] or 0) / 100
            cnt[h] += r["n"]
        else:
            ret[h] += (r["total"] or 0) / 100
            ret_n[h] += r["n"]

    peak = max(range(HOURS), key=lambda h: tot[h]) if any(tot) else None
    return {
        # Qatorlar `_spark()` bilan bir xil kalitlarga ega — batafsil panel
        # ikkalasini ham farqsiz ishlatadi.
        "hours": [
            {"h": h, "label": f"{h:02d}:00", "total": tot[h], "n": cnt[h],
             "avg": (tot[h] / cnt[h]) if cnt[h] else 0,
             "returns": ret[h], "returns_n": ret_n[h]}
            for h in range(HOURS)
        ],
        "total": sum(tot),
        "receipts": sum(cnt),
        "peak": {"label": f"{peak:02d}:00", "total": tot[peak], "n": cnt[peak]}
        if peak is not None else None,
        "chart": _hour_chart(tot, cnt),
    }


def _axis(values, plot_h: float):
    """O'q: (tepa qiymati, masshtab, chiziqlar ro'yxati uchun qadam)."""
    max_v = max(values) if values else 0
    step = _nice_step(max_v)
    top_v = step * (int(max_v // step) + 1) if max_v > 0 else step
    return top_v, plot_h / top_v, step


def _hour_chart(tot: list[float], cnt: list[int]) -> dict:
    """Soatlik grafik (SVG) koordinatalari.

    Chap o'q — so'm (ustunlar), o'ng o'q — chek soni (chiziq). Ikki o'lchov
    bitta rasmda bo'lgani uchun har ikkalasining o'qi alohida imzolanadi va
    ranglari bir-biridan aniq farq qiladi; hoverda aniq raqamlar chiqadi.
    """
    left, right, top, bottom = 64, 56, 18, 34
    w, h = 960, 250
    plot_w = w - left - right
    plot_h = h - top - bottom
    slot = plot_w / HOURS
    bar_w = min(20.0, slot - 8)

    top_v, scale, step = _axis(tot, plot_h)
    n_top, n_scale, n_step = _axis(cnt, plot_h)

    ticks, v = [], 0.0
    while v <= top_v + 1e-9:
        ticks.append({"y": round(top + plot_h - v * scale, 1), "label": _short(v)})
        v += step
    n_ticks, v = [], 0.0
    while v <= n_top + 1e-9:
        n_ticks.append({"y": round(top + plot_h - v * n_scale, 1), "label": f"{int(v)}"})
        v += n_step

    bars, pts = [], []
    for hh in range(HOURS):
        x = left + hh * slot + (slot - bar_w) / 2
        bh = tot[hh] * scale
        y = top + plot_h - bh
        r = min(4.0, bh / 2)
        path = (
            f"M{x:.1f},{y + r:.1f} a{r:.1f},{r:.1f} 0 0 1 {r:.1f},-{r:.1f} "
            f"h{bar_w - 2 * r:.1f} a{r:.1f},{r:.1f} 0 0 1 {r:.1f},{r:.1f} "
            f"v{bh - r:.1f} h-{bar_w:.1f} z"
        ) if bh > 0 else ""
        cx = x + bar_w / 2
        ny = top + plot_h - cnt[hh] * n_scale
        bars.append({
            "h": hh, "label": f"{hh:02d}:00",
            "x": round(x, 1), "y": round(y, 1), "w": round(bar_w, 1),
            "cx": round(cx, 1), "path": path, "ny": round(ny, 1),
            "slot_x": round(left + hh * slot, 1), "slot_w": round(slot, 1),
            "total": tot[hh], "n": cnt[hh],
            # X o'qi: har ikki soatda bir imzo — 00:00, 02:00 … 22:00
            "show_label": hh % 2 == 0,
        })
        pts.append(f"{cx:.1f},{ny:.1f}")

    return {
        "w": w, "h": h, "left": left, "top": top, "bottom": bottom,
        "plot_w": round(plot_w, 1), "plot_h": plot_h,
        "baseline": top + plot_h, "axis_right": round(left + plot_w, 1),
        "ticks": ticks, "n_ticks": n_ticks, "bars": bars,
        "line": " ".join(pts),
        "has_data": any(tot) or any(cnt),
    }


# ---------------------------------------------------------------- grafik


#: O'q qadami va qisqa raqam — yagona nusxa grafik.py da (ikki joyda
#: boshqacha ishlamasin). Eski nomlar saqlanadi.
_nice_step = grafik.nice_step


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


_short = grafik.short
