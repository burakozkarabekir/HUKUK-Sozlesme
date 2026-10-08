"""Health endpoints. `/health` is public; `/health/deps` checks dependencies (admin only)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.api.deps import get_db
from app.auth import require_admin
from app.config import Settings, get_settings
from app.errors import AppError
from app.llm import prompts
from app.models import CurrentUser, HealthResponse

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


async def _timed(check) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    started = time.perf_counter()
    try:
        ok, message = await check()
    except AppError as exc:
        ok, message = False, exc.message
    except Exception as exc:
        logger.warning("dependency_check_failed", exc_info=True)
        ok, message = False, f"{type(exc).__name__}: {str(exc)[:200]}"
    return {"ok": ok, "message": message, "latency_ms": round((time.perf_counter() - started) * 1000)}


@router.get("/health/deps")
async def health_deps(
    request: Request,
    settings: Settings = Depends(get_settings),
    user: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    """LLM, contract source, OCR and DB connectivity (admin; makes real calls)."""

    async def database() -> tuple[bool, str]:
        def run() -> str:
            from app.db import connect, current_version

            conn = connect(settings.db_path)
            try:
                return f"SQLite şema sürümü {current_version(conn)}"
            finally:
                conn.close()

        return True, await asyncio.to_thread(run)

    async def llm() -> tuple[bool, str]:
        client = request.app.state.llm
        client.ensure_configured()
        result = await client.complete(prompts.BASE_RULES, prompts.CHECK_SIMPLE, purpose="health", user=user.username)
        return True, f"{settings.CHAT_MODEL} yanıt verdi ({result.usage.total_tokens} token)"

    async def embeddings() -> tuple[bool | None, str]:
        if not settings.vector_search_enabled:
            return None, "Vektör arama kapalı (EMBEDDING_MODEL boş)"
        vectors = await request.app.state.llm.embed(["test"], purpose="health", user=user.username)
        return True, f"{settings.EMBEDDING_MODEL}: {len(vectors[0])} boyutlu vektör"

    async def source() -> tuple[bool, str]:
        def run() -> str:
            from app.sources import get_source

            src = get_source(settings)
            try:
                if settings.CONTRACT_SOURCE == "turkuaz":
                    _, items = src.raw_first_page(1)  # type: ignore[attr-defined]
                    return f"Turkuaz erişilebilir ({len(items)} örnek kayıt)"
                return f"{settings.CONTRACT_SOURCE}: {len(src.list_contracts())} sözleşme bulundu"
            finally:
                src.close()

        return True, await asyncio.to_thread(run)

    async def ocr() -> tuple[bool | None, str]:
        if not settings.OCR_ENABLED:
            return None, "OCR kapalı"
        configured = bool(settings.DOC_INTELLIGENCE_ENDPOINT and settings.DOC_INTELLIGENCE_KEY.get_secret_value())
        return configured, "Yapılandırılmış" if configured else "DOC_INTELLIGENCE_ENDPOINT / KEY eksik"

    names = {
        "Veritabanı": database,
        "Yapay zeka (sohbet modeli)": llm,
        "Embedding": embeddings,
        "Sözleşme kaynağı": source,
        "OCR": ocr,
    }
    results = await asyncio.gather(*(_timed(check) for check in names.values()))
    return {"checks": dict(zip(names, results, strict=True))}
