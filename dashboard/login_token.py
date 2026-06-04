"""HMAC-signed, expiring, optionally single-use tokens for dashboard login.

Used by BOTH the dashboard (verify a login token; mint/verify the session
cookie) and the Telegram bot (mint a login token). The `secret` is the
dashboard's random password (rotates on dashboard restart), so old tokens die.
"""

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional, Set


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def mint(secret: str, ttl: int = 120) -> str:
    """Return a signed token valid for `ttl` seconds."""
    payload = {"exp": int(time.time()) + int(ttl), "nonce": os.urandom(8).hex()}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_sign(secret, body)}"


def verify(secret: str, token: str, used: Optional[Set[str]] = None) -> bool:
    """True iff `token` has a valid signature, is unexpired, and (if `used` is
    given) has an unseen nonce — which is then recorded (single-use). Never
    raises; any malformed/tampered/expired/reused token returns False."""
    try:
        body, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, _sign(secret, body)):
            return False
        payload = json.loads(_unb64(body))
        if int(payload["exp"]) < time.time():
            return False
        if used is not None:
            nonce = payload.get("nonce")
            if nonce in used:
                return False
            used.add(nonce)
        return True
    except Exception:
        return False
