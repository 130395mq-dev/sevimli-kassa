"""Panel tepasidagi aloqa chiroqlari qatori.

`{% aloqa_strip %}` — base.html'da, har sahifada. Sahifa ochilganda
server tomonda chiziladi (JS'siz ham ko'rinadi), keyin sahifadagi kichik
skript /aloqa.json dan 15 soniyada bir so'rab, ranglarni yangilab turadi.
"""

from django import template

from sales import aloqa

register = template.Library()


@register.inclusion_tag("dashboard/_aloqa.html", takes_context=True)
def aloqa_strip(context):
    request = context.get("request")
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated):
        return {"snap": None}
    return {"snap": aloqa.snapshot()}


@register.inclusion_tag("dashboard/_dot.html")
def dot(link, label=""):
    """Bitta chiroq: {% dot r.link %} yoki {% dot r.link "Kassa-1" %}."""
    return {"link": link, "label": label}
