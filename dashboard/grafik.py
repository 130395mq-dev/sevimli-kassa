"""Kichik grafiklar geometriyasi — SVG koordinatalarini hisoblaydi.

Bu yerda faqat matematika: qiymatlar ro'yxati kiradi, SVG yo'llari (path)
chiqadi. Rang, o'lcham va sarlavha shablonda beriladi — shuning uchun bitta
funksiya har joyda ishlatiladi va alohida sinaladi.

Nega SVG, kutubxona emas: panel Django shabloni, yig'ish (build) bosqichi
yo'q. Grafik server tomonda chiziladi — sahifa ochilishi bilan tayyor
turadi, internet sekin bo'lsa ham kechikmaydi.
"""

from __future__ import annotations

import math

#: Donutdagi bo'laklar orasidagi bo'shliq (gradus). Bo'laklar bir-biriga
#: yopishib ketmasin — ko'z ularni ajratsin.
GAP_DEG = 2.0


def _pt(c: float, r: float, ang: float) -> tuple[float, float]:
    """Markazi (c,c) bo'lgan doirada burchak bo'yicha nuqta. 0° — tepa."""
    a = math.radians(ang - 90)
    return c + r * math.cos(a), c + r * math.sin(a)


def spark_area(values: list[float], w: int = 190, h: int = 46,
               pad: float = 4) -> dict:
    """Mini area+chiziq: karta ichidagi 7 kunlik trend.

    Qaytadi: chiziq yo'li, tagidagi soya yo'li, har nuqtaning koordinatasi,
    eng baland va eng past nuqta indeksi (kartada belgilanadi).
    Hamma qiymat nol bo'lsa — chiziq o'rtada tekis yotadi.
    """
    n = len(values)
    if n == 0:
        return {"line": "", "area": "", "points": [], "max_i": -1, "min_i": -1,
                "w": w, "h": h}
    top, bottom = pad, h - pad
    lo, hi = min(values), max(values)
    span = hi - lo
    step = (w - 2 * pad) / (n - 1) if n > 1 else 0

    pts = []
    for i, v in enumerate(values):
        x = pad + i * step
        # Tekis qator (hammasi teng) — o'rtadan chizamiz
        y = (top + bottom) / 2 if span <= 0 else bottom - (v - lo) / span * (bottom - top)
        pts.append({"x": round(x, 1), "y": round(y, 1), "v": v, "i": i})

    line = "M" + " L".join(f"{p['x']},{p['y']}" for p in pts)
    area = (line + f" L{pts[-1]['x']},{h - 1} L{pts[0]['x']},{h - 1} Z") if n > 1 else ""
    hi_i = max(range(n), key=lambda i: values[i])
    lo_i = min(range(n), key=lambda i: values[i])
    return {
        "line": line, "area": area, "points": pts, "w": w, "h": h,
        "max_i": hi_i, "min_i": lo_i,
        # Shablon indeks bo'yicha ola olmaydi — nuqtalarni tayyor beramiz
        "hi": pts[hi_i], "lo": pts[lo_i],
        "flat": span <= 0,
    }


def spark_bars(values: list[float], w: int = 190, h: int = 46,
               gap: float = 2, radius: float = 2) -> dict:
    """Mini ustunlar: soatlik yoki kunlik ixcham grafik.

    Nol qiymatda ustun chizilmaydi (bo'sh joy — bu ham ma'lumot).
    """
    n = len(values)
    if n == 0:
        return {"bars": [], "w": w, "h": h, "max_i": -1}
    slot = w / n
    bw = max(1.0, slot - gap)
    hi = max(values) or 0
    bars = []
    for i, v in enumerate(values):
        bh = (v / hi * (h - 2)) if hi > 0 else 0
        x = i * slot + (slot - bw) / 2
        y = h - bh
        r = min(radius, bw / 2, bh / 2) if bh > 0 else 0
        bars.append({
            "i": i, "v": v, "x": round(x, 1), "y": round(y, 1),
            "w": round(bw, 1), "h": round(bh, 1), "r": round(r, 1),
            "cx": round(x + bw / 2, 1),
        })
    hi_i = max(range(n), key=lambda i: values[i])
    return {"bars": bars, "w": w, "h": h, "max_i": hi_i,
            "hi": bars[hi_i], "empty": hi <= 0}


def donut(parts: list[dict], size: int = 104, thickness: int = 16) -> dict:
    """Halqa diagramma. parts: [{"label":…, "value":…}, …].

    Har bo'lak uchun SVG yo'li va foizi qaytadi. Bitta bo'lak 100% bo'lsa
    to'liq halqa chiziladi (yoy bilan to'liq aylana chizib bo'lmaydi).
    Jami nol bo'lsa — bo'sh halqa.
    """
    total = sum(max(0.0, p["value"]) for p in parts)
    c = size / 2
    R = c
    r = c - thickness
    out = []
    if total <= 0:
        return {"parts": [], "total": 0, "size": size, "c": c, "r_in": r,
                "empty": True}

    live = [p for p in parts if p["value"] > 0]
    ang = 0.0
    for idx, p in enumerate(live):
        share = p["value"] / total
        sweep = share * 360
        # Bo'shliq faqat bir nechta bo'lak bo'lsa
        gap = GAP_DEG if len(live) > 1 else 0.0
        a0 = ang + gap / 2
        a1 = ang + sweep - gap / 2
        ang += sweep
        if a1 <= a0:                      # juda kichik bo'lak
            a1 = a0 + 0.35
        if len(live) == 1:
            d = (f"M{c},{c - R} A{R},{R} 0 1 1 {c - 0.01},{c - R} "
                 f"L{c - 0.01},{c - r} A{r},{r} 0 1 0 {c},{c - r} Z")
        else:
            x1, y1 = _pt(c, R, a0)
            x2, y2 = _pt(c, R, a1)
            x3, y3 = _pt(c, r, a1)
            x4, y4 = _pt(c, r, a0)
            big = 1 if (a1 - a0) > 180 else 0
            d = (f"M{x1:.2f},{y1:.2f} A{R},{R} 0 {big} 1 {x2:.2f},{y2:.2f} "
                 f"L{x3:.2f},{y3:.2f} A{r},{r} 0 {big} 0 {x4:.2f},{y4:.2f} Z")
        out.append({
            "label": p["label"], "value": p["value"], "d": d,
            "percent": round(share * 100, 1),
            "percent_int": round(share * 100),
            "i": idx,
        })
    return {"parts": out, "total": total, "size": size, "c": c, "r_in": r,
            "empty": False}


def ring(percent: float, size: int = 76, thickness: int = 8) -> dict:
    """Bitta ko'rsatkichli halqa (masalan: nechta kassa ulangan).

    percent 0–100. To'liq 100 bo'lsa butun halqa bo'yaladi.
    """
    p = max(0.0, min(100.0, float(percent)))
    c = size / 2
    r = c - thickness / 2
    circumference = 2 * math.pi * r
    return {
        "size": size, "c": c, "r": round(r, 2), "thickness": thickness,
        "dash": round(circumference * p / 100, 2),
        "gap": round(circumference * (100 - p) / 100, 2),
        "percent": round(p),
    }


def bars_h(rows: list[dict], limit: int | None = None) -> list[dict]:
    """Gorizontal ustunlar uchun foiz kengligi (eng kattasiga nisbatan).

    rows: [{"label":…, "value":…, …}] — tartibi saqlanadi.
    Har qatorga `bar` (0–100) va `share` (jamidan foiz) qo'shiladi.
    """
    rows = list(rows)[:limit] if limit else list(rows)
    if not rows:
        return []
    top = max(r["value"] for r in rows) or 0
    total = sum(r["value"] for r in rows) or 0
    for r in rows:
        r["bar"] = round(r["value"] / top * 100) if top > 0 else 0
        r["share"] = round(r["value"] / total * 100, 1) if total > 0 else 0
    return rows


# ------------------------------------------------------- o'q va raqamlar


def nice_step(max_value: float) -> float:
    """Toza o'q qadamlari: 1/2/5 × 10^n, 4–6 ta chiziq chiqadigan qilib."""
    if max_value <= 0:
        return 1
    raw = max_value / 4
    mag = 10 ** len(str(int(raw))) / 10 if raw >= 1 else 1
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def short(v: float) -> str:
    """O'q uchun qisqa raqam: 1 250 000 → 1.25 mln, 45 000 → 45 ming."""
    v = float(v)
    if v >= 1_000_000:
        s = f"{v / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return f"{s} mln"
    if v >= 1_000:
        s = f"{v / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{s} ming"
    return f"{v:.0f}"


def chart_lines(series: list[dict], labels: list[str], w: int = 880,
                h: int = 280, money: bool = True) -> dict:
    """Katta chiziqli/area grafik: bir nechta qator bitta o'qda.

    series: [{"name":…, "values":[…], "area": bool, "dash": bool}]
    Hamma qator bir xil uzunlikda va BIR XIL o'lchov birligida bo'lishi shart —
    ikkita turli o'q qo'ymaymiz, u yolg'on taqqoslash beradi.
    """
    left, right, top, bottom = 68, 16, 16, 34
    plot_w = w - left - right
    plot_h = h - top - bottom
    n = max((len(s["values"]) for s in series), default=0)
    if n == 0:
        return {"w": w, "h": h, "empty": True, "series": [], "ticks": [],
                "xlabels": [], "baseline": top + plot_h, "left": left,
                "plot_w": plot_w, "plot_h": plot_h}

    hi = max((max(s["values"]) if s["values"] else 0) for s in series)
    step = nice_step(hi)
    top_v = step * (int(hi // step) + 1) if hi > 0 else step
    scale = plot_h / top_v
    dx = plot_w / (n - 1) if n > 1 else 0

    ticks, v = [], 0.0
    while v <= top_v + 1e-9:
        ticks.append({"y": round(top + plot_h - v * scale, 1),
                      "label": short(v) if money else f"{int(v)}"})
        v += step

    out = []
    for s in series:
        pts = [
            {"x": round(left + i * dx, 1),
             "y": round(top + plot_h - val * scale, 1), "v": val, "i": i}
            for i, val in enumerate(s["values"])
        ]
        line = "M" + " L".join(f"{p['x']},{p['y']}" for p in pts) if pts else ""
        area = ""
        if s.get("area") and len(pts) > 1:
            area = (line + f" L{pts[-1]['x']},{top + plot_h} "
                           f"L{pts[0]['x']},{top + plot_h} Z")
        out.append({**s, "line": line, "area": area, "points": pts})

    every = max(1, n // 12)
    marks = [i for i in range(n) if i % every == 0]
    if n - 1 not in marks and (n - 1) - marks[-1] > every / 2:
        marks.append(n - 1)
    xlabels = [{"x": round(left + i * dx, 1), "label": labels[i]} for i in marks]
    # Sichqoncha uchun: har nuqtada hamma qatorning qiymati bitta izohda.
    # Shablonda qatorlarni bir-biriga moslash qiyin — tayyor beramiz.
    half = dx / 2 if dx else plot_w
    hover = [
        {
            "x": round(max(left, left + i * dx - half), 1),
            "w": round(dx if dx else plot_w, 1),
            "cx": round(left + i * dx, 1),
            "label": labels[i],
            "items": [{"name": sr["name"], "v": sr["values"][i],
                       "y": round(top + plot_h - sr["values"][i] * scale, 1)}
                      for sr in series if i < len(sr["values"])],
        }
        for i in range(n)
    ]
    return {
        "hover": hover,
        "w": w, "h": h, "left": left, "top": top, "plot_w": plot_w,
        "plot_h": plot_h, "baseline": top + plot_h, "axis_right": left + plot_w,
        "ticks": ticks, "series": out, "xlabels": xlabels, "empty": hi <= 0,
    }


def chart_bars(values: list[float], labels: list[str], w: int = 880,
               h: int = 260, money: bool = True) -> dict:
    """Katta ustunli grafik (bitta qator)."""
    left, right, top, bottom = 68, 16, 16, 34
    plot_w = w - left - right
    plot_h = h - top - bottom
    n = len(values)
    if n == 0:
        return {"w": w, "h": h, "empty": True, "bars": [], "ticks": [],
                "baseline": top + plot_h, "left": left, "plot_w": plot_w,
                "plot_h": plot_h}
    slot = plot_w / n
    bw = min(34.0, slot - 6)
    hi = max(values)
    step = nice_step(hi)
    top_v = step * (int(hi // step) + 1) if hi > 0 else step
    scale = plot_h / top_v

    ticks, v = [], 0.0
    while v <= top_v + 1e-9:
        ticks.append({"y": round(top + plot_h - v * scale, 1),
                      "label": short(v) if money else f"{int(v)}"})
        v += step

    every = max(1, n // 14)
    bars = []
    for i, val in enumerate(values):
        bh = val * scale
        x = left + i * slot + (slot - bw) / 2
        y = top + plot_h - bh
        r = min(4.0, bw / 2, bh / 2) if bh > 0 else 0
        path = (
            f"M{x:.1f},{y + r:.1f} a{r:.1f},{r:.1f} 0 0 1 {r:.1f},-{r:.1f} "
            f"h{bw - 2 * r:.1f} a{r:.1f},{r:.1f} 0 0 1 {r:.1f},{r:.1f} "
            f"v{bh - r:.1f} h-{bw:.1f} z"
        ) if bh > 0 else ""
        bars.append({
            "i": i, "v": val, "label": labels[i], "path": path,
            "x": round(x, 1), "y": round(y, 1), "w": round(bw, 1),
            "cx": round(x + bw / 2, 1), "slot_x": round(left + i * slot, 1),
            "slot_w": round(slot, 1),
            "show_label": i % every == 0 or i == n - 1,
        })
    return {
        "w": w, "h": h, "left": left, "top": top, "plot_w": plot_w,
        "plot_h": plot_h, "baseline": top + plot_h, "axis_right": left + plot_w,
        "ticks": ticks, "bars": bars, "empty": hi <= 0,
    }
