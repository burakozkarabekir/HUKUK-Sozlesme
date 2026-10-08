"""JSON application logging (rotating file) and the DB-backed audit log.

Rules:
- Prompt contents and contract texts are never logged; only IDs and the user
  question may appear.
- Known secret values and common credential patterns are masked.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from typing import Any

from app.config import Settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
user_var: ContextVar[str] = ContextVar("user", default="-")

MASK = "***"

_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"),
    re.compile(
        r"(?i)((?:api[-_]?key|access[-_]?token|client[-_]?secret|password|secret|authorization)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&]+"
    ),
]

# Standard LogRecord attributes; anything else passed via `extra=` is emitted.
_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class SecretMasker:
    def __init__(self, secrets: list[str] | None = None) -> None:
        # Very short values would mask unrelated text; ignore them.
        self._secrets = sorted({s for s in (secrets or []) if len(s) >= 6}, key=len, reverse=True)

    def mask(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(lambda m: m.group(1) + MASK, text)
        return text


class JsonFormatter(logging.Formatter):
    def __init__(self, masker: SecretMasker) -> None:
        super().__init__()
        self._masker = masker

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "user": user_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        text = json.dumps(payload, ensure_ascii=False, default=str)
        return self._masker.mask(text)


def setup_logging(settings: Settings) -> None:
    """Configure root logging. Safe to call more than once."""
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = JsonFormatter(SecretMasker(settings.secret_values()))

    file_handler = RotatingFileHandler(
        settings.LOG_DIR / "app.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.setLevel(settings.LOG_LEVEL)

    # Our middleware writes access logs; route uvicorn through root and silence
    # its own access log to avoid duplicates.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    # Third-party HTTP clients are noisy at INFO and may print URLs with tokens.
    for name in ("httpx", "httpx2", "httpcore", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)


def write_audit(
    conn: sqlite3.Connection,
    action: str,
    *,
    user_id: str | None = None,
    target_type: str | None = None,
    target_ids: list[str] | None = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """Append an audit record. Never pass contract text or prompts in `details`."""
    conn.execute(
        "INSERT INTO audit_log (ts, user_id, action, target_type, target_ids_json, details_json, ip)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(UTC).isoformat(timespec="seconds"),
            user_id or user_var.get(),
            action,
            target_type,
            json.dumps(target_ids or [], ensure_ascii=False),
            json.dumps(details or {}, ensure_ascii=False, default=str),
            ip,
        ),
    )
