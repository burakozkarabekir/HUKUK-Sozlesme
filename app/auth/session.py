"""Signed cookies (HMAC-SHA256) for the Entra session and the login flow state.

Payload is base64url JSON, not encrypted: it holds no secrets beyond the user's
identity and group-derived role. Expiry is enforced server-side.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

SESSION_COOKIE = "sozlesme_session"
FLOW_COOKIE = "sozlesme_auth_flow"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    def __init__(self, secret: str) -> None:
        # VARSAYIM: Without SESSION_SECRET_KEY a random per-process key is used (dev only;
        # entra mode requires the setting so all workers share it).
        self._key = (secret or secrets.token_urlsafe(48)).encode()

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode(), hashlib.sha256).digest())

    def dumps(self, data: dict[str, Any], max_age: int) -> str:
        payload = _b64(json.dumps({**data, "exp": int(time.time()) + max_age}, separators=(",", ":")).encode())
        return f"{payload}.{self._sign(payload)}"

    def loads(self, token: str | None) -> dict[str, Any] | None:
        if not token or "." not in token:
            return None
        payload, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(signature, self._sign(payload)):
            return None
        try:
            data = json.loads(_unb64(payload))
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or int(data.get("exp", 0)) < time.time():
            return None
        return data
