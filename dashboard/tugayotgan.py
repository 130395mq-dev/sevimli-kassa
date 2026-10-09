"""Yaxshi sotilib, kam qolgan yoki tugagan tovarlar (egasining so'rovi, 2026-10-04).

«Panelda yaxshi sotilib kam va tugagan mahsulotlar ro'yxati kerak» — ya'ni
buyurtma berish / skladdan keltirish kerak bo'lgan tovarlar.

Ma'lumot ko'chirish tavsiyasi bilan BIR XIL va bitta hisobda olinadi
(`kochirish.build`): har do'konning oxirgi 30 kunlik savdosi MoySklad'dan
(faqat o'qish), qoldiq — bazadagi `catalog.Stock`. Shuning uchun MoySklad'ga
qo'shimcha so'rov ketmaydi va ikkala sahifa bir vaqtda yangilanadi.

Qoida (ko'chirish tavsiyasidagi «yetmayapti» bilan bir xil sonlar):
  * YAXSHI SOTILADI — do'konda 30 kunda kamida `MIN_SOLD` (10) ta sotilgan;
  * TUGAGAN        — shu do'konda qoldiq 0 (yoki manfiy);
  * KAM QOLGAN     — qoldiq bor, lekin hozirgi sur'atda `LOW_COVER_DAYS`
                     (7) kunga ham yetmaydi;
  * KERAK          — `TARGET_DAYS` (14) kunlik savdoga yetishi uchun qancha
                     keltirish kerak;
  * BOSHQA JOYDA   — shu tovarning boshqa omborlardagi (sklad ham) qoldig'i:
                     bor bo'lsa — keltirish mumkin, yo'q bo'lsa — buyurtma.
Tartib: avval tugaganlar, ichida kunlik savdosi (so'm) kattasi birinchi.

Eslatma: qoldiq MoySklad'dagi hisob qoldig'i. Hisobda bor-u, javonda yo'q
tovar (masalan S76025, 2026-10-02) bu ro'yxatga TUSHMAYDI — uni faqat
inventarizatsiya ko'rsatadi.
"""

from __future__ import annotations

import csv
from decimal import ROUND_UP, Decimal
from io import StringIO

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render

from . import access, kochirish

OUT, LOW = "out", "low"
PAGE_LIMIT = 300           # sahifada ko'rsatiladigan qatorlar; hammasi — CSV'da


def _ceil(qty: Decimal, weighed: bool) -> Decimal:
    step = Decimal("0.1") if weighed else Decimal("1")
    return (qty / step).to_integral_value(rounding=ROUND_UP) * step


def shortage(sales: dict, stock: dict, products: dict, shops: dict, all_wh: dict) -> list[dict]:
    """Sof hisob (bazaga, MoySklad'ga tegmaydi).

    sales    — {(ombor_id, product_id): 30 kunda sotilgan soni}
    stock    — {(ombor_id, product_id): qoldiq} — HAMMA omborlar (sklad ham)
    products — {product_id: {"name", "code", "uom", "price" (tiyin), "is_weight"}}
    shops    — {ombor_id: nomi} — savdo qiladigan omborlar
    all_wh   — {ombor_id: nomi} — hamma omborlar («boshqa joyda» uchun)
    """
    by_product: dict = {}
    for (wh, pid), qty in stock.items():
        by_product.setdefault(pid, {})[wh] = Decimal(qty)

    days = Decimal(kochirish.PERIOD_DAYS)
    rows = []
    for (wh, pid), sold in sales.items():
        if wh not in shops:
            continue
        sold = Decimal(sold)
        p = products.get(pid)
        if not p or sold < kochirish.MIN_SOLD:
            continue
        rate = sold / days
        stocks = by_product.get(pid, {})
        st = stocks.get(wh, Decimal(0))
        left = max(st, Decimal(0))
        cover = left / rate
        if cover >= kochirish.LOW_COVER_DAYS:
            continue
        elsewhere = sorted(
            ({"name": all_wh.get(w) or "boshqa ombor", "qty": q}
             for w, q in stocks.items() if w != wh and q > 0),
            key=lambda e: -e["qty"],
        )
        rows.append({
            "product_id": pid, "name": p["name"], "code": p.get("code") or "",
            "uom": p.get("uom") or "", "price": p.get("price") or 0,
            "wh_id": wh, "wh": shops[wh],
            "state": OUT if left <= 0 else LOW,
            "sold": sold, "rate": rate, "stock": st,
            "days": round(float(cover), 1),
            "need": _ceil(rate * kochirish.TARGET_DAYS - left, kochirish._is_weighed(p)),
            # Kunlik savdo, so'm: tugagan tovarda — har kuni yo'qotilayotgan savdo
            "per_day": int(rate * (p.get("price") or 0)) / 100,
            "elsewhere": elsewhere[:3],
            "elsewhere_total": sum((e["qty"] for e in elsewhere), Decimal(0)),
        })
    rows.sort(key=lambda r: (r["state"] != OUT, -r["per_day"], r["name"]))
    return rows


def filtered(rows: list[dict], shop: str = "", state: str = "", query: str = "") -> list[dict]:
    if shop:
        rows = [r for r in rows if r["wh_id"] == shop]
    if state in (OUT, LOW):
        rows = [r for r in rows if r["state"] == state]
    q = (query or "").strip().casefold()
    if q:
        rows = [r for r in rows if q in r["name"].casefold() or q in r["code"].casefold()]
    return rows


def to_csv(data: dict, rows: list[dict]) -> tuple[str, str]:
    from .tovarlar import _csv_num, _csv_text

    buf = StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([f"Tugayotgan va tugagan tovarlar — savdo {data['start']:%d.%m}–{data['end']:%d.%m} (30 kun)"])
    w.writerow(["Holat", "Kodi", "Nomi", "O'lchov", "Do'kon", "30 kunda sotildi", "Kuniga",
                "Qoldiq", "Necha kunga yetadi", "Kerak (14 kunlik)", "Boshqa omborlarda",
                "Kunlik savdo, so'm"])
    for r in rows:
        w.writerow([
            "tugagan" if r["state"] == OUT else "kam qolgan", _csv_text(r["code"]), r["name"],
            r["uom"], r["wh"], _csv_num(r["sold"]), f"{r['rate']:.2f}".replace(".", ","),
            _csv_num(r["stock"]) or "0", f"{r['days']:.1f}".replace(".", ","), _csv_num(r["need"]),
            "; ".join(f"{e['name']}: {_csv_num(e['qty'])}" for e in r["elsewhere"]),
            f"{r['per_day']:.0f}",
        ])
    return f"sevimli-tugayotgan-{data['end']:%Y%m%d}.csv", buf.getvalue()


@login_required
def page(request):
    """?dokon= / ?holat=out|low / ?q= — filtr, ?format=csv — Excel,
    ?yangila=1 — MoySklad'dan qayta hisoblash."""
    manager = access.manager_of(request.user)
    # Boshqaruvchi MoySklad'dan qayta hisoblatmaydi (og'ir so'rov — egasining ishi)
    data = kochirish.get(refresh=request.GET.get("yangila") == "1" and not manager)
    ready = bool(data.get("ok")) and "short" in data
    shop, state, query = (request.GET.get(k, "") for k in ("dokon", "holat", "q"))
    all_rows = data.get("short") or []
    if manager:
        # Faqat o'z marketi; boshqa omborlardagi qoldiq ko'rsatilmaydi.
        # Keshdagi asl ma'lumotga tegilmaydi — nusxa olinadi.
        shop = str(manager.warehouse_ms_id)
        all_rows = [{**r, "elsewhere": [], "elsewhere_total": 0}
                    for r in all_rows if str(r["wh_id"]) == shop]
        data = {**data, "rows": [], "short": all_rows,
                "warehouses": [w for w in data.get("warehouses") or [] if str(w["id"]) == shop]}
    rows = filtered(all_rows, shop, state, query) if ready else []
    if request.GET.get("format") == "csv" and ready:
        name, text = to_csv(data, rows)
        resp = HttpResponse("﻿" + text, content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{name}"'
        return resp
    out = [r for r in rows if r["state"] == OUT]
    params = request.GET.copy()
    for k in ("format", "yangila"):
        params.pop(k, None)
    return render(request, "dashboard/tugayotgan.html", {
        "k": data, "ready": ready, "rows": rows[:PAGE_LIMIT], "total": len(rows),
        "hidden": max(len(rows) - PAGE_LIMIT, 0),
        "out_count": len(out), "low_count": len(rows) - len(out),
        "lost": sum(r["per_day"] for r in out),
        "shop": shop, "state": state, "query": query, "keep": params.urlencode(),
        "rules": {"days": kochirish.PERIOD_DAYS, "min_sold": kochirish.MIN_SOLD,
                  "low": kochirish.LOW_COVER_DAYS, "target": kochirish.TARGET_DAYS},
    })
