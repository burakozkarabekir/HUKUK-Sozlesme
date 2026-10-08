"""Authentication & request guards.

`AuthMiddleware` resolves the user for every request (none / basic / entra),
applies the per-user rate limit and the CSRF header check, and puts the user
into `request.state.user` and the log context. Endpoints enforce access with
the `get_current_user` / `require_admin` dependencies.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any

from fastapi import Depends, HTTPException, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth.session import SESSION_COOKIE, Signer
from app.config import Settings, get_settings
from app.logging_conf import user_var
from app.models import CurrentUser

# VARSAYIM: In AUTH_MODE=none (dev only) every request acts as a local admin.
DEV_USER = CurrentUser(username="gelistirici", display_name="Geliştirici", is_admin=True)

_PUBLIC_PATHS = {"/health"}
_PUBLIC_PREFIXES = ("/static/", "/auth/", "/favicon")
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class RateLimiter:
    """Sliding one-minute window per key. VARSAYIM: in-memory, per worker process."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - 60:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return False, max(1, int(60 - (now - hits[0])) + 1)
            hits.append(now)
            return True, 0


def _route_path(scope: Scope) -> str:
    path: str = scope.get("path", "")
    root = scope.get("root_path", "")
    return path[len(root) :] if root and path.startswith(root) else path


async def _json(
    send: Send, status: int, code: str, message: str, headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    body = json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class AuthMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings, signer: Signer) -> None:
        self.app = app
        self.settings = settings
        self.limiter = RateLimiter(settings.RATE_LIMIT_PER_MINUTE)
        self.signer = signer
        self.basic = None
        if settings.AUTH_MODE == "basic":
            from app.auth.basic import BasicAuthenticator

            self.basic = BasicAuthenticator(settings)

    def resolve(self, headers: dict[bytes, bytes], ip: str) -> CurrentUser | None:
        mode = self.settings.AUTH_MODE
        if mode == "none":
            return DEV_USER
        if mode == "basic" and self.basic is not None:
            header = headers.get(b"authorization", b"").decode("latin-1")
            return self.basic.authenticate(header, ip) if header else None
        cookies = _parse_cookies(headers.get(b"cookie", b"").decode("latin-1"))
        data = self.signer.loads(cookies.get(SESSION_COOKIE))
        if not data:
            return None
        return CurrentUser(
            username=data["username"],
            display_name=data.get("name") or data["username"],
            is_admin=bool(data.get("admin")),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = _route_path(scope)
        state: dict[str, Any] = scope.setdefault("state", {})
        if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        ip = scope["client"][0] if scope.get("client") else "-"
        if self.basic is not None and headers.get(b"authorization") and self.basic.blocked(ip):
            await _json(
                send, 429, "too_many_failed_logins", "Çok fazla hatalı giriş denemesi. 5 dakika sonra tekrar deneyin."
            )
            return
        user = self.resolve(headers, ip)
        state["user"] = user
        if user is not None:
            user_var.set(user.username)

        if path.startswith("/api/"):
            if (
                self.settings.AUTH_MODE != "none"
                and scope.get("method") in _UNSAFE_METHODS
                and headers.get(b"x-requested-with") != b"fetch"
            ):
                # Browsers cannot add this header cross-site without a CORS preflight (which we never allow).
                await _json(
                    send,
                    403,
                    "csrf_rejected",
                    "İstek güvenlik kontrolünden geçemedi. Sayfayı yenileyip tekrar deneyin.",
                )
                return
            allowed, retry_after = self.limiter.allow(user.username if user else f"ip:{ip}")
            if not allowed:
                await _json(
                    send,
                    429,
                    "rate_limited",
                    "Çok fazla istek gönderdiniz. Lütfen biraz bekleyip tekrar deneyin.",
                    [(b"retry-after", str(retry_after).encode())],
                )
                return
        await self.app(scope, receive, send)


def _parse_cookies(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep:
            cookies[name] = value
    return cookies


def unauthenticated(settings: Settings) -> HTTPException:
    if settings.AUTH_MODE == "basic":
        return HTTPException(
            status_code=401,
            detail="Bu işlem için giriş yapmanız gerekiyor.",
            headers={"WWW-Authenticate": 'Basic realm="Sozlesme", charset="UTF-8"'},
        )
    return HTTPException(status_code=401, detail="Oturumunuz sona erdi. Lütfen tekrar giriş yapın.")


async def get_current_user(request: Request, settings: Settings = Depends(get_settings)) -> CurrentUser:
    user = getattr(request.state, "user", None)
    if user is None and settings.AUTH_MODE == "none":
        user = DEV_USER  # e.g. when the middleware is not installed (unit tests)
    if user is None:
        raise unauthenticated(settings)
    user_var.set(user.username)
    return user


async def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Bu işlem yalnızca yöneticiler içindir.")
    return user
