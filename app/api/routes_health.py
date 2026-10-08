"""Health endpoints. `/health` is public; `/health/deps` (admin) arrives in phase 7."""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app import __version__
from app.api.deps import get_db
from app.config import Settings, get_settings
from app.models import HealthResponse

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/health", response_model=HealthResponse)
def health(
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    db_status = "ok"
    try:
        conn.execute("SELECT 1 FROM schema_migrations LIMIT 1").fetchall()
    except sqlite3.Error:
        logger.exception("health_db_check_failed")
        db_status = "error"
    body = HealthResponse(
        status="ok" if db_status == "ok" else "degraded",
        app=settings.APP_NAME,
        env=settings.APP_ENV,
        version=__version__,
        db=db_status,
    )
    return JSONResponse(body.model_dump(), status_code=200 if db_status == "ok" else 503)
