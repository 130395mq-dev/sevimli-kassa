"""
Davomat bahosi (Sevimli HR ma'lumoti asosida).

Ma'lumot: Sevimli HR'dan olingan nusxa (kpi/hr.py → HrDavomat). HR kaliti
sozlanmagan bo'lsa `manba()` None qaytaradi va KPI'da davomat «Ma'lumot
yetarli emas» bo'lib turadi — soxta raqam yo'q.

Qoidalar (egasining hujjatidan):
  * ruxsatli ta'til/yo'qlik — baholanmaydi (jazo yo'q);
  * chiqish belgisi yo'q yoki yozuv xato — TEKSHIRUVGA chiqadi, ish soati
    hisoblanmaydi (cheksiz soat hosil bo'lmaydi), ball qo'yilmaydi;
  * smenani vaqtida tugatgan xodim to'liq ball oladi;
  * HR'da kechirilgan kechikish/erta ketish (o'rniga boshqa xodim turgan) —
    jazo bermaydi;
  * smena hali tugamagan kun baholanmaydi (kun oxirida baholanadi);
  * shunchaki kech chiqish bonus bermaydi — faqat TASDIQLANGAN qo'shimcha
    soat, chegaralangan bonus bilan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.utils import timezone

MAX_SHIFT_HOURS = 16   # bundan uzun «ishlangan» yozuv — xato deb tekshiruvga


@dataclass
class DavomatKun:
    reja_boshi: datetime | None = None      # smena bo'yicha rejalashtirilgan
    reja_oxiri: datetime | None = None
    kirish: datetime | None = None          # haqiqiy kirish/chiqish (HR)
    chiqish: datetime | None = None
    tanaffus_daqiqa: int = 0
    ruxsatli_yoqlik: bool = False           # tasdiqlangan ta'til/yo'qlik
    tasdiqlangan_qoshimcha_soat: Decimal = Decimal(0)
    kechikish_kechirilgan: bool = False     # HR'da kechirilgan
    erta_ketish_kechirilgan: bool = False
    smena_tugamagan: bool = False           # hozir smena davom etyapti


def baho(k: DavomatKun, ball_soat: Decimal, ball_max: Decimal) -> dict:
    out = {"ball": None, "sabab": "", "tekshiruv": False, "ishlangan_soat": None,
           "kechikish_daqiqa": 0, "erta_ketish_daqiqa": 0, "qoshimcha_soat": "0", "bonus": "0"}
    if k.ruxsatli_yoqlik:
        return {**out, "sabab": "Ruxsatli yo'qlik — baholanmaydi"}
    if not k.reja_boshi or not k.reja_oxiri or k.reja_oxiri <= k.reja_boshi:
        return {**out, "sabab": "Smena jadvali yo'q — baholanmaydi"}
    if k.smena_tugamagan and not k.chiqish:
        return {**out, "sabab": "Smena hali tugamagan — tugagach baholanadi"}
    if not k.kirish:
        return {**out, "ball": "0", "tekshiruv": True,
                "sabab": "Kirish belgisi yo'q va ruxsatli yo'qlik ham yo'q — kelmagan (tekshiring)"}
    if not k.chiqish:
        return {**out, "tekshiruv": True,
                "sabab": "Chiqish belgisi yo'q — ish soati hisoblanmadi, tekshiruvga chiqarildi"}
    if k.chiqish <= k.kirish:
        return {**out, "tekshiruv": True, "sabab": "Davomat yozuvi xato (chiqish kirishdan oldin)"}
    worked_min = (k.chiqish - k.kirish).total_seconds() / 60 - max(0, k.tanaffus_daqiqa)
    if worked_min / 60 > MAX_SHIFT_HOURS:
        return {**out, "tekshiruv": True,
                "sabab": f"{MAX_SHIFT_HOURS} soatdan uzun yozuv — xato bo'lishi mumkin, tekshiruvga chiqarildi"}
    plan_min = (k.reja_oxiri - k.reja_boshi).total_seconds() / 60
    late = max(0, int((k.kirish - k.reja_boshi).total_seconds() // 60))
    early = max(0, int((k.reja_oxiri - k.chiqish).total_seconds() // 60))
    izoh = []
    if late and k.kechikish_kechirilgan:
        izoh.append(f"{late} daqiqa kechikish HR'da kechirilgan")
        late = 0
    if early and k.erta_ketish_kechirilgan:
        izoh.append(f"{early} daqiqa erta ketish HR'da kechirilgan")
        early = 0
    ball = max(Decimal(0), Decimal(100) * (1 - Decimal(late + early) / Decimal(plan_min)))
    over = max(Decimal(0), Decimal(k.tasdiqlangan_qoshimcha_soat or 0))
    bonus = min(over * ball_soat, ball_max)
    return {**out, "ball": str(ball.quantize(Decimal("0.01"))),
            "ishlangan_soat": round(max(0.0, worked_min) / 60, 2),
            "kechikish_daqiqa": late, "erta_ketish_daqiqa": early,
            "qoshimcha_soat": str(over), "bonus": str(bonus),
            "sabab": f"100 × (1 − ({late} + {early}) / {int(plan_min)} daqiqa)"
                     + (f" ({'; '.join(izoh)})" if izoh else "")}


def manba(tovaroved, day, now=None) -> DavomatKun | str | None:
    """Bir kunlik davomat (HR nusxasidan).

    None — Sevimli HR ulanmagan (kalit yo'q); matn — nega baholab bo'lmasligi
    (bog'lanmagan, ma'lumot hali olinmagan); DavomatKun — baholash uchun yozuv.
    """
    from . import hr
    from .models import HrDavomat

    if not hr.configured():
        return None
    hid = (tovaroved.hr_xodim_id or "").strip()
    if not hid:
        return "Tovaroved Sevimli HR xodimiga bog'lanmagan (Bog'lash sahifasida tanlang)"
    row = HrDavomat.objects.filter(hr_xodim_id=hid, sana=day).first()
    if row is None:
        return "Sevimli HR'dan bu kun ma'lumoti hali olinmagan"
    now = now or timezone.now()
    return DavomatKun(
        reja_boshi=row.reja_boshi, reja_oxiri=row.reja_oxiri, kirish=row.kirish, chiqish=row.chiqish,
        ruxsatli_yoqlik=row.dam_kuni, kechikish_kechirilgan=row.kechikish_kechirilgan,
        erta_ketish_kechirilgan=row.erta_ketish_kechirilgan,
        smena_tugamagan=bool(row.reja_oxiri and now < row.reja_oxiri),
    )
