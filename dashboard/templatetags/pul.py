"""
Pul va sonlarni ko'rsatish uchun shablon filtrlari.

Django'ning `floatformat` i mingliklarni ajratmaydi, `intcomma` esa
vergul qo'yadi (2,347,000). Bizda probel ishlatiladi — chekdagidek,
va o'qish osonroq.
"""

from django import template

register = template.Library()

# Uzilmaydigan probel — raqam qator oxirida ikkiga bo'linib ketmasin
THIN = " "


@register.filter
def som(value) -> str:
    """2347000 → «2 347 000». Kasr qismi tashlanadi."""
    if value in (None, ""):
        return "—"
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return "—"
    sign = "-" if number < 0 else ""
    return sign + f"{abs(number):,}".replace(",", THIN)


@register.filter
def som_aniq(value) -> str:
    """Tiyini bo'lsa ko'rsatadi: 16001.2 → «16 001,20», 2000 → «2 000».

    Vaznli tovar qatorlarida summa tiyinli bo'ladi — `som` uni yaxlitlab
    qo'yardi va chek qatorlari yig'indisi jamiga mos kelmay ko'rinardi.
    """
    if value in (None, ""):
        return "—"
    try:
        tiyin = int(round(float(value) * 100))
    except (TypeError, ValueError):
        return "—"
    sign = "-" if tiyin < 0 else ""
    whole, frac = divmod(abs(tiyin), 100)
    text = f"{whole:,}".replace(",", THIN)
    if frac:
        text += f",{frac:02d}"
    return sign + text


@register.filter
def som_tiyin(value) -> str:
    """Bazadagi tiyin → so'm (tiyini bilan): 1600120 → «16 001,20»."""
    try:
        return som_aniq(int(value) / 100)
    except (TypeError, ValueError):
        return "—"


@register.filter
def mutlaq(value):
    """Ishorasiz qiymat: −50 → 50 (ishorani shablon o'zi qo'yadi)."""
    try:
        return abs(value)
    except TypeError:
        return value
