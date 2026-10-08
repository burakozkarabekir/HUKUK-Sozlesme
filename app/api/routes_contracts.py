"""Contract list / detail / PDF endpoints."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse

from app.api.deps import audit, get_db
from app.auth import get_current_user
from app.config import Settings, get_settings
from app.errors import AppError
from app.models import CurrentUser
from app.services.contracts import ContractFilters, filter_options, get_chunk, get_contract, list_contracts

router = APIRouter(prefix="/api", tags=["contracts"])


def contract_filters(
    q: str | None = Query(None, max_length=200),
    contract_type: str | None = Query(None, max_length=200),
    party: str | None = Query(None, max_length=200),
    status: str | None = Query(None, max_length=100),
    date_from: date | None = None,
    date_to: date | None = None,
    active_only: bool = False,
) -> ContractFilters:
    return ContractFilters(
        q=q or None,
        contract_type=contract_type or None,
        party=party or None,
        status=status or None,
        date_from=date_from,
        date_to=date_to,
        active_only=active_only,
    )


@router.get("/contracts")
def contracts(
    filters: ContractFilters = Depends(contract_filters),
    page: int = Query(1, ge=1, le=100_000),
    page_size: int = Query(25, ge=1, le=500),
    user: CurrentUser = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    items, total = list_contracts(conn, filters, page, page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/contracts/filters")
def contract_filter_options(
    user: CurrentUser = Depends(get_current_user), conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    return filter_options(conn)


@router.get("/contracts/{contract_id}")
def contract_detail(
    contract_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    contract = get_contract(conn, contract_id)
    if contract is None:
        raise AppError("contract_not_found", "Sözleşme bulunamadı.", 404)
    audit(conn, request, user, "contract.view", target_type="contract", target_ids=[contract_id])
    return contract


@router.get("/contracts/{contract_id}/files/{file_id}")
def contract_file(
    contract_id: str,
    file_id: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_db),
) -> FileResponse:
    row = conn.execute(
        "SELECT file_name, storage_path, mime_type FROM contract_files WHERE contract_id = ? AND file_id = ?",
        (contract_id, file_id),
    ).fetchone()
    if row is None or not row["storage_path"]:
        raise AppError("file_not_found", "Dosya bulunamadı.", 404)
    data_dir = settings.DATA_DIR.resolve()
    path = (data_dir / row["storage_path"]).resolve()
    if not path.is_relative_to(data_dir) or not path.is_file():
        raise AppError("file_not_found", "Dosya bulunamadı.", 404)
    audit(
        conn,
        request,
        user,
        "contract.file_view",
        target_type="contract",
        target_ids=[contract_id],
        details={"file_id": file_id},
    )
    return FileResponse(
        Path(path),
        media_type="application/pdf",
        filename=row["file_name"],
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/chunks/{chunk_id}")
def chunk_detail(
    chunk_id: Annotated[int, PathParam(ge=1, le=2**63 - 1)],
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    chunk = get_chunk(conn, chunk_id)
    if chunk is None:
        raise AppError("chunk_not_found", "Madde bulunamadı (sözleşme sonradan güncellenmiş olabilir).", 404)
    audit(
        conn,
        request,
        user,
        "contract.clause_view",
        target_type="contract",
        target_ids=[chunk["contract_id"]],
        details={"chunk_id": chunk_id},
    )
    return chunk
