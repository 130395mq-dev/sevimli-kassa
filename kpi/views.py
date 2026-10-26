"""Tovarovedlar KPI — panel sahifalari.

Kirish: egasi hammasini ko'radi. Tovaroved (o'z logini bilan) — faqat o'z
kartasi va o'z do'koni minuslari (dashboard/access.py). Market boshqaruvchisi
bu bo'limni ko'rmaydi.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from catalog.models import SyncState, Warehouse
from dashboard import access

from . import hisob, ms
from .models import (Biriktirish, ChopEtish, Hujjat, KpiKun, KpiQoida, MinusIssue, MinusScan,
                     MsXodim, Tovaroved)

DAVRLAR = [("bugun", "Bugun"), ("kecha", "Kecha"), ("hafta", "Oxirgi 7 kun"),
           ("oy", "Shu oy"), ("otgan_oy", "O'tgan oy")]


def _today() -> date:
    return ms.local_date(timezone.now())


def davr_range(key: str) -> tuple[str, date, date]:
    t = _today()
    if key == "kecha":
        return key, t - timedelta(days=1), t - timedelta(days=1)
    if key == "hafta":
        return key, t - timedelta(days=6), t
    if key == "oy":
        return key, t.replace(day=1), t
    if key == "otgan_oy":
        last = t.replace(day=1) - timedelta(days=1)
        return key, last.replace(day=1), last
    return "bugun", t, t


def _bounds(start: date, end: date):
    return ms.day_bounds(start)[0], ms.day_bounds(end)[1]


def _owner(request) -> bool:
    return access.manager_of(request.user) is None and access.tovaroved_of(request.user) is None


def _forbid(request):
    return access.forbidden(request)


def _current_assignment(tov: Tovaroved, day: date | None = None) -> Biriktirish | None:
    day = day or _today()
    return (tov.biriktirishlar.filter(boshlanish__lte=day).filter(Q(tugash__isnull=True) | Q(tugash__gte=day))
            .order_by("-boshlanish", "-pk").first())


def _status() -> dict:
    states = {s.entity: s for s in SyncState.objects.filter(entity__startswith="kpi:")}
    last_scan = MinusScan.objects.order_by("-at").first()
    names = {"kpi:minus": "Minus tekshiruvi", "kpi:hujjatlar": "Hujjatlar", "kpi:chop": "Chop etish (audit)",
             "kpi:ochirilgan": "O'chirilgan hujjatlar", "kpi:xodimlar": "MoySklad xodimlari",
             "kpi:hisob": "KPI hisobi"}
    rows = [{"name": label, "ok": bool(states.get(k) and states[k].last_success_at),
             "at": states[k].last_success_at if states.get(k) else None,
             "error": states[k].last_error if states.get(k) else ""} for k, label in names.items()]
    return {"rows": rows, "last_scan": last_scan, "enabled": Biriktirish.objects.exists()}


def _late(i: MinusIssue) -> bool:
    """Muddatida tuzatilmagani ANIQ ma'lum (uzilish tufayli noaniqlari — yo'q)."""
    if i.reopened:
        return True
    if i.closed_at is None:
        return i.last_seen >= i.deadline
    if i.closed_at <= i.deadline:
        return False
    return not (i.closed_prev_scan and i.closed_prev_scan < i.deadline)


def _issue_stats(wh, start, end, now) -> dict:
    s, e = _bounds(start, end)
    e = min(e, now)
    base = MinusIssue.objects.filter(warehouse_ms_id=wh).exclude(confirmed_cause="xato_emas")
    late = [i for i in base.filter(deadline__gte=s, deadline__lt=e) if _late(i)]
    open_qs = base.filter(status=MinusIssue.OPEN)
    return {
        "yangi": base.filter(first_seen__gte=s, first_seen__lt=e, pre_tracking=False).count(),
        "tuzatilgan": base.filter(closed_at__gte=s, closed_at__lt=e).count(),
        # Tuzatilgan, lekin qisqa vaqtda yana minusga tushgan — tuzatish hisoblanmaydi
        "qayta": base.filter(closed_at__gte=s, closed_at__lt=e, reopened=True).count(),
        "kechikkan": len(late),
        "ochiq": open_qs.count(),
        "ochiq_kech": open_qs.filter(deadline__lt=now).count(),
        "oldingi": open_qs.filter(pre_tracking=True).count(),
    }


def _volume(wh, start, end) -> dict:
    s, e = _bounds(start, end)
    rows = (Hujjat.objects.filter(created__gte=s, created__lt=e, deleted_at__isnull=True)
            .filter(Q(store_ms_id=wh) | Q(target_ms_id=wh))
            .values("kind").annotate(n=Count("id"), tur=Sum("products_count"), dona=Sum("quantity"), summa=Sum("sum")))
    by = {r["kind"]: r for r in rows}
    return [{"kind": k, "nomi": n, **{x: (by.get(k) or {}).get(x) or 0 for x in ("n", "tur", "dona", "summa")}}
            for k, n in Hujjat.KINDS]


def _prints(uid, start, end) -> dict:
    if not uid:
        return {"hodisa": 0, "noyob": 0}
    s, e = _bounds(start, end)
    rows = list(ChopEtish.objects.filter(uid=uid, moment__gte=s, moment__lt=e).values_list("moment", "product_ms_id"))
    # Bir kunda bir tovarni qayta-qayta chop etish — bitta
    return {"hodisa": len(rows), "noyob": len({(ms.local_date(m), p) for m, p in rows})}


@login_required
def bosh(request):
    if not _owner(request):
        return _forbid(request)
    key, start, end = davr_range(request.GET.get("davr", "bugun"))
    now = timezone.now()
    last_scan = MinusScan.objects.order_by("-at").first()
    cards = []
    for tov in Tovaroved.objects.filter(active=True).select_related("ms_xodim"):
        b = _current_assignment(tov)
        p = hisob.period(tov, start, end)
        row = {"tov": tov, "b": b, "p": p}
        if b:
            wh = str(b.warehouse_ms_id)
            row["st"] = _issue_stats(wh, start, end, now)
            neg, active = ((last_scan.per_wh or {}).get(wh) or [None, None]) if last_scan else (None, None)
            row["neg"], row["active"] = neg, active
            row["share"] = round(neg * 100 / active, 2) if neg is not None and active else None
            row["vol"] = _volume(wh, start, end)
        row["prints"] = _prints(tov.ms_uid, start, end)
        cards.append(row)

    def rank(r):
        p = r["p"]
        return (p["umumiy"] is None, -(p["umumiy"] or 0), p["ish"] is None, -(p["ish"] or 0), r["tov"].name)

    cards.sort(key=rank)
    return render(request, "kpi/bosh.html", {
        "cards": cards, "davr": key, "davrlar": DAVRLAR, "start": start, "end": end,
        "status": _status(), "rule": KpiQoida.current(),
    })


@login_required
def men(request):
    tov = access.tovaroved_of(request.user)
    if tov is None:
        return redirect("kpi:bosh")
    return redirect("kpi:xodim", pk=tov.pk)


@login_required
def xodim(request, pk):
    tov = get_object_or_404(Tovaroved, pk=pk)
    me = access.tovaroved_of(request.user)
    if not _owner(request) and (me is None or me.pk != tov.pk):
        return _forbid(request)
    key, start, end = davr_range(request.GET.get("davr", "oy"))
    s, e = _bounds(start, end)
    b = _current_assignment(tov)
    days = list(KpiKun.objects.filter(tovaroved=tov, sana__gte=start, sana__lte=end).order_by("-sana"))
    whs = {str(x.warehouse_ms_id) for x in tov.biriktirishlar.all()}
    issues = (MinusIssue.objects.filter(warehouse_ms_id__in=list(whs))
              .filter(Q(status=MinusIssue.OPEN) | Q(closed_at__gte=s, closed_at__lt=e))
              .order_by("status", "deadline")[:200])
    docs = []
    if b:
        docs = list(Hujjat.objects.filter(created__gte=s, created__lt=e)
                    .filter(Q(store_ms_id=b.warehouse_ms_id) | Q(target_ms_id=b.warehouse_ms_id))
                    .order_by("-created")[:150])
    prints = list(ChopEtish.objects.filter(uid=tov.ms_uid, moment__gte=s, moment__lt=e)[:150]) if tov.ms_uid else []
    return render(request, "kpi/xodim.html", {
        "tov": tov, "b": b, "p": hisob.period(tov, start, end), "days": days, "issues": issues,
        "docs": docs, "prints": prints, "history": tov.biriktirishlar.all(),
        "davr": key, "davrlar": DAVRLAR, "start": start, "end": end, "owner": _owner(request),
        "names": {str(k): v for k, v in Warehouse.objects.values_list("ms_id", "name")},
        "now": timezone.now(),
    })


@login_required
def minuslar(request):
    if not _owner(request):
        return _forbid(request)
    holat = request.GET.get("holat", "open")
    dokon = request.GET.get("dokon", "")
    qs = MinusIssue.objects.all()
    now = timezone.now()
    if holat == "open":
        qs = qs.filter(status=MinusIssue.OPEN)
    elif holat == "kech":
        qs = qs.filter(status=MinusIssue.OPEN, deadline__lt=now)
    elif holat == "closed":
        qs = qs.filter(status=MinusIssue.CLOSED)
    if dokon:
        qs = qs.filter(warehouse_ms_id=dokon)
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(barcode=q))
    total = qs.count()
    stores = sorted({(str(w), n) for w, n in MinusIssue.objects.values_list("warehouse_ms_id", "warehouse_name").distinct()},
                    key=lambda x: x[1])
    return render(request, "kpi/minuslar.html", {
        "rows": qs.select_related("tovaroved").order_by("deadline")[:300], "total": total,
        "holat": holat, "dokon": dokon, "q": q, "stores": stores, "now": now,
    })


@login_required
def minus(request, pk):
    issue = get_object_or_404(MinusIssue.objects.select_related("tovaroved", "product"), pk=pk)
    owner = _owner(request)
    if not owner:
        me = access.tovaroved_of(request.user)
        own = {str(w) for w in me.biriktirishlar.values_list("warehouse_ms_id", flat=True)} if me else set()
        if str(issue.warehouse_ms_id) not in own:
            return _forbid(request)
    if request.method == "POST":
        if not owner:
            return _forbid(request)
        cause = request.POST.get("cause", "")
        if cause and cause not in dict(MinusIssue.CAUSES):
            messages.error(request, "Sabab noto'g'ri.")
            return redirect("kpi:minus", pk=pk)
        issue.confirmed_cause = cause
        issue.confirmed_note = (request.POST.get("note") or "").strip()[:2000]
        ok = request.POST.get("resolution_ok", "")
        issue.resolution_ok = {"1": True, "0": False}.get(ok)
        dl = request.POST.get("deadline", "")
        if dl:
            try:
                naive = datetime.strptime(dl, "%Y-%m-%dT%H:%M")
                issue.deadline = naive.replace(tzinfo=ms.TASHKENT)
            except ValueError:
                messages.error(request, "Muddat sanasi noto'g'ri.")
                return redirect("kpi:minus", pk=pk)
        issue.confirmed_by = request.user.get_username()
        issue.confirmed_at = timezone.now()
        issue.save()
        # Shu minusga tegishli kunlar yangi tasdiq bilan qayta hisoblansin
        for d in {ms.local_date(issue.first_seen), ms.local_date(issue.deadline)}:
            if d <= _today():
                hisob.recompute_day(d)
        messages.success(request, "Saqlandi. Tegishli kunlar KPI'si qayta hisoblandi.")
        return redirect("kpi:minus", pk=pk)
    return render(request, "kpi/minus.html", {
        "i": issue, "owner": owner, "causes": MinusIssue.CAUSES, "now": timezone.now(),
        "names": {str(k): v for k, v in Warehouse.objects.values_list("ms_id", "name")},
        "deadline_local": issue.deadline.astimezone(ms.TASHKENT).strftime("%Y-%m-%dT%H:%M"),
    })


def _time(v: str):
    try:
        return time.fromisoformat(v) if v else None
    except ValueError:
        return None


@login_required
def boglash(request):
    if not _owner(request):
        return _forbid(request)
    from dashboard.views import _LOGIN_RE, _MIN_PASSWORD, _markets, _password_problem

    User = get_user_model()
    markets = _markets()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "xodimlar":
            from . import hujjatlar, ishlar
            client = ishlar.make_client()
            if client is None:
                messages.error(request, "MoySklad tokeni sozlanmagan.")
            else:
                try:
                    n = hujjatlar.sync_employees(client)
                    messages.success(request, f"MoySklad'dan {n} ta xodim o'qildi (faqat o'qish).")
                except Exception as exc:
                    messages.error(request, f"MoySklad javob bermadi: {type(exc).__name__}")
            return redirect("kpi:boglash")

        if action == "save":
            tov = Tovaroved.objects.filter(pk=request.POST.get("id")).first() or Tovaroved()
            name = (request.POST.get("name") or "").strip()[:255]
            if not name:
                messages.error(request, "Ismini yozing.")
                return redirect("kpi:boglash")
            tov.name = name
            tov.ms_xodim = MsXodim.objects.filter(pk=request.POST.get("ms_xodim") or None).first()
            tov.ms_uid = tov.ms_xodim.uid if tov.ms_xodim else (request.POST.get("ms_uid") or "").strip()[:255]
            tov.hr_xodim_id = (request.POST.get("hr_id") or "").strip()[:64]
            tov.hr_xodim_nomi = (request.POST.get("hr_name") or "").strip()[:255]
            tov.smena_boshi = _time(request.POST.get("smena_boshi", ""))
            tov.smena_oxiri = _time(request.POST.get("smena_oxiri", ""))
            tov.active = request.POST.get("active", "1") == "1"
            tov.save()
            messages.success(request, f"«{tov.name}» saqlandi.")
            return redirect("kpi:boglash")

        tov = Tovaroved.objects.filter(pk=request.POST.get("id")).first()
        if tov is None:
            messages.error(request, "Tovaroved topilmadi.")
            return redirect("kpi:boglash")

        if action == "biriktir":
            market = next((m for m in markets if m["id"] == request.POST.get("warehouse")), None)
            try:
                since = date.fromisoformat(request.POST.get("boshlanish") or "")
            except ValueError:
                since = None
            if market is None or since is None:
                messages.error(request, "Do'kon va boshlanish sanasini tanlang.")
                return redirect("kpi:boglash")
            with transaction.atomic():
                # Avvalgi ochiq biriktirish shu sanadan oldingi kun bilan yopiladi —
                # eski kunlar natijasi eski do'konda qoladi
                for old in tov.biriktirishlar.filter(tugash__isnull=True):
                    if old.boshlanish >= since:
                        messages.error(request, "Yangi sana amaldagi biriktirishdan keyin bo'lishi kerak.")
                        return redirect("kpi:boglash")
                    old.tugash = since - timedelta(days=1)
                    old.save(update_fields=["tugash"])
                Biriktirish.objects.create(tovaroved=tov, warehouse_ms_id=market["id"],
                                           warehouse_name=market["name"], boshlanish=since)
            messages.success(request, f"«{tov.name}» → {market['name']} ({since:%d.%m.%Y} dan).")
        elif action == "tugat":
            b = tov.biriktirishlar.filter(pk=request.POST.get("b")).first()
            try:
                until = date.fromisoformat(request.POST.get("tugash") or "")
            except ValueError:
                until = None
            if b and until and until >= b.boshlanish:
                b.tugash = until
                b.save(update_fields=["tugash"])
                messages.success(request, "Biriktirish yopildi.")
            else:
                messages.error(request, "Tugash sanasi noto'g'ri.")
        elif action == "login":
            username = (request.POST.get("username") or "").strip()
            password = request.POST.get("password1") or ""
            problem = ""
            if tov.user_id:
                problem = "Bu tovarovedning logini bor."
            elif not re.fullmatch(_LOGIN_RE, username):
                problem = "Login 3–30 belgi: lotin harflar, raqamlar, nuqta, chiziqcha."
            elif User.objects.filter(username__iexact=username).exists():
                problem = f"«{username}» logini band."
            else:
                problem = _password_problem(password, request.POST.get("password2") or "", username)
            if problem:
                messages.error(request, problem)
            else:
                with transaction.atomic():
                    tov.user = User.objects.create_user(username=username, password=password, first_name=tov.name[:150])
                    tov.save(update_fields=["user"])
                messages.success(request, f"«{username}» logini yaratildi. U panelga kirsa faqat o'z KPI'sini ko'radi.")
        elif action == "login_off" and tov.user_id:
            tov.user.is_active = not tov.user.is_active
            tov.user.save(update_fields=["is_active"])
            messages.success(request, "Kirish " + ("yoqildi." if tov.user.is_active else "to'xtatildi."))
        return redirect("kpi:boglash")

    return render(request, "kpi/boglash.html", {
        "rows": Tovaroved.objects.select_related("ms_xodim", "user").prefetch_related("biriktirishlar"),
        "xodimlar": MsXodim.objects.filter(archived=False),
        "markets": markets, "today": _today(), "min_password": _MIN_PASSWORD,
    })


_RULE_INT = ["w_ish", "w_davomat", "w_minus_ulush", "w_muddat", "w_ochiq", "w_sennik", "tuzatish_soat",
             "oldingi_tuzatish_soat", "qayta_paydo_kun", "faol_sotuv_kun", "kun_boshi", "kun_oxiri", "min_qamrov"]
_RULE_DEC = ["minus_ulush_chegara", "qoshimcha_ball_soat", "qoshimcha_ball_max"]


@login_required
def qoida(request):
    if not _owner(request):
        return _forbid(request)
    rule = KpiQoida.current()
    if request.method == "POST":
        data, problem = {}, ""
        for f in _RULE_INT:
            try:
                data[f] = int(request.POST.get(f, getattr(rule, f)))
                if data[f] < 0:
                    raise ValueError
            except ValueError:
                problem = f"«{KpiQoida._meta.get_field(f).verbose_name}» — butun musbat son bo'lsin."
        for f in _RULE_DEC:
            try:
                data[f] = Decimal(str(request.POST.get(f, getattr(rule, f))).replace(",", "."))
                if data[f] < 0:
                    raise InvalidOperation
            except InvalidOperation:
                problem = f"«{KpiQoida._meta.get_field(f).verbose_name}» — son bo'lsin."
        data["faol_tovar"] = request.POST.get("faol_tovar", rule.faol_tovar)
        if data["faol_tovar"] not in dict(KpiQoida.FAOL):
            problem = "Faol tovar ta'rifi noto'g'ri."
        if not problem and data["w_ish"] + data["w_davomat"] == 0:
            problem = "Ish va davomat og'irliklari ikkalasi 0 bo'lmasin."
        if not problem and not (0 <= data["kun_boshi"] < data["kun_oxiri"] <= 24):
            problem = "Ish kuni soatlari noto'g'ri."
        if not problem and data["minus_ulush_chegara"] <= 0:
            problem = "Minus ulushi chegarasi 0 dan katta bo'lsin."
        if problem:
            messages.error(request, problem)
            return redirect("kpi:qoida")
        new = KpiQoida.objects.create(version=rule.version + 1, created_by=request.user.get_username(),
                                      izoh=(request.POST.get("izoh") or "").strip()[:255], **data)
        n = 0
        if request.POST.get("qayta") == "1":
            for i in range(min(int(request.POST.get("qayta_kun") or 7), 62)):
                n += hisob.recompute_day(_today() - timedelta(days=i), new)
        messages.success(request, f"Yangi qoida v{new.version} saqlandi." + (f" {n} ta kunlik natija qayta hisoblandi." if n else
                                  " Eski kunlar o'z versiyasida qoldi."))
        return redirect("kpi:qoida")
    return render(request, "kpi/qoida.html", {"rule": rule, "versions": KpiQoida.objects.all()[:20],
                                              "faol": KpiQoida.FAOL})


@login_required
def chop(request):
    if not _owner(request):
        return _forbid(request)
    key, start, end = davr_range(request.GET.get("davr", "bugun"))
    s, e = _bounds(start, end)
    rows = list(ChopEtish.objects.filter(moment__gte=s, moment__lt=e).order_by("-moment")[:500])
    names = dict(Tovaroved.objects.exclude(ms_uid="").values_list("ms_uid", "name"))
    per = defaultdict(lambda: {"hodisa": 0, "noyob": set()})
    for r in rows:
        per[r.uid]["hodisa"] += 1
        per[r.uid]["noyob"].add((ms.local_date(r.moment), r.product_ms_id))
    summary = sorted(({"uid": u, "name": names.get(u, ""), "hodisa": v["hodisa"], "noyob": len(v["noyob"])}
                      for u, v in per.items()), key=lambda x: -x["noyob"])
    return render(request, "kpi/chop.html", {"rows": rows, "summary": summary, "names": names,
                                             "davr": key, "davrlar": DAVRLAR, "start": start, "end": end})
