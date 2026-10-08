"""Admin endpoints: manual sync and status."""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.deps import audit, get_db
from app.auth import require_admin
from app.config import Settings, get_settings
from app.ingest.pipeline import SYNC_LOCK, start_background_sync
from app.llm.usage import daily_usage
from app.locks import lock_holder
from app.models import CurrentUser

router = APIRouter(prefix="/api/admin", tags=["admin"])


class SyncRequest(BaseModel):
    full: bool = False


@router.post("/sync", status_code=202)
def trigger_sync(
    request: Request,
    body: SyncRequest | None = None,
    user: CurrentUser = Depends(require_admin),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    full = bool(body and body.full)
    start_background_sync(settings, triggered_by=user.username, full=full)
    audit(conn, request, user, "admin.sync", details={"full": full})
    return JSONResponse({"status": "started", "message": "Senkronizasyon başlatıldı."}, status_code=202)


def _sync_run(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["errors"] = json.loads(data.pop("errors_json") or "[]")
    return data


@router.get("/status")
def status(
    user: CurrentUser = Depends(require_admin),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    counts = {
        "contracts": conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0],
        "files": conn.execute("SELECT COUNT(*) FROM contract_files").fetchone()[0],
        "attachments": conn.execute("SELECT COUNT(*) FROM contract_files WHERE is_attachment = 1").fetchone()[0],
        "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "embeddings": conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
    }
    runs = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 10").fetchall()
    ocr_rows = conn.execute(
        "SELECT f.contract_id, f.file_id, f.file_name, f.page_count, f.ocr_status, c.contract_no, c.title"
        " FROM contract_files f JOIN contracts c ON c.contract_id = f.contract_id"
        " WHERE f.ocr_status IN ('pending', 'failed') ORDER BY c.contract_no LIMIT 500"
    ).fetchall()
    usage = daily_usage(conn, 7)
    jobs = {
        row["status"]: row["n"]
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM analysis_jobs GROUP BY status")
    }
    return {
        "counts": counts,
        "sync_running": lock_holder(conn, SYNC_LOCK) is not None,
        "sync_runs": [_sync_run(r) for r in runs],
        "ocr_pending": [dict(r) for r in ocr_rows],
        "ocr_enabled": settings.OCR_ENABLED,
        "token_usage": usage,
        "token_usage_today": usage[0] if usage and usage[0]["day"] == _today() else None,
        "analysis_jobs": jobs,
        "config": {
            "contract_source": settings.CONTRACT_SOURCE,
            "chat_model": settings.CHAT_MODEL,
            "embedding_model": settings.EMBEDDING_MODEL or None,
            "vector_search": settings.vector_search_enabled,
            "auth_mode": settings.AUTH_MODE,
            "env": settings.APP_ENV,
        },
    }


def _today() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).date().isoformat()
