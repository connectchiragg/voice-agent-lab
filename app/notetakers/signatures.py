"""Webhook signatures, standard library only (importable by the worker and the tests).

- Standard Webhooks (https://www.standardwebhooks.com), used by Fathom
  (https://developers.fathom.ai/webhooks) and Granola (https://docs.granola.ai/webhooks).
  (Fireflies is used only for a one-time history import, so it has no webhook here.)
"""
import base64
import binascii
import hashlib
import hmac
import time

TOLERANCE_S = 300


def verify_standard_webhook(secret: str, headers: dict[str, str], body: bytes,
                            now: float | None = None) -> bool:
    """Standard Webhooks (https://www.standardwebhooks.com), as Fathom signs its webhooks:
    base64(HMAC-SHA256(key, f"{id}.{timestamp}.{body}")) against each "v1,<sig>" in
    `webhook-signature`; the key is the base64 part of "whsec_<base64>"."""
    msg_id, stamp, signatures = (headers.get("webhook-id", ""), headers.get("webhook-timestamp", ""),
                                 headers.get("webhook-signature", ""))
    if not (secret and msg_id and stamp.isdigit() and signatures):
        return False
    if abs((time.time() if now is None else now) - int(stamp)) > TOLERANCE_S:
        return False
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except (ValueError, binascii.Error):
        return False
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{stamp}.".encode() + body,
                                         hashlib.sha256).digest()).decode()
    for candidate in signatures.split():
        version, _, signature = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(signature, expected):
            return True
    return False
