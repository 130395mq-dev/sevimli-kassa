"""
Kunlik va oylik KPI hisobi.

Har mezon uchun: kirish raqamlari, formula va ball (yoki NEGA ball yo'qligi)
`KpiKun.tarkib` ga yoziladi — har ballni ochib ko'rish mumkin.

Qoidalar:
  * ma'lumot yetishmasa ball None — «Ma'lumot yetarli emas» (0 ham, 100 ham emas);
  * o'sha kuni baholanadigan vazifa bo'lmasa — mezon hisobga olinmaydi
    (xodim jazolanmaydi);
  * taxmin (ehtimoliy sabab) ballga kirmaydi;
  * oddiy qoldiq tuzatishi (oprixodovaniye) yoki hujjatsiz yo'qolgan minus —
    rahbar tasdiqlamaguncha neytral (na yaxshi, na yomon);
  * hujjatlar soni ball bermaydi — faqat «ish hajmi».
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from . import ms
from .davomat import baho as davomat_baho, manba as davomat_manba
from .models import Biriktirish, ChopEtish, Hujjat, KpiKun, KpiQoida, MinusIssue, MinusScan

D0 = Decimal(0)
D100 = Decimal(100)


def q2(x) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def clamp(x: Decimal) -> Decimal:
    return max(D0, min(D100, x))


def assignments_on(day: date):
    return (Biriktirish.objects.filter(boshlanish__lte=day, tovaroved__active=True)
            .filter(Q(tugash__isnull=True) | Q(tugash__gte=day))
            .select_related("tovaroved").order_by("tovaroved_id", "-boshlanish", "-pk"))


def _window(day: date, rule: KpiQoida, now):
    start, _ = ms.day_bounds(day)
    w0 = start + timedelta(hours=rule.kun_boshi)
    w1 = start + timedelta(hours=rule.kun_oxiri)
    return w0, min(w1, now)


def coverage(wh: str, day: date, rule: KpiQoida, now) -> dict:
    """Ish soatlarining qanchasida minus tekshiruvi bo'lgan."""
    w0, w1 = _window(day, rule, now)
    if w1 <= w0:
        return {"soat": 0, "jami": 0, "foiz": None, "scans": []}
    total = int((w1 - w0).total_seconds() // 3600) + (1 if (w1 - w0).total_seconds() % 3600 else 0)
    hours, scans = set(), []
    for at, per_wh in MinusScan.objects.filter(at__gte=w0, at__lt=w1).values_list("at", "per_wh"):
        if wh in (per_wh or {}):
            hours.add(int((at - w0).total_seconds() // 3600))
            scans.append(per_wh[wh])
    return {"soat": len(hours), "jami": total, "foiz": q2(Decimal(len(hours)) * 100 / total), "scans": scans}


def _active_by_sales(wh: str, day: date, days: int) -> int | None:
    from dashboard.access import registers_of
    from sales.models import SaleItem

    regs = registers_of(wh)
    if not regs:
        return None
    start, end = ms.day_bounds(day)
    return (SaleItem.objects.filter(sale__shift__register_id__in=regs, ms_product_id__isnull=False,
                                    sale__created_at__gte=start - timedelta(days=days - 1),
                                    sale__created_at__lt=end)
            .values("ms_product_id").distinct().count())


def m_minus_ulush(wh, day, rule, cov) -> dict:
    m = {"key": "minus_ulush", "nomi": "Minusdagi tovarlar ulushi", "vazn": rule.w_minus_ulush}
    if cov["foiz"] is None:
        return {**m, "ball": None, "sabab": "Ish kuni hali boshlanmagan"}
    if cov["foiz"] < rule.min_qamrov:
        return {**m, "ball": None,
                "sabab": f"Ma'lumot yetarli emas: {cov['jami']} ish soatidan faqat {cov['soat']} tasida "
                         f"tekshiruv bo'lgan (kamida {rule.min_qamrov}% kerak)"}
    negs = [s[0] for s in cov["scans"]]
    neg = Decimal(sum(negs)) / len(negs)
    if rule.faol_tovar == KpiQoida.FAOL_SOTUV:
        active = _active_by_sales(wh, day, rule.faol_sotuv_kun)
        tarif = f"oxirgi {rule.faol_sotuv_kun} kunda shu do'kon kassalarida sotilgan tovarlar"
    else:
        vals = [s[1] for s in cov["scans"] if s[1]]
        active = int(sum(vals) / len(vals)) if vals else None
        tarif = "omborda qoldig'i 0 dan farqli tovarlar"
    if not active:
        return {**m, "ball": None, "sabab": f"Faol tovarlar soni noma'lum ({tarif})"}
    share = neg * 100 / active
    ball = clamp(D100 * (1 - share / Decimal(rule.minus_ulush_chegara)))
    return {**m, "ball": q2(ball), "sabab": "",
            "kirish": {"minus_ortacha": str(q2(neg)), "faol_tovar": active, "faol_tarif": tarif,
                       "ulush_foiz": str(q2(share)), "chegara_foiz": str(rule.minus_ulush_chegara),
                       "tekshiruvlar": len(negs)},
            "formula": f"100 × (1 − {q2(share)}% / {rule.minus_ulush_chegara}%) = {q2(ball)}"}


def _excluded(i: MinusIssue) -> bool:
    return i.confirmed_cause == "xato_emas"


def m_muddat(wh, day, rule, now, cov) -> dict:
    m = {"key": "muddat", "nomi": "Minuslarni muddatida tuzatish", "vazn": rule.w_muddat}
    if cov["foiz"] is None or cov["foiz"] < rule.min_qamrov:
        return {**m, "ball": None, "sabab": "Ma'lumot yetarli emas (tekshiruv qamrovi past)"}
    start, end = ms.day_bounds(day)
    end = min(end, now)
    ok, bad, neutral = [], [], []
    for i in MinusIssue.objects.filter(warehouse_ms_id=wh, deadline__gte=start, deadline__lt=end):
        label = f"{i.name or i.product_ms_id}"
        if _excluded(i):
            neutral.append(f"{label}: rahbar «xato emas» deb belgilagan")
        elif i.closed_at and i.closed_at <= i.deadline:
            if i.reopened:
                bad.append(f"{label}: tuzatilgan, lekin {rule.qayta_paydo_kun} kun ichida yana minusga "
                           "tushdi — tuzatish hisoblanmadi")
            elif not i.resolution:
                neutral.append(f"{label}: qanday yopilgani hali aniqlanmoqda")
            elif i.resolution in (MinusIssue.RES_OPRIXOD, MinusIssue.RES_NOMALUM) and i.resolution_ok is not True:
                neutral.append(f"{label}: {i.get_resolution_display()} — rahbar tasdiqlamagan")
            else:
                ok.append(f"{label}: {i.get_resolution_display()}")
        elif i.closed_at is None and i.last_seen < i.deadline:
            # Muddatdan keyin tekshiruv bo'lmagan — tuzatilganmi, yo'qmi, noma'lum
            neutral.append(f"{label}: muddatdan keyin tekshiruv bo'lmagan — noma'lum")
        elif i.closed_at and i.closed_prev_scan and i.closed_prev_scan < i.deadline:
            # Uzilish: minus muddatgacha yoki undan keyin yo'qolgani aniq emas
            neutral.append(f"{label}: tekshiruvlar orasida uzilish — qachon tuzatilgani noaniq")
        else:
            bad.append(f"{label}: muddatida tuzatilmagan")
    n = len(ok) + len(bad)
    base = {"kirish": {"vaqtida": ok, "kechikkan": bad, "neytral": neutral}}
    if not n:
        return {**m, **base, "ball": None,
                "sabab": "Bu kuni muddati tugagan baholanadigan vazifa yo'q — hisobga olinmadi"}
    ball = D100 * len(ok) / n
    return {**m, **base, "ball": q2(ball), "sabab": "",
            "formula": f"{len(ok)} vaqtida / {n} ta = {q2(ball)}"}


def m_ochiq(wh, day, rule, now, cov) -> dict:
    m = {"key": "ochiq", "nomi": "Ochiq muammolar muddatdan o'tmagani", "vazn": rule.w_ochiq}
    if cov["foiz"] is None or cov["foiz"] < rule.min_qamrov:
        return {**m, "ball": None, "sabab": "Ma'lumot yetarli emas (tekshiruv qamrovi past)"}
    _, end = ms.day_bounds(day)
    at = min(end, now)
    issues = [i for i in MinusIssue.objects.filter(warehouse_ms_id=wh, first_seen__lte=at)
              .filter(Q(closed_at__isnull=True) | Q(closed_at__gt=at)) if not _excluded(i)]
    if not issues:
        return {**m, "ball": None, "sabab": "Ochiq muammo yo'q — hisobga olinmadi"}
    late = [i for i in issues if i.deadline < at]
    ages = [(at - i.first_seen).total_seconds() / 3600 for i in issues]
    ball = D100 * (1 - Decimal(len(late)) / len(issues))
    return {**m, "ball": q2(ball), "sabab": "",
            "kirish": {"ochiq": len(issues), "muddati_otgan": len(late),
                       "ortacha_yosh_soat": round(sum(ages) / len(ages), 1),
                       "kuzatuvdan_oldingi": sum(1 for i in issues if i.pre_tracking)},
            "formula": f"100 × (1 − {len(late)} / {len(issues)}) = {q2(ball)}"}


def m_sennik(rule) -> dict:
    return {"key": "sennik", "nomi": "Kerakli senniklar o'z vaqtida", "vazn": rule.w_sennik, "ball": None,
            "sabab": "Hali baholanmaydi: auditda shablon (sennik yoki etiketka) ko'rinmaydi va "
                     "sennik talabi (narx o'zgarishi) hali hisoblanmayapti. Chop etishlar faqat hajm sifatida ko'rsatiladi."}


def volume(tov, wh, day) -> dict:
    """Ish hajmi — BALL BERMAYDI. Hujjat bir marta (yaratilgan kuni) sanaladi."""
    start, end = ms.day_bounds(day)
    out = {}
    for kind, name in Hujjat.KINDS:
        qs = Hujjat.objects.filter(kind=kind, created__gte=start, created__lt=end, deleted_at__isnull=True)
        qs = qs.filter(Q(store_ms_id=wh) | Q(target_ms_id=wh))
        agg = qs.aggregate(n=Count("id"), tur=Sum("products_count"), dona=Sum("quantity"), summa=Sum("sum"))
        own = qs.filter(creator_uid=tov.ms_uid, creator_source__in=list(ms.MANUAL_SOURCES)).count() if tov.ms_uid else 0
        auto = qs.filter(creator_checked=True).exclude(creator_source__in=list(ms.MANUAL_SOURCES)).count()
        out[kind] = {"nomi": name, "hujjat": agg["n"] or 0, "tovar_turi": agg["tur"] or 0,
                     "dona": str(q2(agg["dona"] or 0)), "summa": int(agg["summa"] or 0),
                     "ozi_qolda": own, "integratsiya": auto}
    prints = ChopEtish.objects.filter(moment__gte=start, moment__lt=end, uid=tov.ms_uid) if tov.ms_uid else ChopEtish.objects.none()
    out["chop"] = {"nomi": "Chop etish (sennik/etiketka)", "hodisa": prints.count(),
                   "noyob_tovar": prints.values("product_ms_id").distinct().count()}
    return out


def compute(b: Biriktirish, day: date, rule: KpiQoida, now=None) -> KpiKun:
    now = now or timezone.now()
    tov, wh = b.tovaroved, str(b.warehouse_ms_id)
    cov = coverage(wh, day, rule, now)
    mezon = [m_minus_ulush(wh, day, rule, cov), m_muddat(wh, day, rule, now, cov),
             m_ochiq(wh, day, rule, now, cov), m_sennik(rule)]
    used = [x for x in mezon if x["ball"] is not None and x["vazn"]]
    ish = None
    if used:
        ish = q2(sum(Decimal(x["ball"]) * x["vazn"] for x in used) / sum(x["vazn"] for x in used))
    rec = davomat_manba(tov, day, now)
    if rec is None:
        davomat = {"ball": None, "sabab": "Sevimli HR hali ulanmagan — davomat ma'lumoti yo'q"}
    elif isinstance(rec, str):
        davomat = {"ball": None, "sabab": rec}
    else:
        davomat = davomat_baho(rec, rule.qoshimcha_ball_soat, rule.qoshimcha_ball_max)
    bonus = Decimal(davomat.get("bonus") or 0)
    umumiy = None
    if ish is not None and davomat["ball"] is not None:
        w = rule.w_ish + rule.w_davomat
        umumiy = q2((ish * rule.w_ish + Decimal(davomat["ball"]) * rule.w_davomat) / w + bonus)
    for x in mezon:
        if x.get("ball") is not None:
            x["ball"] = str(x["ball"])
    tarkib = {
        "qoida": rule.snapshot(),
        "mezonlar": mezon,
        "ish_formula": (" + ".join(f"{x['ball']}×{x['vazn']}" for x in used) +
                        f" / {sum(x['vazn'] for x in used)}") if used else "",
        "davomat": davomat,
        "qamrov": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in cov.items() if k != "scans"},
        "hajm": volume(tov, wh, day),
        "dokon": b.warehouse_name,
    }
    obj, _ = KpiKun.objects.update_or_create(tovaroved=tov, sana=day, defaults={
        "warehouse_ms_id": wh, "warehouse_name": b.warehouse_name, "qoida_version": rule.version,
        "ish_ball": ish, "bonus": bonus, "umumiy": umumiy, "tarkib": tarkib,
        "davomat_ball": Decimal(davomat["ball"]) if davomat["ball"] is not None else None,
    })
    return obj


def recompute_day(day: date, rule: KpiQoida | None = None, now=None) -> int:
    rule = rule or KpiQoida.current()
    seen, n = set(), 0
    for b in assignments_on(day):
        if b.tovaroved_id in seen:
            continue  # bir kunda bitta do'kon (eng oxirgi biriktirish)
        seen.add(b.tovaroved_id)
        compute(b, day, rule, now)
        n += 1
    return n


def recompute_recent(now=None) -> int:
    """Bugun va oxirgi kunlar (qayta paydo bo'lish oynasi) qayta hisoblanadi;
    undan eski kunlar o'z qoida versiyasi bilan qoladi."""
    now = now or timezone.now()
    rule = KpiQoida.current()
    today = ms.local_date(now)
    return sum(recompute_day(today - timedelta(days=i), rule, now) for i in range(rule.qayta_paydo_kun + 2))


def period(tov, start: date, end: date) -> dict:
    """Davr (masalan oy) bahosi — shu davrda saqlangan kunlik natijalardan."""
    days = list(KpiKun.objects.filter(tovaroved=tov, sana__gte=start, sana__lte=end).order_by("sana"))
    ish = [d.ish_ball for d in days if d.ish_ball is not None]
    umumiy = [d.umumiy for d in days if d.umumiy is not None]
    dav = [d.davomat_ball for d in days if d.davomat_ball is not None]
    soat = [Decimal(str(h)) for h in ((d.tarkib or {}).get("davomat", {}).get("ishlangan_soat") for d in days)
            if h is not None]
    return {
        "kunlar": len(days),
        "ish_kunlar": len(ish),
        "ish": q2(sum(ish) / len(ish)) if ish else None,
        "davomat": q2(sum(dav) / len(dav)) if dav else None,
        "davomat_kunlar": len(dav),
        "soat": q2(sum(soat)) if soat else None,
        "tekshiruv": sum(1 for d in days if (d.tarkib or {}).get("davomat", {}).get("tekshiruv")),
        "umumiy": q2(sum(umumiy) / len(umumiy)) if umumiy else None,
        "yetarli_emas": len(days) - len(ish),
        "versiyalar": sorted({d.qoida_version for d in days}),
    }
