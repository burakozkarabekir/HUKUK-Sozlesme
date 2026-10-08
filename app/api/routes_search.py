"""Keyword search over all contract clauses."""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import audit, get_db
from app.api.routes_contracts import contract_filters
from app.auth import get_current_user
from app.errors import AppError
from app.models import CurrentUser
from app.services.contracts import ContractFilters
from app.services.search import keyword_search, parse_query

router = APIRouter(prefix="/api", tags=["search"])


@router.get("/search")
def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=300),
    mode: Literal["and", "or"] = "and",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    filters: ContractFilters = Depends(contract_filters),
    user: CurrentUser = Depends(get_current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    if not parse_query(q):
        raise AppError("invalid_query", "Arama ifadesi en az bir harf veya rakam içermelidir.", 400)
    filters = filters.model_copy(update={"q": None})  # `q` here is the clause search, not metadata
    result = keyword_search(conn, q, mode=mode, filters=filters, limit=page_size, offset=(page - 1) * page_size)
    audit(conn, request, user, "search", details={"q": q, "mode": mode, "total": result["total"]})
    return {**result, "page": page, "page_size": page_size}
