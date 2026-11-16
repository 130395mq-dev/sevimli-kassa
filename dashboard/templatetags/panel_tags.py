"""Panel karkasi uchun teglar: `{% panel_menu as menu %}` — tepa menyu (dashboard/menu.py)."""

from django import template

from dashboard import access, menu

register = template.Library()


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
