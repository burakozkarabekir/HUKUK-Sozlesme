"""Cross-process named locks stored in SQLite (`app_locks`).

Used so that only one sync runs at a time across uvicorn workers, the nightly
timer and the CLI. A lock expires after `ttl_seconds` so a crashed holder
cannot block forever.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

from app.db import transaction


def new_owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def acquire_lock(conn: sqlite3.Connection, name: str, owner: str, ttl_seconds: int) -> bool:
    now = datetime.now(UTC)
    expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
    with transaction(conn):
        row = conn.execute("SELECT owner, expires_at FROM app_locks WHERE name = ?", (name,)).fetchone()
        if row and row["owner"] != owner and row["expires_at"] > now.isoformat():
            return False
        conn.execute(
            "INSERT INTO app_locks (name, owner, acquired_at, expires_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET owner = excluded.owner,"
            " acquired_at = excluded.acquired_at, expires_at = excluded.expires_at",
            (name, owner, now.isoformat(), expires),
        )
    return True


def refresh_lock(conn: sqlite3.Connection, name: str, owner: str, ttl_seconds: int) -> None:
    expires = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
    conn.execute("UPDATE app_locks SET expires_at = ? WHERE name = ? AND owner = ?", (expires, name, owner))


def release_lock(conn: sqlite3.Connection, name: str, owner: str) -> None:
    conn.execute("DELETE FROM app_locks WHERE name = ? AND owner = ?", (name, owner))


def lock_holder(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT owner FROM app_locks WHERE name = ? AND expires_at > ?", (name, datetime.now(UTC).isoformat())
    ).fetchone()
    return row["owner"] if row else None
