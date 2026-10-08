"""Token usage bookkeeping (`llm_usage` table)."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.db import connect
from app.llm.client import Usage, UsageRecorder


def insert_usage(
    conn: sqlite3.Connection,
    model: str,
    purpose: str,
    user: str | None,
    usage: Usage,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    conn.execute(
        "INSERT INTO llm_usage"
        " (created_at, day, model, purpose, user_id, input_tokens, output_tokens, total_tokens)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now.isoformat(timespec="seconds"),
            now.date().isoformat(),
            model,
            purpose,
            user,
            usage.input_tokens,
            usage.output_tokens,
            usage.total_tokens,
        ),
    )


def make_db_usage_recorder(db_path: Path) -> UsageRecorder:
    def _write(model: str, purpose: str, user: str | None, usage: Usage) -> None:
        conn = connect(db_path)
        try:
            insert_usage(conn, model, purpose, user, usage)
        finally:
            conn.close()

    async def record(model: str, purpose: str, user: str | None, usage: Usage) -> None:
        await asyncio.to_thread(_write, model, purpose, user, usage)

    return record


def daily_usage(conn: sqlite3.Connection, days: int = 7) -> list[dict[str, int | str]]:
    """Per-day totals for the last `days` days (UTC), newest first."""
    since = (datetime.now(UTC).date() - timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        "SELECT day, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,"
        " SUM(output_tokens) AS output_tokens, SUM(total_tokens) AS total_tokens"
        " FROM llm_usage WHERE day >= ? GROUP BY day ORDER BY day DESC",
        (since,),
    ).fetchall()
    return [dict(row) for row in rows]
