"""HTTP Basic authentication against bcrypt hashes from BASIC_AUTH_USERS (test environment)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import threading
import time
from collections import deque

import bcrypt

from app.config import Settings
from app.models import CurrentUser

CACHE_SECONDS = 600
MAX_FAILURES = 10
FAILURE_WINDOW_SECONDS = 300


def parse_users(raw: str) -> dict[str, bytes]:
    """'ali:$2b$12$...,veli:$2b$12$...' -> {username: hash}."""
    users: dict[str, bytes] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        username, sep, hashed = item.partition(":")
        if not sep or not username.strip() or not hashed.strip().startswith("$2"):
            raise ValueError(f"BASIC_AUTH_USERS girdisi geçersiz: {username or item[:10]}…")
        users[username.strip()] = hashed.strip().encode()
    return users


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


class BasicAuthenticator:
    def __init__(self, settings: Settings) -> None:
        self._users = parse_users(settings.BASIC_AUTH_USERS.get_secret_value())
        self._admins = set(settings.basic_auth_admins)
        self._cache: dict[str, float] = {}  # sha256(credentials) -> valid until
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        # Compared against for unknown users, so timing does not reveal valid names.
        self._dummy_hash = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt(rounds=12))

    def blocked(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            failures = self._failures.get(ip)
            while failures and failures[0] < now - FAILURE_WINDOW_SECONDS:
                failures.popleft()
            return bool(failures) and len(failures) >= MAX_FAILURES

    def _fail(self, ip: str) -> None:
        with self._lock:
            self._failures.setdefault(ip, deque()).append(time.monotonic())

    def authenticate(self, header: str | None, ip: str) -> CurrentUser | None:
        if not header or not header.lower().startswith("basic "):
            return None
        try:
            decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            self._fail(ip)
            return None
        username, _, password = decoded.partition(":")
        key = hashlib.sha256(decoded.encode()).hexdigest()
        now = time.monotonic()
        with self._lock:
            valid_until = self._cache.get(key, 0.0)
        if valid_until < now:
            hashed = self._users.get(username)
            # Always run bcrypt (dummy hash for unknown users) to avoid user enumeration by timing.
            target = hashed or self._dummy_hash
            if not bcrypt.checkpw(password.encode(), target) or hashed is None:
                self._fail(ip)
                return None
            with self._lock:
                self._cache[key] = now + CACHE_SECONDS
        return CurrentUser(username=username, display_name=username, is_admin=username in self._admins)
