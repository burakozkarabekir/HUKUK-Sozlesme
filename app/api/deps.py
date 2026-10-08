"""Shared FastAPI dependencies."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import Depends

from app.config import Settings, get_settings
from app.db import connect


def get_db(settings: Settings = Depends(get_settings)) -> Iterator[sqlite3.Connection]:
    conn = connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()
