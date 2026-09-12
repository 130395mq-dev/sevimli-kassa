"""Device-bound, revocable cashier sessions. Never trust a client role."""
import hashlib
from django.core import signing
from sales.models import Cashier, KassaSession
from sales import sessions

SALT = "sevimli.cashier-session.v2"
MAX_AGE = 24 * 3600

def _credential(register, cashier):
    secret = cashier.pin_hash if cashier else register.password_hash
    return hashlib.sha256((register.api_token + ":" + secret).encode()).hexdigest()

def _cashier(register, cashier_id):
    if not cashier_id:
        return None
    cashier = Cashier.objects.filter(pk=cashier_id, active=True).first()
    if cashier is None:
        raise ValueError("Kassir faol emas")
    allowed = register.settings.allowed_cashiers
    if allowed.exists() and not allowed.filter(pk=cashier_id).exists():
        raise ValueError("Bu kassaga kirishga ruxsat yo'q")
    return cashier

def issue(request, who):
    reg = request.register
    device, _ = sessions.device_of(request)
    cashier = _cashier(reg, int(who["id"]))
    row = KassaSession.objects.filter(register=reg, device=device,
                                      cashier_id=who["id"]).first() if device else None
    return signing.dumps({
        "cashier_id": who["id"],
        "register": reg.pk,
        "device": device,
        "generation": row.started_at.isoformat() if row else "",
        "credential": _credential(reg, cashier),
    }, salt=SALT, compress=True)

def verify(request):
    token = (request.headers.get("X-Session") or "").strip()
    try:
        data = signing.loads(token, salt=SALT, max_age=MAX_AGE)
        reg = request.register
        device, _ = sessions.device_of(request)
        if data["register"] != reg.pk or data["device"] != device:
            return None
        cashier = _cashier(reg, int(data["cashier_id"]))
        if data["credential"] != _credential(reg, cashier):
            return None
        if device:
            row = KassaSession.objects.filter(register=reg, device=device,
                                              cashier_id=data["cashier_id"]).first()
            if not row or row.started_at.isoformat() != data["generation"]:
                return None
        return {"cashier_id": data["cashier_id"],
                "is_manager": bool(cashier and cashier.is_manager)}
    except (signing.BadSignature, ValueError, TypeError, KeyError, AttributeError):
        return None
