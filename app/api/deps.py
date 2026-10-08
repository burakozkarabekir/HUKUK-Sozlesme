"""Shared FastAPI dependencies and helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

from fastapi import Depends, Request

from app.config import Settings, get_settings
from app.db import connect
from app.logging_conf import write_audit
from app.models import CurrentUser


def get_db(settings: Settings = Depends(get_settings)) -> Iterator[sqlite3.Connection]:
    conn = connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def audit(
    conn: sqlite3.Connection,
    request: Request,
    user: CurrentUser,
    action: str,
    *,
    target_type: str | None = None,
    target_ids: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Audit record for a request. Only IDs and user input - never contract text."""
    write_audit(
        conn,
        action,
        user_id=user.username,
        target_type=target_type,
        target_ids=target_ids,
        details=details,
        ip=client_ip(request),
    )
