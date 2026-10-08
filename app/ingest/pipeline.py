"""Sync pipeline: fetch -> extract -> (OCR) -> chunk -> index. Incremental.

- `updated_since` = start time of the last fully successful sync.
- Each file's SHA-256 is stored; unchanged files are not re-processed.
- Each contract is processed independently; one failing contract does not stop
  the run (status "partial"). Results go to `sync_runs`.
- A DB lock ("sync") guarantees a single sync at a time across processes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings
from app.db import connect, migrate, transaction, utcnow_iso
from app.errors import AppError
from app.ingest.chunker import Chunk, chunk_pages
from app.ingest.normalize import fold
from app.ingest.pdf_extract import PageText, extract_pdf
from app.locks import acquire_lock, lock_holder, new_owner_id, refresh_lock, release_lock
from app.sources import get_source
from app.sources.base import ContractFile, ContractMeta, ContractSource

logger = logging.getLogger(__name__)

SYNC_LOCK = "sync"
SYNC_LOCK_TTL = 2 * 60 * 60
# Embedder: list of texts -> list of vectors (phase 4); None disables embeddings.
Embedder = Callable[[list[str]], list[list[float]]]


class SyncAlreadyRunning(AppError):
    def __init__(self) -> None:
        super().__init__("sync_running", "Şu anda başka bir senkronizasyon çalışıyor. Lütfen bitmesini bekleyin.", 409)


@dataclass
class SyncStats:
    run_id: int = 0
    status: str = "running"
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)


@dataclass
class FileOutcome:
    chunks: list[Chunk]
    page_count: int
    char_count: int
    needs_ocr: bool
    ocr_status: str | None
    method: str
    low_text_pages: list[int]


def meta_norm(meta: ContractMeta) -> str:
    parts = [
        meta.contract_no or "",
        meta.title or "",
        *meta.parties,
        meta.contract_type or "",
        meta.status or "",
    ]
    return fold(" | ".join(p for p in parts if p))


def store_file(settings: Settings, data: bytes, sha: str) -> str:
    relative = Path("files") / sha[:2] / f"{sha}.pdf"
    target = settings.DATA_DIR / relative
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
    return relative.as_posix()


def attachment_label(file: ContractFile) -> str | None:
    return f"Ek: {file.file_name}" if file.is_attachment else None


def label_attachment_chunks(chunks: list[Chunk], label: str | None) -> list[Chunk]:
    """Make clause refs of attachment files self-explanatory ("Ek: X.pdf / Madde 2")."""
    if not label:
        return chunks
    for chunk in chunks:
        if not chunk.clause_ref:
            chunk.clause_ref = label
        elif not chunk.clause_ref.startswith(("Ek-", "Ek:")):
            chunk.clause_ref = f"{label} / {chunk.clause_ref}"
    return chunks


def process_file(settings: Settings, data: bytes, label: str | None, ocr: object | None) -> FileOutcome:
    extracted = extract_pdf(data, settings.OCR_MIN_CHARS_PER_PAGE)
    pages: list[PageText] = extracted.pages
    ocr_status: str | None = None
    if extracted.needs_ocr:
        if settings.OCR_ENABLED and ocr is not None:
            try:
                pages = ocr.ocr_pdf(data)  # type: ignore[attr-defined]
                ocr_status = "done"
            except AppError as exc:
                logger.warning("ocr_failed", extra={"error": exc.message})
                ocr_status = "failed"
        else:
            ocr_status = "pending"
    chunks = label_attachment_chunks(chunk_pages(pages, label), label)
    return FileOutcome(
        chunks=chunks,
        page_count=extracted.page_count,
        char_count=sum(len(p.text) for p in pages),
        needs_ocr=extracted.needs_ocr,
        ocr_status=ocr_status,
        method=extracted.method,
        low_text_pages=extracted.low_text_pages,
    )


def upsert_contract(conn: sqlite3.Connection, meta: ContractMeta, source_name: str) -> None:
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO contracts (contract_id, contract_no, title, parties_json, parties_norm, contract_type,
            start_date, end_date, status, source_updated_at, extra_json, meta_norm, source, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(contract_id) DO UPDATE SET
            contract_no = excluded.contract_no, title = excluded.title, parties_json = excluded.parties_json,
            parties_norm = excluded.parties_norm, contract_type = excluded.contract_type,
            start_date = excluded.start_date, end_date = excluded.end_date, status = excluded.status,
            source_updated_at = excluded.source_updated_at, extra_json = excluded.extra_json,
            meta_norm = excluded.meta_norm, source = excluded.source, updated_at = excluded.updated_at
        """,
        (
            meta.contract_id,
            meta.contract_no,
            meta.title,
            json.dumps(meta.parties, ensure_ascii=False),
            fold(" | ".join(meta.parties)),
            meta.contract_type,
            meta.start_date.isoformat() if meta.start_date else None,
            meta.end_date.isoformat() if meta.end_date else None,
            meta.status,
            meta.updated_at.isoformat() if meta.updated_at else None,
            json.dumps(meta.extra, ensure_ascii=False, default=str),
            meta_norm(meta),
            source_name,
            now,
            now,
        ),
    )


def replace_file_chunks(
    conn: sqlite3.Connection,
    contract_id: str,
    file: ContractFile,
    sha: str,
    storage_path: str,
    outcome: FileOutcome,
) -> list[int]:
    """Replace one file's row and chunks atomically. Returns new chunk ids."""
    with transaction(conn):
        conn.execute("DELETE FROM chunks WHERE contract_id = ? AND file_id = ?", (contract_id, file.file_id))
        conn.execute(
            """
            INSERT INTO contract_files (contract_id, file_id, file_name, is_attachment, mime_type, sha256,
                storage_path, page_count, char_count, needs_ocr, ocr_status, indexed_at, extract_method,
                low_text_pages_json, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(contract_id, file_id) DO UPDATE SET
                file_name = excluded.file_name, is_attachment = excluded.is_attachment,
                mime_type = excluded.mime_type, sha256 = excluded.sha256, storage_path = excluded.storage_path,
                page_count = excluded.page_count, char_count = excluded.char_count,
                needs_ocr = excluded.needs_ocr, ocr_status = excluded.ocr_status,
                indexed_at = excluded.indexed_at, extract_method = excluded.extract_method,
                low_text_pages_json = excluded.low_text_pages_json, error = NULL,
                updated_at = excluded.updated_at
            """,
            (
                contract_id,
                file.file_id,
                file.file_name,
                int(file.is_attachment),
                file.mime_type,
                sha,
                storage_path,
                outcome.page_count,
                outcome.char_count,
                int(outcome.needs_ocr),
                outcome.ocr_status,
                utcnow_iso(),
                outcome.method,
                json.dumps(outcome.low_text_pages),
                utcnow_iso(),
            ),
        )
        ids = []
        row = conn.execute("SELECT value FROM app_counters WHERE name = 'chunk_id'").fetchone()
        current_max = conn.execute("SELECT COALESCE(MAX(id), 0) FROM chunks").fetchone()[0]
        next_id = max(row[0] if row else 0, current_max) + 1
        for chunk in outcome.chunks:
            cursor = conn.execute(
                "INSERT INTO chunks (id, contract_id, file_id, is_attachment, ordinal, clause_ref, clause_title,"
                " page_start, page_end, text, text_norm) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    next_id,
                    contract_id,
                    file.file_id,
                    int(file.is_attachment),
                    chunk.ordinal,
                    chunk.clause_ref,
                    chunk.clause_title,
                    chunk.page_start,
                    chunk.page_end,
                    chunk.text,
                    fold(chunk.text),
                ),
            )
            ids.append(int(cursor.lastrowid))
            next_id += 1
        conn.execute(
            "INSERT INTO app_counters (name, value) VALUES ('chunk_id', ?)"
            " ON CONFLICT(name) DO UPDATE SET value = MAX(value, excluded.value)",
            (next_id - 1,),
        )
    return ids


def store_embeddings(conn: sqlite3.Connection, settings: Settings, embedder: Embedder | None, ids: list[int]) -> None:
    if embedder is None or not ids:
        return
    from app.services.embeddings import store_vectors  # local import: numpy only when enabled

    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT id, text FROM chunks WHERE id IN ({placeholders})", ids).fetchall()  # noqa: S608
    store_vectors(conn, settings.EMBEDDING_MODEL, [r["id"] for r in rows], embedder([r["text"] for r in rows]))


def sync_contract(
    conn: sqlite3.Connection,
    settings: Settings,
    source: ContractSource,
    meta: ContractMeta,
    ocr: object | None = None,
    embedder: Embedder | None = None,
) -> str:
    """Sync one contract. Returns "new", "updated" or "unchanged"."""
    existing = conn.execute(
        "SELECT meta_norm, start_date, end_date FROM contracts WHERE contract_id = ?", (meta.contract_id,)
    ).fetchone()
    files = source.get_contract_document(meta.contract_id)
    known = {
        row["file_id"]: row["sha256"]
        for row in conn.execute("SELECT file_id, sha256 FROM contract_files WHERE contract_id = ?", (meta.contract_id,))
    }
    upsert_contract(conn, meta, source.name)
    changed = existing is None or (
        existing["meta_norm"],
        existing["start_date"],
        existing["end_date"],
    ) != (
        meta_norm(meta),
        meta.start_date and meta.start_date.isoformat(),
        meta.end_date and meta.end_date.isoformat(),
    )

    seen: set[str] = set()
    for file in files:
        seen.add(file.file_id)
        data = file.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if known.get(file.file_id) == sha:
            conn.execute(
                "UPDATE contract_files SET file_name = ?, is_attachment = ? WHERE contract_id = ? AND file_id = ?",
                (file.file_name, int(file.is_attachment), meta.contract_id, file.file_id),
            )
            continue
        changed = True
        outcome = process_file(settings, data, attachment_label(file), ocr)
        storage_path = store_file(settings, data, sha)
        ids = replace_file_chunks(conn, meta.contract_id, file, sha, storage_path, outcome)
        store_embeddings(conn, settings, embedder, ids)

    for file_id in set(known) - seen:
        conn.execute("DELETE FROM contract_files WHERE contract_id = ? AND file_id = ?", (meta.contract_id, file_id))
        changed = True
    if existing is None:
        return "new"
    return "updated" if changed else "unchanged"


# VARSAYIM: The incremental watermark only advances after a fully successful run, so
# contracts that failed are listed again next time. Contracts are deleted only by a
# full sync (`--full`): an incremental listing cannot tell "deleted" from "unchanged".
def last_success_start(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT started_at FROM sync_runs WHERE status = 'success' ORDER BY id DESC LIMIT 1").fetchone()
    return row["started_at"] if row else None


def _user_message(exc: Exception) -> str:
    return exc.message if isinstance(exc, AppError) else f"Beklenmeyen hata ({type(exc).__name__})"


# VARSAYIM: A full sync never deletes more than half of the archive in one run; a
# larger drop is far more likely an API/mapping problem than real deletions.
FULL_SYNC_MAX_DELETE_RATIO = 0.5


def _delete_missing(conn: sqlite3.Connection, listed: set[str], stats: SyncStats) -> int:
    existing = [row["contract_id"] for row in conn.execute("SELECT contract_id FROM contracts")]
    stale = [contract_id for contract_id in existing if contract_id not in listed]
    if not stale:
        return 0
    if stats.errors:
        stats.errors.append({"contract_id": "", "message": "Liste eksik/hatalı alındığı için silme işlemi yapılmadı."})
        return 0
    if len(stale) > len(existing) * FULL_SYNC_MAX_DELETE_RATIO:
        stats.errors.append(
            {
                "contract_id": "",
                "message": f"Kaynakta {len(stale)}/{len(existing)} sözleşme görünmüyor; güvenlik nedeniyle "
                "silme yapılmadı. Kaynak yanıtını ve eşleştirmeyi kontrol edin (cli.py check-turkuaz).",
            }
        )
        return 0
    for contract_id in stale:
        conn.execute("DELETE FROM contracts WHERE contract_id = ?", (contract_id,))
    return len(stale)


def _embed_missing_safely(conn: sqlite3.Connection, settings: Settings, embedder: Embedder) -> list[dict[str, str]]:
    """Fill vectors that failed earlier (the file hash is already stored, so the file
    itself will not be processed again)."""
    from app.services.embeddings import embed_missing

    try:
        embed_missing(conn, settings, embedder)
    except Exception as exc:
        logger.warning("sync_embed_missing_failed", exc_info=True)
        return [{"contract_id": "", "message": f"Embedding üretilemedi: {_user_message(exc)}"}]
    return []


def build_ocr(settings: Settings) -> object | None:
    if not settings.OCR_ENABLED:
        return None
    from app.ingest.ocr import DocumentIntelligenceOcr

    return DocumentIntelligenceOcr(settings)


def run_sync(
    settings: Settings,
    *,
    trigger: str = "cli",
    triggered_by: str | None = None,
    full: bool = False,
    source: ContractSource | None = None,
    ocr: object | None = None,
    embedder: Embedder | None = None,
) -> SyncStats:
    from datetime import datetime

    conn = connect(settings.db_path)
    migrate(conn)
    owner = new_owner_id()
    if not acquire_lock(conn, SYNC_LOCK, owner, SYNC_LOCK_TTL):
        conn.close()
        raise SyncAlreadyRunning
    stats = SyncStats()
    started = utcnow_iso()
    cursor = conn.execute(
        "INSERT INTO sync_runs (started_at, status, trigger, triggered_by) VALUES (?, 'running', ?, ?)",
        (started, trigger, triggered_by),
    )
    stats.run_id = int(cursor.lastrowid)
    logger.info("sync_started", extra={"run_id": stats.run_id, "trigger": trigger, "full": full})
    own_source = source is None
    try:
        source = source or get_source(settings)
        ocr = ocr or build_ocr(settings)
        if embedder is None and settings.vector_search_enabled:
            from app.services.embeddings import make_sync_embedder

            embedder = make_sync_embedder(settings)
        since_text = None if full else last_success_start(conn)
        since = datetime.fromisoformat(since_text) if since_text else None
        metas = source.list_contracts(since)
        listed: set[str] = set()
        for meta in metas:
            listed.add(meta.contract_id)
            refresh_lock(conn, SYNC_LOCK, owner, SYNC_LOCK_TTL)
            try:
                outcome = sync_contract(conn, settings, source, meta, ocr, embedder)
            except Exception as exc:
                logger.warning("sync_contract_failed", extra={"contract_id": meta.contract_id}, exc_info=True)
                stats.errors.append({"contract_id": meta.contract_id, "message": _user_message(exc)})
                continue
            setattr(stats, outcome, getattr(stats, outcome) + 1)
        # A listing the source itself flags as incomplete (page limit, unmapped records)
        # must not advance the watermark nor drive deletions.
        for warning in getattr(source, "listing_warnings", []) or []:
            stats.errors.append({"contract_id": "", "message": warning})
        if embedder is not None:
            stats.errors.extend(_embed_missing_safely(conn, settings, embedder))
        if full:
            stats.deleted = _delete_missing(conn, listed, stats)
        stats.status = "partial" if stats.errors else "success"
    except Exception as exc:
        logger.exception("sync_failed")
        stats.status = "failed"
        stats.errors.append({"contract_id": "", "message": _user_message(exc)})
    finally:
        conn.execute(
            "UPDATE sync_runs SET finished_at = ?, status = ?, new_count = ?, updated_count = ?,"
            " unchanged_count = ?, error_count = ?, errors_json = ? WHERE id = ?",
            (
                utcnow_iso(),
                stats.status,
                stats.new,
                stats.updated,
                stats.unchanged,
                len(stats.errors),
                json.dumps(stats.errors[:200], ensure_ascii=False),
                stats.run_id,
            ),
        )
        release_lock(conn, SYNC_LOCK, owner)
        if own_source and source is not None:
            source.close()
        conn.close()
    logger.info(
        "sync_finished",
        extra={
            "run_id": stats.run_id,
            "status": stats.status,
            "new": stats.new,
            "updated": stats.updated,
            "unchanged": stats.unchanged,
            "deleted": stats.deleted,
            "errors": len(stats.errors),
        },
    )
    return stats


def start_background_sync(settings: Settings, triggered_by: str, full: bool = False) -> None:
    """Start a sync thread in this process (admin button). Raises if one is running."""
    conn = connect(settings.db_path)
    try:
        if lock_holder(conn, SYNC_LOCK):
            raise SyncAlreadyRunning
    finally:
        conn.close()

    def _run() -> None:
        try:
            run_sync(settings, trigger="manual", triggered_by=triggered_by, full=full)
        except SyncAlreadyRunning:
            logger.info("background_sync_skipped_already_running")
        except Exception:
            logger.exception("background_sync_crashed")

    threading.Thread(target=_run, name="sozlesme-sync", daemon=True).start()


def reindex(settings: Settings, embedder: Embedder | None = None) -> dict[str, int]:
    """Re-extract and re-chunk all stored PDFs (after chunker changes) and rebuild FTS."""
    conn = connect(settings.db_path)
    migrate(conn)
    owner = new_owner_id()
    if not acquire_lock(conn, SYNC_LOCK, owner, SYNC_LOCK_TTL):
        conn.close()
        raise SyncAlreadyRunning
    counts = {"files": 0, "chunks": 0, "errors": 0}
    try:
        ocr = build_ocr(settings)
        if embedder is None and settings.vector_search_enabled:
            from app.services.embeddings import make_sync_embedder

            embedder = make_sync_embedder(settings)
        rows = conn.execute(
            "SELECT contract_id, file_id, file_name, is_attachment, mime_type, sha256, storage_path"
            " FROM contract_files WHERE storage_path IS NOT NULL"
        ).fetchall()
        for row in rows:
            refresh_lock(conn, SYNC_LOCK, owner, SYNC_LOCK_TTL)
            path = settings.DATA_DIR / row["storage_path"]
            try:
                data = path.read_bytes()
                file = ContractFile(
                    file_id=row["file_id"],
                    file_name=row["file_name"],
                    is_attachment=bool(row["is_attachment"]),
                    mime_type=row["mime_type"],
                )
                outcome = process_file(settings, data, attachment_label(file), ocr)
                ids = replace_file_chunks(conn, row["contract_id"], file, row["sha256"], row["storage_path"], outcome)
                store_embeddings(conn, settings, embedder, ids)
                counts["files"] += 1
                counts["chunks"] += len(ids)
            except Exception:
                logger.warning("reindex_file_failed", extra={"contract_id": row["contract_id"]}, exc_info=True)
                counts["errors"] += 1
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
    finally:
        release_lock(conn, SYNC_LOCK, owner)
        conn.close()
    return counts
