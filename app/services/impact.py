"""Legislation impact analysis.

Flow: extract concepts (LLM) -> user edits keywords -> background job:
find candidate clauses (FTS5 [+ vectors]) grouped per contract -> evaluate
each contract (LLM, structured output) -> verify quotes in code -> store.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.config import Settings
from app.db import connect, utcnow_iso
from app.errors import AppError
from app.ingest.normalize import fold, fold_tokens
from app.llm import prompts
from app.llm.client import LLMClient
from app.llm.schemas import CONTRACT_IMPACT, LEGISLATION_CONCEPTS, ContractImpact, LegislationConcepts
from app.services.contracts import ContractFilters, contract_summary
from app.services.search import Term, fetch_chunks, fts_expression

logger = logging.getLogger(__name__)

LEVEL_ORDER = {"ETKILENIR": 0, "MUHTEMELEN_ETKILENIR": 1, "ETKILENMEZ": 2, "DEGERLENDIRILEMEDI": 3}
LEVEL_LABELS = {
    "ETKILENIR": "Etkilenir",
    "MUHTEMELEN_ETKILENIR": "Muhtemelen Etkilenir",
    "ETKILENMEZ": "Etkilenmez",
    "DEGERLENDIRILEMEDI": "Değerlendirilemedi",
}
# Errors after which further LLM calls are pointless (configuration problems).
FATAL_LLM_CODES = {"llm_not_configured", "llm_auth", "llm_forbidden", "llm_not_found"}
MAX_KEYWORDS = 60


class ImpactFilters(BaseModel):
    contract_type: str | None = None
    party: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    active_only: bool = False

    @field_validator("date_from", "date_to", mode="before")
    @classmethod
    def _empty_date(cls, value: Any) -> Any:
        return None if value == "" else value

    def to_contract_filters(self) -> ContractFilters:
        return ContractFilters(
            contract_type=self.contract_type or None,
            party=self.party or None,
            date_from=self.date_from or None,
            date_to=self.date_to or None,
            active_only=self.active_only,
        )


class RunRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    text: str = Field(min_length=1)
    keywords: list[str] = Field(min_length=1, max_length=MAX_KEYWORDS)
    extraction: dict[str, Any] | None = None
    filters: ImpactFilters = Field(default_factory=ImpactFilters)
    input_kind: str = "text"


# ---------------------------------------------------------------------------
# Concept extraction
# ---------------------------------------------------------------------------


def check_legislation_text(text: str, settings: Settings) -> str:
    text = text.strip()
    if not text:
        raise AppError("empty_legislation", "Lütfen mevzuat değişikliği metnini girin veya PDF yükleyin.", 400)
    if len(text) > settings.MAX_LEGISLATION_CHARS:
        raise AppError(
            "legislation_too_long",
            f"Mevzuat metni en fazla {settings.MAX_LEGISLATION_CHARS:,} karakter olabilir.".replace(",", "."),
            413,
        )
    return text


def legislation_block(text: str, settings: Settings) -> str:
    limit = settings.IMPACT_LEGISLATION_PROMPT_CHARS
    clipped = text if len(text) <= limit else text[:limit] + "\n[... metnin devamı kısaltıldı ...]"
    return prompts.wrap_document(clipped, tag="mevzuat_metni")


def clean_keywords(keywords: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        keyword = re.sub(r"\s+", " ", str(keyword)).strip().strip('"').strip()
        key = fold(keyword)
        if keyword and len(keyword) <= 80 and fold_tokens(keyword) and key not in seen:
            seen.add(key)
            result.append(keyword)
    return result[:MAX_KEYWORDS]


async def extract_concepts(llm: LLMClient, settings: Settings, text: str, user: str) -> LegislationConcepts:
    parsed, _ = await llm.generate_json(
        prompts.IMPACT_EXTRACT,
        legislation_block(text, settings),
        LEGISLATION_CONCEPTS,
        purpose="impact_extract",
        user=user,
    )
    assert isinstance(parsed, LegislationConcepts)  # noqa: S101 - narrowing for type checkers
    parsed.keywords = clean_keywords(parsed.keywords)
    return parsed


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


def keyword_terms(keywords: list[str]) -> list[Term]:
    terms: list[Term] = []
    seen: set[tuple[str, ...]] = set()
    for keyword in keywords:
        tokens = tuple(fold_tokens(keyword))
        if not tokens or tokens in seen:
            continue
        seen.add(tokens)
        terms.append(Term(tokens, phrase=len(tokens) > 1))
    return terms


def find_candidates(
    conn: sqlite3.Connection,
    keywords: list[str],
    filters: ContractFilters,
    *,
    per_contract: int,
    max_contracts: int,
    vector_ids: list[int] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Best `per_contract` clauses per contract, contracts ranked by best match."""
    terms = keyword_terms(keywords)
    where, params = filters.to_sql("c")
    ranked: list[tuple[str, int]] = []
    if terms:
        rows = conn.execute(
            "SELECT k.id, k.contract_id FROM chunks_fts JOIN chunks k ON k.id = chunks_fts.rowid"  # noqa: S608
            f" JOIN contracts c ON c.contract_id = k.contract_id WHERE chunks_fts MATCH ? AND {where}"
            " ORDER BY bm25(chunks_fts) LIMIT ?",
            [fts_expression(terms, "or"), *params, max_contracts * per_contract * 4],
        ).fetchall()
        ranked = [(r["contract_id"], int(r["id"])) for r in rows]
    if vector_ids:
        placeholders = ",".join("?" * len(vector_ids))
        rows = conn.execute(
            f"SELECT k.id, k.contract_id FROM chunks k JOIN contracts c ON c.contract_id = k.contract_id"  # noqa: S608
            f" WHERE k.id IN ({placeholders}) AND {where}",
            [*vector_ids, *params],
        ).fetchall()
        by_id = {int(r["id"]): r["contract_id"] for r in rows}
        # Interleave so semantic matches are not always behind keyword matches.
        merged: list[tuple[str, int]] = []
        vector_ranked = [(by_id[i], i) for i in vector_ids if i in by_id]
        for index in range(max(len(ranked), len(vector_ranked))):
            if index < len(ranked):
                merged.append(ranked[index])
            if index < len(vector_ranked):
                merged.append(vector_ranked[index])
        ranked = merged

    grouped: dict[str, list[int]] = {}
    for contract_id, chunk_id in ranked:
        bucket = grouped.setdefault(contract_id, [])
        if chunk_id not in bucket and len(bucket) < per_contract:
            bucket.append(chunk_id)
    selected = list(grouped.items())[:max_contracts]
    return {contract_id: fetch_chunks(conn, ids) for contract_id, ids in selected}


# ---------------------------------------------------------------------------
# Quote verification (hallucination check)
# ---------------------------------------------------------------------------

_ELLIPSIS_RE = re.compile(r"\.{3,}|…|\[\.\.\.\]")
_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def normalize_for_match(text: str) -> str:
    return _NON_WORD_RE.sub(" ", fold(text)).strip()


# VARSAYIM: A quote counts as verified when its words (ignoring case, Turkish characters
# and punctuation) appear in that order in one candidate clause; "..." may skip text.
def verify_quote(quote: str, chunks: list[dict[str, Any]], preferred: int | None = None) -> dict[str, Any] | None:
    """Return the chunk that really contains `quote` (case/diacritic/punctuation-insensitive).

    Quotes may skip text with "..."; every part (>= 2 words) must occur, in order,
    in the same chunk."""
    parts = [normalize_for_match(p) for p in _ELLIPSIS_RE.split(quote)]
    parts = [p for p in parts if len(p.split()) >= 2 or (p and len(parts) == 1)]
    if not parts or sum(len(p) for p in parts) < 8:
        return None
    ordered = sorted(chunks, key=lambda c: c["chunk_id"] != preferred)
    for chunk in ordered:
        haystack = normalize_for_match(chunk["text"])
        position = 0
        for part in parts:
            found = haystack.find(part, position)
            if found < 0:
                break
            position = found + len(part)
        else:
            return chunk
    return None


# ---------------------------------------------------------------------------
# Evaluation of one contract
# ---------------------------------------------------------------------------


@dataclass
class JobContext:
    job_id: str
    user: str | None
    legislation: str
    summary: str
    topics: list[str]


def evaluation_input(
    ctx: JobContext, contract: dict[str, Any], chunks: list[dict[str, Any]], settings: Settings
) -> str:
    sources = []
    for number, chunk in enumerate(chunks, start=1):
        sources.append(
            prompts.wrap_document(
                chunk["text"][:4000],
                kaynak=number,
                madde=chunk["clause_ref"],
                baslik=chunk["clause_title"],
                sayfa=chunk["page_start"],
                ek=chunk["file_name"] if chunk["is_attachment"] else None,
            )
        )
    meta = (
        f"Sözleşme kimliği (contract_id): {contract['contract_id']}\n"
        f"Sözleşme no: {contract['contract_no'] or '-'}\n"
        f"Başlık: {contract['title'] or '-'}\n"
        f"Taraflar: {', '.join(contract['parties']) or '-'}\n"
        f"Tip: {contract['contract_type'] or '-'}\n"
        f"Süre: {contract['start_date'] or '?'} - {contract['end_date'] or '?'}  Durum: {contract['status'] or '-'}"
    )
    topics = ", ".join(ctx.topics) or "-"
    return (
        f"MEVZUAT DEĞİŞİKLİĞİ ÖZETİ:\n{ctx.summary or '-'}\nKonular: {topics}\n\n"
        f"{legislation_block(ctx.legislation, settings)}\n\n"
        f"SÖZLEŞME:\n{meta}\n\nADAY MADDELER:\n" + "\n\n".join(sources)
    )


def verify_evaluation(evaluation: ContractImpact, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clauses = []
    for clause in evaluation.affected_clauses:
        preferred = None
        if clause.source and 1 <= clause.source <= len(chunks):
            preferred = chunks[clause.source - 1]["chunk_id"]
        match = verify_quote(clause.quote, chunks, preferred)
        reference = match or (chunks[clause.source - 1] if preferred else None)
        clauses.append(
            {
                "clause_ref": (reference or {}).get("clause_ref") or clause.clause_ref,
                "clause_title": (reference or {}).get("clause_title"),
                "page": (reference or {}).get("page_start") or clause.page,
                "quote": clause.quote,
                "quote_verified": match is not None,
                "reason": clause.reason,
                "chunk_id": (reference or {}).get("chunk_id"),
                "file_id": (reference or {}).get("file_id"),
                "is_attachment": (reference or {}).get("is_attachment", False),
                "file_name": (reference or {}).get("file_name"),
            }
        )
    return clauses


async def evaluate_contract(
    llm: LLMClient, settings: Settings, ctx: JobContext, contract: dict[str, Any], chunks: list[dict[str, Any]]
) -> dict[str, Any]:
    parsed, _ = await llm.generate_json(
        prompts.IMPACT_EVALUATE,
        evaluation_input(ctx, contract, chunks, settings),
        CONTRACT_IMPACT,
        purpose="impact_evaluate",
        user=ctx.user,
    )
    assert isinstance(parsed, ContractImpact)  # noqa: S101
    clauses = verify_evaluation(parsed, chunks)
    level = parsed.impact_level
    if level == "ETKILENMEZ":
        clauses = []
    return {
        "contract_id": contract["contract_id"],  # never trust the model's id
        "impact_level": level,
        "confidence": parsed.confidence,
        "affected_clauses": clauses,
        "unverified_quotes": sum(1 for c in clauses if not c["quote_verified"]),
        "recommended_action": parsed.recommended_action,
        "summary": parsed.summary,
        "candidate_count": len(chunks),
    }


# ---------------------------------------------------------------------------
# Jobs (persistence)
# ---------------------------------------------------------------------------


def create_job(conn: sqlite3.Connection, request: RunRequest, user: str) -> str:
    job_id = uuid.uuid4().hex
    title = (request.title or "").strip() or request.text.strip().splitlines()[0][:120]
    conn.execute(
        "INSERT INTO analysis_jobs (id, created_by, created_at, status, title, legislation_text, extraction_json,"
        " keywords_json, filters_json, input_kind) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            user,
            utcnow_iso(),
            title,
            request.text,
            json.dumps(request.extraction or {}, ensure_ascii=False),
            json.dumps(clean_keywords(request.keywords), ensure_ascii=False),
            request.filters.model_dump_json(),
            request.input_kind,
        ),
    )
    return job_id


def _job_row(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM analysis_jobs WHERE id = ?", (job_id,)).fetchone()


def job_results(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.contract_id, r.impact_level, r.confidence, r.result_json, r.error, c.*"
        " FROM analysis_results r LEFT JOIN contracts c ON c.contract_id = r.contract_id WHERE r.job_id = ?",
        (job_id,),
    ).fetchall()
    results = []
    for row in rows:
        data = json.loads(row["result_json"] or "{}")
        contract = (
            contract_summary(row)
            if row["contract_no"] is not None or row["title"] is not None
            else {"contract_id": row["contract_id"]}
        )
        results.append({**data, **contract, "impact_level": row["impact_level"], "error": row["error"]})
    results.sort(
        key=lambda r: (LEVEL_ORDER.get(r["impact_level"], 9), -(r.get("confidence") or 0), r.get("contract_no") or "")
    )
    return results


def job_detail(conn: sqlite3.Connection, job_id: str, include_results: bool = True) -> dict[str, Any] | None:
    row = _job_row(conn, job_id)
    if row is None:
        return None
    counts = {
        r["impact_level"]: r["n"]
        for r in conn.execute(
            "SELECT impact_level, COUNT(*) AS n FROM analysis_results WHERE job_id = ? GROUP BY impact_level",
            (job_id,),
        )
    }
    detail = {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "total": row["total"],
        "processed": row["processed"],
        "error_message": row["error_message"],
        "cancel_requested": bool(row["cancel_requested"]),
        "keywords": json.loads(row["keywords_json"] or "[]"),
        "filters": json.loads(row["filters_json"] or "{}"),
        "extraction": json.loads(row["extraction_json"] or "{}"),
        "input_kind": row["input_kind"],
        "counts": counts,
    }
    if include_results:
        detail["legislation_text"] = row["legislation_text"]
        detail["results"] = job_results(conn, job_id)
    return detail


def job_history(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    ids = [r["id"] for r in conn.execute("SELECT id FROM analysis_jobs ORDER BY created_at DESC LIMIT ?", (limit,))]
    return [d for d in (job_detail(conn, job_id, include_results=False) for job_id in ids) if d]


def request_cancel(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = _job_row(conn, job_id)
    if row is None:
        raise AppError("job_not_found", "Analiz bulunamadı.", 404)
    if row["status"] in ("completed", "failed", "cancelled"):
        raise AppError("job_finished", "Analiz zaten tamamlanmış; iptal edilemez.", 409)
    now = utcnow_iso()
    conn.execute(
        "UPDATE analysis_jobs SET cancel_requested = 1,"
        " status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,"
        " finished_at = CASE WHEN status = 'queued' THEN ? ELSE finished_at END WHERE id = ?",
        (now, job_id),
    )
    return job_detail(conn, job_id, include_results=False) or {}


def _store_result(conn: sqlite3.Connection, job_id: str, result: dict[str, Any], error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO analysis_results (job_id, contract_id, impact_level, confidence, result_json, created_at, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            result["contract_id"],
            result["impact_level"],
            result.get("confidence"),
            json.dumps(result, ensure_ascii=False),
            utcnow_iso(),
            error,
        ),
    )
    conn.execute(
        "UPDATE analysis_jobs SET processed = processed + 1, heartbeat_at = ? WHERE id = ?", (utcnow_iso(), job_id)
    )


def _finish(conn: sqlite3.Connection, job_id: str, status: str, message: str | None = None) -> None:
    conn.execute(
        "UPDATE analysis_jobs SET status = ?, finished_at = ?, error_message = ? WHERE id = ?",
        (status, utcnow_iso(), message, job_id),
    )


def _cancel_requested(conn: sqlite3.Connection, job_id: str) -> bool:
    row = conn.execute("SELECT cancel_requested FROM analysis_jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row[0])


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------


async def run_job(job_id: str, llm: LLMClient, settings: Settings) -> str:
    """Execute a claimed job. Returns the final status."""
    conn = connect(settings.db_path)
    try:
        row = _job_row(conn, job_id)
        if row is None:
            return "missing"
        conn.execute("DELETE FROM analysis_results WHERE job_id = ?", (job_id,))
        conn.execute("UPDATE analysis_jobs SET processed = 0, total = 0, error_message = NULL WHERE id = ?", (job_id,))
        extraction = json.loads(row["extraction_json"] or "{}")
        keywords = json.loads(row["keywords_json"] or "[]")
        filters = ImpactFilters.model_validate_json(row["filters_json"] or "{}").to_contract_filters()
        ctx = JobContext(
            job_id=job_id,
            user=row["created_by"],
            legislation=row["legislation_text"],
            summary=extraction.get("summary", ""),
            topics=extraction.get("topics", []),
        )
        logger.info("impact_job_started", extra={"job_id": job_id, "keywords": len(keywords)})

        vector_ids: list[int] | None = None
        if settings.vector_search_enabled:
            try:
                from app.services.embeddings import vector_ranked_ids

                query = ctx.summary or ctx.legislation[:4000]
                vector = (await llm.embed([query], purpose="impact_embedding", user=ctx.user))[0]
                vector_ids = await asyncio.to_thread(
                    _vector_ids, settings, vector, filters, settings.IMPACT_MAX_CONTRACTS * 2, vector_ranked_ids
                )
            except AppError:
                logger.warning("impact_vector_search_failed", extra={"job_id": job_id})

        candidates = await asyncio.to_thread(_candidates, settings, keywords, filters, vector_ids)
        conn.execute("UPDATE analysis_jobs SET total = ? WHERE id = ?", (len(candidates), job_id))
        if not candidates:
            _finish(conn, job_id, "completed", None)
            return "completed"

        contracts = {
            cid: contract_summary(r)
            for cid in candidates
            if (r := conn.execute("SELECT * FROM contracts WHERE contract_id = ?", (cid,)).fetchone())
        }
        fatal: list[AppError] = []
        cancelled = False
        concurrency = asyncio.Semaphore(settings.LLM_MAX_CONCURRENCY)

        async def evaluate(contract_id: str, chunks: list[dict[str, Any]]) -> None:
            async with concurrency:
                if fatal or _cancel_requested(conn, job_id):
                    return
                contract = contracts.get(contract_id)
                if contract is None:
                    return
                try:
                    result = await evaluate_contract(llm, settings, ctx, contract, chunks)
                except Exception as exc:
                    if isinstance(exc, AppError) and exc.code in FATAL_LLM_CODES:
                        fatal.append(exc)
                        return
                    if not isinstance(exc, AppError):
                        logger.exception("impact_contract_failed", extra={"job_id": job_id, "contract_id": contract_id})
                    message = exc.message if isinstance(exc, AppError) else "Değerlendirme sırasında beklenmeyen hata."
                    _store_result(
                        conn,
                        job_id,
                        {
                            "contract_id": contract_id,
                            "impact_level": "DEGERLENDIRILEMEDI",
                            "affected_clauses": [],
                            "candidate_count": len(chunks),
                        },
                        message,
                    )
                    return
                _store_result(conn, job_id, result)

        tasks = [asyncio.create_task(evaluate(cid, chunks)) for cid, chunks in candidates.items()]
        try:
            while tasks:
                done, pending = await asyncio.wait(tasks, timeout=1.0)
                tasks = list(pending)
                if _cancel_requested(conn, job_id):
                    cancelled = True
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    break
                for task in done:
                    if task.exception():
                        raise task.exception()  # type: ignore[misc]
        finally:
            # Whatever happens (DB error, shutdown), no evaluation may keep running and
            # spending tokens after this function returns and closes the connection.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if fatal:
            _finish(conn, job_id, "failed", fatal[0].message)
            return "failed"
        if cancelled:
            _finish(conn, job_id, "cancelled", "Analiz kullanıcı tarafından iptal edildi.")
            return "cancelled"
        _finish(conn, job_id, "completed", None)
        logger.info("impact_job_completed", extra={"job_id": job_id, "contracts": len(candidates)})
        return "completed"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("impact_job_failed", extra={"job_id": job_id})
        message = exc.message if isinstance(exc, AppError) else "Analiz sırasında beklenmeyen bir hata oluştu."
        _finish(conn, job_id, "failed", message)
        return "failed"
    finally:
        conn.close()


def _candidates(
    settings: Settings, keywords: list[str], filters: ContractFilters, vector_ids: list[int] | None
) -> dict[str, list[dict[str, Any]]]:
    conn = connect(settings.db_path)
    try:
        return find_candidates(
            conn,
            keywords,
            filters,
            per_contract=settings.IMPACT_CHUNKS_PER_CONTRACT,
            max_contracts=settings.IMPACT_MAX_CONTRACTS,
            vector_ids=vector_ids,
        )
    finally:
        conn.close()


def _vector_ids(settings, vector, filters, limit, ranker) -> list[int]:  # type: ignore[no-untyped-def]
    conn = connect(settings.db_path)
    try:
        return ranker(conn, vector, settings.EMBEDDING_MODEL, filters, limit)
    finally:
        conn.close()
