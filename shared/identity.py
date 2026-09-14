"""One immutable receipt number, available before the first network request."""
import base64
import uuid


def receipt_number(local_uuid):
    # All 128 UUID bits are retained; different tills/shifts cannot reuse a sequence.
    return "SK-" + base64.b32encode(uuid.UUID(str(local_uuid)).bytes).decode('ascii').rstrip('=')
