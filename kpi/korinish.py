"""
Reyting sahifasi uchun grafik ko'rsatkichlar — faqat hisob (SVG geometriyasi
`dashboard.grafik` da). Rang va matn shablonda.

Rang qoidasi (dataviz): har tovarovedning rangi uning o'zi bilan bog'liq
(tartib raqami — pk bo'yicha), reytingdagi o'rniga qarab o'zgarmaydi.
Holat ranglari (yaxshi / ogohlantirish / xavf) doim matn bilan birga.
"""

from __future__ import annotations

from datetime import timedelta

from dashboard import grafik

from . import ms
from .models import KpiKun, MinusIssue, MinusScan

PALETTE = 5          # --c0 … --c4 (panel palitrasi, validatordan o'tgan)


def status_of(score) -> str:
    if score is None:
        return "none"
    return "good" if score >= 80 else ("warn" if score >= 50 else "bad")


def num_txt(v) -> str:
    """72.50 → «72.5», 80.00 → «80» (ortiqcha nollarsiz, doim nuqta bilan)."""
    if v is None:
        return "—"
    return f"{float(v):.1f}".rstrip("0").rstrip(".")


def score_ring(score):
    """Ball halqasi. None — ma'lumot yetarli emas (halqa bo'sh, «—»)."""
    r = grafik.ring(float(score) if score is not None else 0, size=88, thickness=9)
    r["value"] = score
    r["text"] = num_txt(score)
    r["status"] = status_of(score)
    return r


def share_meter(share, chegara):
    """Minus ulushi chizg'ichi: chegara belgisi bilan. Shkala — chegaraning
    ikki barobari (yoki ulushning o'zi, agar u kattaroq bo'lsa)."""
    if share is None:
        return None
    ch = float(chegara) if chegara else 5.0
    scale = max(ch * 2, float(share)) or 1
    status = "good" if share <= ch / 2 else ("warn" if share <= ch else "bad")
    return {
        "share": share, "chegara": chegara, "status": status,
        "label": {"good": "yaxshi", "warn": "chegaraga yaqin", "bad": "chegaradan oshgan"}[status],
        "chegara_txt": num_txt(ch),
        "fill": round(min(100.0, float(share) / scale * 100), 1),
        "mark": round(ch / scale * 100, 1),
    }


def composition(wh, now) -> dict:
    """Ochiq minuslar tarkibi: muddati o'tgan / kuzatuvdan oldingi / muddati bor."""
    qs = (MinusIssue.objects.filter(warehouse_ms_id=wh, status=MinusIssue.OPEN)
          .exclude(confirmed_cause="xato_emas"))
    late = qs.filter(deadline__lt=now).count()
    pre = qs.filter(deadline__gte=now, pre_tracking=True).count()
    ok = qs.filter(deadline__gte=now, pre_tracking=False).count()
    total = late + pre + ok
    segs = [
        {"key": "late", "label": "muddati o'tgan", "n": late},
        {"key": "pre", "label": "kuzatuvdan oldingi", "n": pre},
        {"key": "ok", "label": "muddati bor", "n": ok},
    ]
    for s in segs:
        s["pct"] = round(s["n"] * 100 / total, 2) if total else 0
    return {"total": total, "segs": segs}


def score_spark(tov, start, end, w: int = 260, h: int = 44, pad: float = 4):
    """Kunlik ish bali — o'zgarmas 0–100 shkalada (kichik tebranish katta
    ko'rinmasin). Davr qisqa bo'lsa ham so'nggi 14 kun ko'rinadi;
    2 kundan kam bo'lsa — None."""
    since = min(start, end - timedelta(days=13))
    rows = list(KpiKun.objects.filter(tovaroved=tov, sana__gte=since, sana__lte=end,
                                      ish_ball__isnull=False).order_by("sana")
                .values_list("sana", "ish_ball"))
    if len(rows) < 2:
        return None
    step = (w - 2 * pad) / (len(rows) - 1)
    pts = [(round(pad + i * step, 1), round(h - pad - float(v) / 100 * (h - 2 * pad), 1))
           for i, (_, v) in enumerate(rows)]
    line = "M" + " L".join(f"{x},{y}" for x, y in pts)
    return {
        "w": w, "h": h, "line": line,
        "area": line + f" L{pts[-1][0]},{h} L{pts[0][0]},{h} Z",
        "end": {"x": pts[-1][0], "y": pts[-1][1]},
        "n": len(rows), "first": num_txt(rows[0][1]), "last": num_txt(rows[-1][1]),
    }


def latest_counts(wh):
    """Shu do'kon bo'yicha oxirgi tekshiruv: (minus soni, faol tovar soni)."""
    scan = MinusScan.objects.filter(per_wh__has_key=str(wh)).order_by("-at").first()
    if not scan:
        return None, None
    neg, active = (scan.per_wh.get(str(wh)) or [None, None])[:2]
    return neg, active


def thin_labels(xlabels, gap, right):
    """X o'qi yozuvlari bir-biriga tegmasin: oralig'i `gap` dan kam bo'lsa
    tashlab ketiladi; oxirgisi doim qoladi (eng muhim nuqta) va o'ng
    chetdan chiqib ketmasin deb o'ngga tekislanadi."""
    if not xlabels:
        return []
    kept = [dict(xlabels[0], anchor="middle")]
    for xl in xlabels[1:]:
        if xl["x"] - kept[-1]["x"] >= gap:
            kept.append(dict(xl, anchor="middle"))
    last = xlabels[-1]
    if kept[-1]["x"] != last["x"]:
        if len(kept) > 1 and last["x"] - kept[-1]["x"] < gap:
            kept.pop()
        kept.append(dict(last, anchor="middle"))
    if len(kept) > 1 and kept[-1]["x"] >= right - 1:
        kept[-1]["anchor"] = "end"
    return kept


def minus_trend(series_defs, start, end, now, chegara=None):
    """Minusdagi faol tovar ulushi (%) dinamikasi — bitta o'q, har do'kon
    bitta chiziq. Ulush (soni emas): katta va kichik do'kon bir shkalada
    adolatli taqqoslanadi va KPI'dagi «minus ulushi» mezoni bilan bir xil.

    series_defs: [(ombor_id, nomi, rang_indeksi)]. Bir kunlik davrda —
    soatlar, uzunroqda — kunlar (oraliqdagi oxirgi tekshiruv). Grafik hamma
    do'konda ma'lumot bor joydan boshlanadi: kuzatuvdan oldingi vaqt
    0 deb chizilmaydi. `chegara` — uzuq gorizontal chiziq.
    """
    if not series_defs:
        return None
    s0, _ = ms.day_bounds(start)
    _, e0 = ms.day_bounds(end)
    e0 = min(e0, now)
    hourly = start == end
    buckets: dict[str, dict] = {}
    for at, per in (MinusScan.objects.filter(at__gte=s0, at__lt=e0).order_by("at")
                    .values_list("at", "per_wh")):
        loc = at.astimezone(ms.TASHKENT)
        key = loc.strftime("%H:00") if hourly else loc.strftime("%d.%m")
        b = buckets.setdefault(key, {})
        for wh, _, _ in series_defs:
            neg, active = ((per or {}).get(wh) or [None, None])[:2]
            if neg is not None and active:
                b[wh] = (round(neg * 100 / active, 2), neg)
    labels = list(buckets)
    series = []
    for wh, name, idx in series_defs:
        vals, last = [], None
        for k in labels:
            last = buckets[k].get(wh, last)
            vals.append(last)
        if any(v is not None for v in vals):
            series.append({"wh": wh, "name": name, "idx": idx % PALETTE, "values": vals})
    if not series:
        return None
    first = max(next(i for i, v in enumerate(s["values"]) if v is not None) for s in series)
    labels = labels[first:]
    if len(labels) < 2:
        return None
    for s in series:
        s["counts"] = [v[1] for v in s["values"][first:]]
        s["values"] = [v[0] for v in s["values"][first:]]
    lines = [{"name": s["name"], "values": s["values"]} for s in series]
    ch = float(chegara) if chegara else None
    if ch:
        # Chegara ham shkalaga kirsin (chiziq grafik ichida ko'rinsin)
        lines.append({"name": "chegara", "values": [ch] * len(labels)})

    def build(w, h, gap):
        chart = grafik.chart_lines(lines, labels, w=w, h=h, money=False)
        chart["goal"] = chart["series"].pop() if ch else None
        if ch:
            chart["goal"]["text"] = num_txt(chegara)
            chart["goal"]["y"] = chart["goal"]["points"][0]["y"]
            for hv in chart.get("hover", []):
                hv["items"] = hv["items"][:-1]
        for out, s in zip(chart["series"], series):
            out["idx"] = s["idx"]
            out["last"] = num_txt(s["values"][-1])
            out["end"] = out["points"][-1]
        for i, hv in enumerate(chart.get("hover", [])):
            for it, s in zip(hv["items"], series):
                it["idx"] = s["idx"]
                it["txt"] = f"{s['values'][i]:.2f}"
                it["n"] = s["counts"][i]
        for t in chart["ticks"]:
            t["label"] += "%"
        chart["xlabels"] = thin_labels(chart["xlabels"], gap, chart["axis_right"])
        return chart

    chart = build(880, 240, 60)
    # Telefon uchun ixcham nusxa: yozuvlar kattaroq, kamroq — yonga surmasdan sig'adi
    chart["mob"] = build(480, 300, 110)
    chart["hourly"] = hourly
    return chart
