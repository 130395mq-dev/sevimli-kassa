"""Server-signed catalog prices preserve legitimate offline prices."""
from django.core import signing
from catalog.models import Product

SALT = "sevimli.catalog-price.v1"
# Catalog snapshots are reusable offline; signature proves that the price was
# issued centrally. Current cashier permissions still determine allowed types.
def quote(product, register):
    return signing.dumps({"product": product.pk, "register": register.pk,
                          "base": int(product.sale_price),
                          "prices": product.prices or {}}, salt=SALT, compress=True)

def validate(raw, register, allowed_types, default_type, selected_type):
    product = Product.objects.filter(pk=raw.get("product_id")).first()
    if not product:
        raise ValueError("Tovar katalogda topilmadi")
    if raw.get("ms_product_id") and str(raw["ms_product_id"]) != str(product.ms_id):
        raise ValueError("Tovar identifikatorlari mos emas")
    selected = selected_type or default_type
    if selected and selected not in {str(p["id"]) for p in allowed_types}:
        raise ValueError("Bu narx turiga ruxsat yo'q")
    if selected != default_type and not register.settings.allow_price_type_switch:
        raise ValueError("Narx turi faqat markazdan belgilanadi")
    prices, base = product.prices or {}, int(product.sale_price)
    token = raw.get("price_quote")
    if token:
        try:
            data = signing.loads(token, salt=SALT)
            if data["product"] != product.pk or data["register"] != register.pk:
                raise ValueError()
            prices, base = data["prices"], data["base"]
        except (signing.BadSignature, ValueError, KeyError, TypeError):
            raise ValueError("Narx tasdig'i noto'g'ri")
    expected = int(prices.get(selected) or base)
    if int(raw.get("price") or 0) != expected:
        raise ValueError("Narx markazdagi katalogga mos emas. Katalogni yangilang.")
