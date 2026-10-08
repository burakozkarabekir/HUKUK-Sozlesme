"""Contract metadata queries and the filter model shared by list/search/impact."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from pydantic import BaseModel

from app.ingest.normalize import fold, fold_tokens

# VARSAYIM: Status values (folded) that mean the contract is no longer in force.
CLOSED_STATUSES = {
    "sona erdi",
    "feshedildi",
    "fesih",
    "iptal",
    "pasif",
    "kapali",
    "closed",
    "terminated",
    "expired",
}


def like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ContractFilters(BaseModel):
    q: str | None = None
    contract_type: str | None = None
    party: str | None = None
    status: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    active_only: bool = False
    contract_ids: list[str] | None = None

    def is_empty(self) -> bool:
        return (
            not any(
                [
                    self.q,
                    self.contract_type,
                    self.party,
                    self.status,
                    self.date_from,
                    self.date_to,
                    self.active_only,
                ]
            )
            and not self.contract_ids
        )

    def to_sql(self, alias: str = "c") -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if self.q:
            for token in fold_tokens(self.q):
                clauses.append(f"{alias}.meta_norm LIKE ? ESCAPE '\\'")
                params.append(f"%{like_escape(token)}%")
        if self.contract_type:
            clauses.append(f"tr_fold({alias}.contract_type) = ?")
            params.append(fold(self.contract_type.strip()))
        if self.status:
            clauses.append(f"tr_fold({alias}.status) = ?")
            params.append(fold(self.status.strip()))
        if self.party:
            clauses.append(f"{alias}.parties_norm LIKE ? ESCAPE '\\'")
            params.append(f"%{like_escape(fold(self.party.strip()))}%")
        if self.date_from:
            clauses.append(f"({alias}.end_date IS NULL OR {alias}.end_date >= ?)")
            params.append(self.date_from.isoformat())
        if self.date_to:
            clauses.append(f"({alias}.start_date IS NULL OR {alias}.start_date <= ?)")
            params.append(self.date_to.isoformat())
        if self.active_only:
            clauses.append(active_sql(alias))
            params.append(date.today().isoformat())
        if self.contract_ids is not None:
            if not self.contract_ids:
                clauses.append("0")
            else:
                clauses.append(f"{alias}.contract_id IN ({','.join('?' * len(self.contract_ids))})")
                params.extend(self.contract_ids)
        return (" AND ".join(clauses) or "1"), params


def active_sql(alias: str = "c") -> str:
    closed = ",".join(f"'{s}'" for s in sorted(CLOSED_STATUSES))
    # VARSAYIM: "In force" = not ended (end_date empty or >= today) and status not closed.
    return (
        f"(({alias}.end_date IS NULL OR {alias}.end_date >= ?)"
        f" AND tr_fold(COALESCE({alias}.status, '')) NOT IN ({closed}))"
    )


def is_active(row: sqlite3.Row | dict[str, Any], today: date | None = None) -> bool:
    today = today or date.today()
    end = row["end_date"]
    status = fold(row["status"] or "")
    return (not end or end >= today.isoformat()) and status not in CLOSED_STATUSES


def contract_summary(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    return {
        "contract_id": row["contract_id"],
        "contract_no": row["contract_no"],
        "title": row["title"],
        "parties": json.loads(row["parties_json"] or "[]"),
        "contract_type": row["contract_type"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "status": row["status"],
        "is_active": is_active(row),
        **({"file_count": row["file_count"]} if "file_count" in keys else {}),
        **({"chunk_count": row["chunk_count"]} if "chunk_count" in keys else {}),
        **({"ocr_pending": bool(row["ocr_pending"])} if "ocr_pending" in keys else {}),
    }


def list_contracts(
    conn: sqlite3.Connection, filters: ContractFilters, page: int = 1, page_size: int = 25
) -> tuple[list[dict[str, Any]], int]:
    where, params = filters.to_sql("c")
    total = conn.execute(f"SELECT COUNT(*) FROM contracts c WHERE {where}", params).fetchone()[0]  # noqa: S608
    rows = conn.execute(
        f"""
        SELECT c.*,
            (SELECT COUNT(*) FROM contract_files f WHERE f.contract_id = c.contract_id) AS file_count,
            (SELECT COUNT(*) FROM chunks k WHERE k.contract_id = c.contract_id) AS chunk_count,
            (SELECT COUNT(*) FROM contract_files f WHERE f.contract_id = c.contract_id
                AND f.ocr_status IN ('pending', 'failed')) AS ocr_pending
        FROM contracts c WHERE {where}
        ORDER BY COALESCE(c.contract_no, c.title, c.contract_id) COLLATE NOCASE
        LIMIT ? OFFSET ?
        """,  # noqa: S608
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return [contract_summary(r) for r in rows], int(total)


def get_contract(conn: sqlite3.Connection, contract_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM contracts WHERE contract_id = ?", (contract_id,)).fetchone()
    if row is None:
        return None
    files = conn.execute(
        "SELECT file_id, file_name, is_attachment, page_count, char_count, needs_ocr, ocr_status, extract_method"
        " FROM contract_files WHERE contract_id = ? ORDER BY is_attachment, file_name",
        (contract_id,),
    ).fetchall()
    clauses = conn.execute(
        "SELECT id, file_id, is_attachment, clause_ref, clause_title, page_start, page_end, text"
        " FROM chunks WHERE contract_id = ? ORDER BY is_attachment, file_id, ordinal",
        (contract_id,),
    ).fetchall()
    return {
        **contract_summary(row),
        "extra": json.loads(row["extra_json"] or "{}"),
        "source": row["source"],
        "updated_at": row["updated_at"],
        "files": [
            {**dict(f), "is_attachment": bool(f["is_attachment"]), "needs_ocr": bool(f["needs_ocr"])} for f in files
        ],
        "clauses": [{**dict(c), "is_attachment": bool(c["is_attachment"])} for c in clauses],
    }


def filter_options(conn: sqlite3.Connection) -> dict[str, list[str]]:
    def distinct(column: str) -> list[str]:
        rows = conn.execute(
            f"SELECT DISTINCT {column} AS v FROM contracts WHERE {column} IS NOT NULL AND {column} != ''"  # noqa: S608
            f" ORDER BY {column} COLLATE NOCASE"
        ).fetchall()
        return [r["v"] for r in rows]

    return {"contract_types": distinct("contract_type"), "statuses": distinct("status")}


def get_chunk(conn: sqlite3.Connection, chunk_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT k.id, k.contract_id, k.file_id, k.is_attachment, k.clause_ref, k.clause_title, k.page_start,"
        " k.page_end, k.text, c.contract_no, c.title, c.parties_json, f.file_name"
        " FROM chunks k JOIN contracts c ON c.contract_id = k.contract_id"
        " LEFT JOIN contract_files f ON f.contract_id = k.contract_id AND f.file_id = k.file_id"
        " WHERE k.id = ?",
        (chunk_id,),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["parties"] = json.loads(data.pop("parties_json") or "[]")
    data["is_attachment"] = bool(data["is_attachment"])
    return data
