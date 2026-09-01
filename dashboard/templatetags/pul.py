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
