"""Server-signed catalog prices preserve legitimate offline prices."""
from django.core import signing
from django.db import transaction
from catalog.models import Product
import uuid


def price_types_for(register, settings):
    from catalog.models import PriceType
    from catalog.sync import CatalogSync
    rows = [{"id": str(p.ms_id).lower(), "name": p.name} for p in PriceType.objects.all()]
    if not rows:
        return [], ""
    wanted = (settings.price_type or "").strip().lower()
    for row in rows:
        if wanted and row["name"].strip().lower() == wanted:
            return rows, row["id"]
    store_type = str(register.store.price_type_ms_id or "").lower() if register.store else ""
    if any(row["id"] == store_type for row in rows):
        return rows, store_type
    for row in rows:
        if any(word in row["name"].lower() for word in CatalogSync.RETAIL_WORDS):
            return rows, row["id"]
    return rows, rows[0]["id"]


@transaction.atomic
def policy_for(register, *, acknowledge="", queue_empty=False):
    """Only panel-issued types are accepted during a POS handover.

    The POS acknowledges the current revision only after closing its cart and
    draining the old outbox. A stale acknowledgement (including A-B-A changes)
    cannot retire a newer transition. Legacy clients never acknowledge, so
    their queued sales remain valid until they are updated.
    """
    from sales.models import RegisterSettings, RegisterPricePolicy
    RegisterSettings.objects.get_or_create(register=register)
    settings = RegisterSettings.objects.select_for_update().get(register=register)
    rows, target = price_types_for(register, settings)
    state, _ = RegisterPricePolicy.objects.select_for_update().get_or_create(
        register=register, defaults={"target_type": target, "accepted_types": [target]})
    changed = False
    if state.target_type != target:
        state.target_type = target
        state.revision = uuid.uuid4()
        state.accepted_types = list(dict.fromkeys([*state.accepted_types, target]))
        changed = True
    if queue_empty and acknowledge == str(state.revision):
        if state.accepted_types != [target]:
            state.accepted_types = [target]
            changed = True
    if changed:
        state.save(update_fields=["target_type", "revision", "accepted_types"])
    return rows, target, str(state.revision), set(state.accepted_types)

SALT = "sevimli.catalog-price.v1"
# Catalog snapshots are reusable offline; signature proves that the price was
# issued centrally. Current cashier permissions still determine allowed types.
def quote(product, register):
    return signing.dumps({"product": product.pk, "register": register.pk,
                          "base": int(product.sale_price),
                          "prices": product.prices or {}}, salt=SALT, compress=True)

def validate(raw, register, allowed_types, default_type, selected_type, accepted_types=None):
    product = Product.objects.filter(pk=raw.get("product_id")).first()
    if not product:
        raise ValueError("Tovar katalogda topilmadi")
    if raw.get("ms_product_id") and str(raw["ms_product_id"]) != str(product.ms_id):
        raise ValueError("Tovar identifikatorlari mos emas")
    selected = selected_type or default_type
    if selected and selected not in {str(p["id"]) for p in allowed_types}:
        raise ValueError("Bu narx turiga ruxsat yo'q")
    # Cashiers cannot choose another type, even if a legacy permission remains.
    if selected not in (accepted_types if accepted_types is not None else {default_type}):
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
            # Eski server/release bergan imzo yaroqsiz bo'lishi mumkin.
            # Bunda klient narxiga ishonmaymiz: yuqorida bazadan olingan
            # HOZIRGI markaziy narx bilan tekshirishda davom etamiz. Narx
            # o'zgartirilgan bo'lsa pastdagi expected solishtiruvi rad etadi.
            prices, base = product.prices or {}, int(product.sale_price)
    expected = int(prices.get(selected) or base)
    if int(raw.get("price") or 0) != expected:
        raise ValueError("Narx markazdagi katalogga mos emas. Katalogni yangilang.")
