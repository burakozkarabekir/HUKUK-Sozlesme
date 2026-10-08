"""Pure ASGI middleware (safe for SSE streaming, unlike BaseHTTPMiddleware)."""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging_conf import request_id_var, user_var

access_logger = logging.getLogger("app.access")
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")

CSP = (
    "default-src 'self'; img-src 'self' data:; object-src 'self'; frame-src 'self'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'self'"
)
SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    # SAMEORIGIN (not DENY) so our own PDFs can be embedded in the side panel.
    (b"x-frame-options", b"SAMEORIGIN"),
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]
# Swagger UI loads assets from a CDN; it is only enabled outside prod.
_CSP_EXEMPT_SUFFIXES = ("/docs", "/redoc", "/docs/oauth2-redirect")


class RequestContextMiddleware:
    """Assigns a request id, sets security headers and writes the access log."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _REQUEST_ID_RE.fullmatch(incoming) else uuid.uuid4().hex
        rid_token = request_id_var.set(request_id)
        user_token = user_var.set("-")
        started = time.perf_counter()
        status_holder = {"status": 500}
        path: str = scope.get("path", "")
        exempt = path.endswith(_CSP_EXEMPT_SUFFIXES)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                existing = {name.lower(): value for name, value in headers}
                # CSP is meaningless for PDFs and can break the browser's PDF viewer.
                skip_csp = exempt or existing.get(b"content-type", b"").startswith(b"application/pdf")
                for name, value in SECURITY_HEADERS:
                    if name in existing or (skip_csp and name == b"content-security-policy"):
                        continue
                    headers.append((name, value))
                headers.append((b"x-request-id", request_id_var.get().encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            access_logger.info(
                "request",
                extra={
                    "method": scope.get("method"),
                    "path": path,
                    "status": status_holder["status"],
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            request_id_var.reset(rid_token)
            user_var.reset(user_token)
