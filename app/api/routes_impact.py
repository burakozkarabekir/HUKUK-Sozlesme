"""Legislation impact analysis endpoints."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.api.deps import audit, get_db
from app.api.routes_chat import get_llm
from app.auth import get_current_user
from app.config import Settings, get_settings
from app.errors import AppError
from app.ingest.pdf_extract import extract_pdf, looks_like_pdf
from app.llm.client import LLMClient
from app.models import CurrentUser
from app.services.export import build_workbook
from app.services.impact import (
    RunRequest,
    check_legislation_text,
    create_job,
    extract_concepts,
    job_detail,
    job_history,
    request_cancel,
)

router = APIRouter(prefix="/api/impact", tags=["impact"])


class ExtractRequest(BaseModel):
    text: str = Field(min_length=1)


async def read_limited_body(request: Request, max_bytes: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise AppError("file_too_large", f"Dosya en fazla {max_bytes // (1024 * 1024)} MB olabilir.", 413)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise AppError("file_too_large", f"Dosya en fazla {max_bytes // (1024 * 1024)} MB olabilir.", 413)
        chunks.append(chunk)
    return b"".join(chunks)


async def legislation_from_pdf(request: Request, settings: Settings) -> str:
    data = await read_limited_body(request, settings.MAX_UPLOAD_MB * 1024 * 1024)
    if not looks_like_pdf(data):
        raise AppError("not_a_pdf", "Yüklenen dosya geçerli bir PDF değil.", 415)
    extracted = await asyncio.to_thread(extract_pdf, data, settings.OCR_MIN_CHARS_PER_PAGE)
    text = extracted.full_text
    if extracted.needs_ocr:
        if not settings.OCR_ENABLED:
            raise AppError(
                "pdf_needs_ocr",
                "PDF taranmış görünüyor; metin çıkarılamadı. Metni kopyalayıp yapıştırın veya OCR'ı etkinleştirin.",
                422,
            )
        from app.ingest.ocr import DocumentIntelligenceOcr

        pages = await asyncio.to_thread(DocumentIntelligenceOcr(settings).ocr_pdf, data)
        text = "\n\n".join(p.text for p in pages)
    return text


@router.post("/extract")
async def extract(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    llm: LLMClient = Depends(get_llm),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Body: JSON {"text": ...} or a raw PDF (Content-Type: application/pdf)."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "application/pdf":
        text, kind = await legislation_from_pdf(request, settings), "pdf"
    elif content_type == "application/json":
        try:
            body = ExtractRequest.model_validate_json(
                await read_limited_body(request, settings.MAX_LEGISLATION_CHARS * 4 + 1024)
            )
        except ValueError as exc:
            raise AppError("validation_error", "İstek verisi geçersiz.", 422) from exc
        text, kind = body.text, "text"
    else:
        raise AppError("unsupported_media_type", "Sadece metin (JSON) veya PDF kabul edilir.", 415)
    text = check_legislation_text(text, settings)
    llm.ensure_configured()
    concepts = await extract_concepts(llm, settings, text, user.username)
    audit(conn, request, user, "impact.extract", details={"input_kind": kind, "chars": len(text)})
    return {"text": text, "input_kind": kind, "extraction": concepts.model_dump()}


@router.post("/run", status_code=202)
async def run(
    body: RunRequest,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    llm: LLMClient = Depends(get_llm),
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    body.text = check_legislation_text(body.text, settings)
    llm.ensure_configured()
    if not [k for k in body.keywords if k.strip()]:
        raise AppError("no_keywords", "En az bir anahtar kelime gerekli.", 400)
    job_id = create_job(conn, body, user.username)
    audit(
        conn,
        request,
        user,
        "impact.run",
        target_type="analysis",
        target_ids=[job_id],
        details={"keywords": len(body.keywords), "filters": body.filters.model_dump(mode="json")},
    )
    runner = getattr(request.app.state, "job_runner", None)
    if runner is not None:
        runner.wake()
    return JSONResponse({"job_id": job_id, "status": "queued"}, status_code=202)


@router.get("/history")
def history(
    limit: int = Query(50, ge=1, le=200),
    user: CurrentUser = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    # VARSAYIM: The legal team shares analyses; everyone sees all analyses.
    return {"items": job_history(conn, limit)}


def _job_or_404(conn: sqlite3.Connection, job_id: str) -> dict:
    job = job_detail(conn, job_id)
    if job is None:
        raise AppError("job_not_found", "Analiz bulunamadı.", 404)
    return job


@router.get("/{job_id}")
def get_job(
    job_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    job = _job_or_404(conn, job_id)
    if job["status"] in ("completed", "cancelled", "failed"):
        audit(conn, request, user, "impact.view", target_type="analysis", target_ids=[job_id])
    return job


@router.post("/{job_id}/cancel")
def cancel(
    job_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    job = request_cancel(conn, job_id)
    audit(conn, request, user, "impact.cancel", target_type="analysis", target_ids=[job_id])
    return job


@router.get("/{job_id}/export.xlsx")
def export(
    job_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    job = _job_or_404(conn, job_id)
    content = build_workbook(job)
    audit(conn, request, user, "impact.export", target_type="analysis", target_ids=[job_id])
    filename = f"etki-analizi-{datetime.now(UTC).strftime('%Y%m%d')}-{job_id[:8]}.xlsx"
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "no-store",
        },
    )
