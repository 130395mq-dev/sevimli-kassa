"""Panel karkasi uchun teglar: `{% panel_menu as menu %}` — tepa menyu (dashboard/menu.py),
`{% statik "…" %}` — statik fayl manzili (shrift)."""

from django import template
from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage

from dashboard import access, menu

register = template.Library()


@register.simple_tag
def statik(path: str) -> str:
    """`{% static %}` kabi — serverda nomiga xesh qo'shilgan manzil (brauzer faylni abadiy
    keshlaydi). Farqi: fayl manifestda bo'lmasa (collectstatic ishlamagan muhit, test) sahifa
    yiqilmaydi — oddiy manzil qaytadi. Shrift yuklanmasa ham panel ochilishi shart."""
    try:
        return staticfiles_storage.url(path)
    except ValueError:
        return settings.STATIC_URL + path


@register.simple_tag(takes_context=True)
def panel_menu(context):
    request = context.get("request")
    if request is None:
        return {"sections": [], "sub": [], "sub_title": ""}
    user = getattr(request, "user", None)
    manager = context.get("panel_manager")
    tovaroved = context.get("panel_tovaroved")
    if "panel_manager" not in context:      # kontekst protsessorisiz chizilgan sahifa
        manager, tovaroved = access.manager_of(user), access.tovaroved_of(user)
    return menu.build(request, manager, tovaroved)
