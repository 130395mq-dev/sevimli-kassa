from django import template

register = template.Library()


@register.filter
def get_item(mapping, key):
    """{{ names|get_item:h.store_ms_id }} — UUID kalit satr sifatida qidiriladi."""
    if not mapping or key in (None, ""):
        return ""
    return mapping.get(str(key), "")
