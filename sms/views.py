"""Panel: «Mijozlarga SMS» sahifasi. Faqat egasi ko'radi va o'zgartiradi —
boshqaruvchi va tovarovedga yopiq (dashboard/access.py ro'yxatlarida yo'q,
bu yerda ham qo'shimcha tekshiriladi)."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone

from dashboard import access

from . import provayder as prov
from . import xizmat
from .models import SmsSozlama, SmsXabar

#: Panel sahifasi SMS xizmatini uzoq kutib qolmasin
PANEL_TIMEOUT = (3, 6)


def _hozir():
    """Hozirgi Toshkent vaqti (sinovlarda almashtiriladi)."""
    return timezone.localtime()


def _owner(request) -> bool:
    return access.manager_of(request.user) is None and access.tovaroved_of(request.user) is None


def _son(value, kamida: int, kopi: int) -> int | None:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if kamida <= n <= kopi else None


def _sozlamani_saqla(request, s: SmsSozlama, p) -> None:
    matn = " ".join((request.POST.get("matn") or "").split())
    soat = _son(request.POST.get("soat"), SmsSozlama.SOAT_MIN, SmsSozlama.SOAT_MAX)
    chegara = _son(request.POST.get("kunlik_chegara"), 1, 5000)
    if not matn:
        messages.error(request, "Tabrik matni bo'sh bo'lmasin.")
        return
    if len(matn) > 480:
        messages.error(request, "Matn juda uzun (480 belgidan oshmasin).")
        return
    if soat is None:
        messages.error(request, f"Yuborish soati {SmsSozlama.SOAT_MIN}:00 dan {SmsSozlama.SOAT_MAX}:00 gacha "
                                "bo'lsin — kechqurun va tunda SMS yuborish mumkin emas.")
        return
    if chegara is None:
        messages.error(request, "Kunlik chegara 1 dan 5000 gacha bo'lsin.")
        return
    matn_ozgardi = matn != s.matn
    s.yoqilgan = request.POST.get("yoqilgan") == "on"
    s.matn, s.soat, s.kunlik_chegara = matn, soat, chegara
    s.updated_by = (getattr(request.user, "username", "") or "")[:150]
    s.save()
    xabar = "Saqlandi."
    if s.yoqilgan and p is None:
        xabar += " SMS xizmati hali ulanmagan — ulanmaguncha SMS ketmaydi."
    elif matn_ozgardi and p is not None:
        xabar += " Matn o'zgardi — u SMS xizmatida ham aynan shunday tasdiqlangan bo'lishi kerak."
    messages.success(request, xabar)


def _sinov(request, p) -> None:
    matn = None                          # None — joriy tabrik matni
    if request.POST.get("nima") == "xizmat":
        matn = getattr(p, "sinov_matni", "") if p is not None else ""
        if not matn:
            messages.error(request, "Bu SMS xizmatida alohida sinov matni yo'q.")
            return
    try:
        x = xizmat.sinov_yubor(request.POST.get("telefon") or "",
                               kim=getattr(request.user, "username", "") or "", matn=matn, p=p)
    except xizmat.SmsXato as e:
        messages.error(request, str(e))
        return
    if x.holat == SmsXabar.YUBORILDI:
        messages.success(request, f"Sinov SMS yuborildi: {x.telefon_korinish}. Telefonni tekshiring.")
    else:
        messages.error(request, f"Sinov SMS yuborilmadi: {x.xato or x.get_holat_display()}")


def _holat_matni(s: SmsSozlama, p, shablon, local) -> tuple[str, str]:
    """(rang, matn) — hozir tabriklar ketyaptimi, ketmasa nega."""
    if p is None:
        return "warn", "SMS xizmati ulanmagan — SMS yuborilmaydi."
    if not s.yoqilgan:
        return "warn", "Tabrik o'chirilgan — SMS yuborilmaydi."
    if shablon is not None and not shablon.ruxsat:
        return "bad", shablon.izoh
    hozir = local.hour * 60 + local.minute
    if hozir < max(s.soat, xizmat.BOSHLANISH_SOAT) * 60:
        return "ok", f"Yoqilgan. Bugungi tabriklar soat {s.soat}:00 dan yuboriladi."
    if hozir >= xizmat.TUGASH_DAQIQA:
        return "ok", "Yoqilgan. Bugungi vaqt tugadi (17:45 gacha yuboriladi) — keyingisi ertaga."
    return "ok", "Yoqilgan. Bugun tug'ilganlarga tabrik yuborilmoqda (bir necha daqiqada bir tekshiriladi)."


@login_required
def bosh(request):
    if not _owner(request):
        return access.forbidden(request)
    s = SmsSozlama.get()
    p = prov.joriy(timeout=PANEL_TIMEOUT)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "sozlama":
            _sozlamani_saqla(request, s, p)
        elif action == "sinov":
            _sinov(request, p)
        elif action == "qayta":
            if xizmat.vaqt_tugadi(_hozir()):
                messages.error(request, "Bugungi SMS vaqti tugagan (17:45 gacha yuboriladi) — "
                                        "bugun endi yuborib bo'lmaydi.")
            else:
                n = xizmat.xatolarni_qayta_navbatga()
                messages.success(request, f"{n} ta tabrik qayta navbatga qo'yildi — yuborish vaqtida o'zi ketadi."
                                 if n else "Qayta yuboriladigan tabrik yo'q.")
        elif action == "yangila" and p is not None:
            xizmat.balans(p, yangi=True)
            xizmat.shablon_holati(p, s.matn, yangi=True)
            messages.success(request, "SMS xizmatidan ma'lumot yangilandi.")
        return redirect("sms:bosh")

    local = _hozir()
    today = local.date()
    balans, balans_xato, shablon = None, "", None
    if p is not None:
        balans, balans_xato = xizmat.balans(p)
        shablon = xizmat.shablon_holati(p, s.matn)
    rang, holat = _holat_matni(s, p, shablon, local)
    bugun = xizmat.bugungi_royxat(today)
    ketadigan = xizmat.haqiqiy_matn(p, s.matn) if p is not None else s.matn
    return render(request, "sms/bosh.html", {
        "s": s,
        "kod": xizmat.kodlash(ketadigan),
        # xizmat matnni o'z shablonidagi yozuv bilan yuboradi va u hisobni o'zgartiradi
        "kod_xizmatdan": xizmat.kodlash(ketadigan) != xizmat.kodlash(s.matn),
        "soatlar": range(SmsSozlama.SOAT_MIN, SmsSozlama.SOAT_MAX + 1),
        "xizmat": p,
        "sinov_matni": getattr(p, "sinov_matni", "") if p is not None else "",
        "balans": balans, "balans_xato": balans_xato, "shablon": shablon,
        "rang": rang, "holat": holat,
        "stat": xizmat.statistika(),
        "bugun": bugun,
        "bugun_xato": sum(1 for r in bugun if r["xabar"] and r["xabar"].holat == SmsXabar.XATO),
        "qayta_mumkin": not xizmat.vaqt_tugadi(local),
        "yaqin": xizmat.yaqin_kunlar(today),
        "oy": xizmat.oy_hisobi(),
        "jurnal": SmsXabar.objects.select_related("customer")[:50],
        "today": today,
    })
