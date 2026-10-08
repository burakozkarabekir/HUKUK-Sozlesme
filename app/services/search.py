"""Keyword search (FTS5) and hybrid retrieval (FTS5 + optional vectors) for RAG.

Queries and the index both use `fold()`, so case and Turkish characters do not
matter ("FESİH" == "fesih", "sozlesme" == "sözleşme"). Single words are
prefix-matched because Turkish is agglutinative ("sözleşme" also finds
"sözleşmenin"); "quoted phrases" match exactly.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.ingest.normalize import fold, fold_tokens
from app.services.contracts import ContractFilters, like_escape

# Folded Turkish stop words, removed from natural-language questions (not from
# explicit keyword searches).
STOPWORDS = {
    "acaba", "ait", "ama", "ancak", "bana", "bir", "biri", "bu", "bunu", "cok", "da", "daha", "de", "diye",
    "en", "gibi", "hangi", "hangileri", "hem", "her", "icin", "ile", "ise", "kadar", "ki", "kim", "kimdir",
    "mi", "midir", "mu", "mudur", "musun", "nasil", "ne", "neden", "nedir", "neler", "nelerdir", "nerede",
    "o", "olan", "olarak", "olur", "sey", "su", "uzere", "var", "ve", "veya", "ya", "yani", "yok",
    "zaman", "mı", "midır", "sozlesmede", "sozlesmenin", "sozlesmesi", "sozlesmeler", "sozlesmelerde",
    "sozlesme", "madde", "maddesi", "maddede", "maddeler", "firmasi", "firmasiyla", "firma",
}  # fmt: skip

_PHRASE_RE = re.compile(r'"([^"]+)"')
MAX_SNIPPET_CHARS = 320


@dataclass(frozen=True)
class Term:
    tokens: tuple[str, ...]
    phrase: bool = False

    @property
    def prefix(self) -> bool:
        return not self.phrase and len(self.tokens[0]) >= 3 and not self.tokens[0].isdigit()

    def fts(self) -> str:
        quoted = '"' + " ".join(self.tokens) + '"'
        return quoted + "*" if self.prefix else quoted

    def regex(self) -> str:
        body = r"\W+".join(re.escape(t) for t in self.tokens)
        return rf"\b{body}\w*" if self.prefix else rf"\b{body}\b"


def parse_query(query: str, drop_stopwords: bool = False) -> list[Term]:
    terms: list[Term] = []
    for phrase in _PHRASE_RE.findall(query):
        tokens = tuple(fold_tokens(phrase))
        if tokens:
            terms.append(Term(tokens, phrase=len(tokens) > 1))
    rest = _PHRASE_RE.sub(" ", query)
    seen = {t.tokens for t in terms}
    for token in fold_tokens(rest):
        token = token.strip("_")
        if not token or (drop_stopwords and (token in STOPWORDS or len(token) < 2)):
            continue
        if (token,) not in seen:
            seen.add((token,))
            terms.append(Term((token,)))
    return terms


def fts_expression(terms: list[Term], mode: str = "and") -> str:
    joiner = " OR " if mode == "or" else " AND "
    return joiner.join(term.fts() for term in terms)


def highlight_segments(text: str, terms: list[Term], max_chars: int = MAX_SNIPPET_CHARS) -> list[dict[str, Any]]:
    """Snippet of `text` around the hits as [{"text", "hit"}] (rendered safely by the UI).

    Works on the folded text, whose positions equal the original's (see fold())."""
    folded = fold(text)
    spans: list[tuple[int, int]] = []
    if terms:
        pattern = re.compile("|".join(f"(?:{t.regex()})" for t in terms))
        spans = [m.span() for m in pattern.finditer(folded) if m.end() > m.start()]
    if spans:
        start = max(0, spans[0][0] - 80)
        while start > 0 and not text[start - 1].isspace():
            start -= 1
    else:
        start = 0
    end = min(len(text), start + max_chars)
    segments: list[dict[str, Any]] = []
    if start > 0:
        segments.append({"text": "…", "hit": False})
    cursor = start
    for s, e in spans:
        if s >= end:
            break
        if e <= cursor:
            continue
        s = max(s, cursor)
        if s > cursor:
            segments.append({"text": text[cursor:s], "hit": False})
        segments.append({"text": text[s : min(e, end)], "hit": True})
        cursor = min(e, end)
    if cursor < end:
        segments.append({"text": text[cursor:end], "hit": False})
    if end < len(text):
        segments.append({"text": "…", "hit": False})
    for segment in segments:
        segment["text"] = re.sub(r"\s+", " ", segment["text"])
    return segments


_ROW_COLUMNS = """
    k.id AS chunk_id, k.contract_id, k.file_id, k.is_attachment, k.clause_ref, k.clause_title,
    k.page_start, k.page_end, k.text, c.contract_no, c.title, c.parties_json, c.contract_type,
    c.start_date, c.end_date, c.status, f.file_name
"""
_ROW_JOINS = """
    JOIN contracts c ON c.contract_id = k.contract_id
    LEFT JOIN contract_files f ON f.contract_id = k.contract_id AND f.file_id = k.file_id
"""


def _row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["parties"] = json.loads(data.pop("parties_json") or "[]")
    data["is_attachment"] = bool(data["is_attachment"])
    return data


def keyword_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    mode: str = "and",
    filters: ContractFilters | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    terms = parse_query(query)
    if not terms:
        return {"total": 0, "items": [], "terms": []}
    where, params = (filters or ContractFilters()).to_sql("c")
    match = fts_expression(terms, mode)
    base = f"FROM chunks_fts JOIN chunks k ON k.id = chunks_fts.rowid {_ROW_JOINS} WHERE chunks_fts MATCH ? AND {where}"
    total = conn.execute(f"SELECT COUNT(*) {base}", [match, *params]).fetchone()[0]
    rows = conn.execute(
        f"SELECT {_ROW_COLUMNS}, bm25(chunks_fts) AS score {base} ORDER BY score LIMIT ? OFFSET ?",
        [match, *params, limit, offset],
    ).fetchall()
    items = []
    for row in rows:
        item = _row(row)
        item["snippet"] = highlight_segments(item.pop("text"), terms)
        items.append(item)
    return {"total": int(total), "items": items, "terms": [" ".join(t.tokens) for t in terms]}


def fts_ranked_ids(
    conn: sqlite3.Connection,
    terms: list[Term],
    filters: ContractFilters,
    limit: int,
    mode: str = "or",
) -> list[int]:
    if not terms:
        return []
    where, params = filters.to_sql("c")
    rows = conn.execute(
        f"SELECT k.id FROM chunks_fts JOIN chunks k ON k.id = chunks_fts.rowid"  # noqa: S608
        f" JOIN contracts c ON c.contract_id = k.contract_id"
        f" WHERE chunks_fts MATCH ? AND {where} ORDER BY bm25(chunks_fts) LIMIT ?",
        [fts_expression(terms, mode), *params, limit],
    ).fetchall()
    return [int(r[0]) for r in rows]


def matching_contracts(conn: sqlite3.Connection, terms: list[Term], max_matches: int = 5) -> list[str]:
    """Contracts whose metadata (no, title, parties) mentions a query word - e.g. a company name."""
    ids: set[str] = set()
    for term in terms:
        word = " ".join(term.tokens)
        if len(word) < 3 or word in STOPWORDS:
            continue
        rows = conn.execute(
            "SELECT contract_id FROM contracts WHERE meta_norm LIKE ? ESCAPE '\\' LIMIT ?",
            (f"%{like_escape(word)}%", max_matches + 1),
        ).fetchall()
        if 0 < len(rows) <= max_matches:
            ids.update(r[0] for r in rows)
    return sorted(ids)[: max_matches * 2]


def rrf_fuse(rankings: list[list[int]], k: int = 60, weights: list[float] | None = None) -> list[int]:
    """Reciprocal rank fusion of several ranked id lists."""
    scores: dict[int, float] = {}
    for index, ranking in enumerate(rankings):
        weight = weights[index] if weights else 1.0
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + weight / (k + rank + 1)
    return [item for item, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]


def fetch_chunks(conn: sqlite3.Connection, ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT {_ROW_COLUMNS} FROM chunks k {_ROW_JOINS} WHERE k.id IN ({placeholders})",  # noqa: S608
        ids,
    ).fetchall()
    by_id = {r["chunk_id"]: _row(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def retrieve(
    conn: sqlite3.Connection,
    question: str,
    *,
    k: int = 8,
    filters: ContractFilters | None = None,
    query_vector: list[float] | None = None,
    embedding_model: str | None = None,
) -> list[dict[str, Any]]:
    """Hybrid retrieval for chat: FTS5 (OR, stop words removed) + vectors + company-name boost."""
    filters = filters or ContractFilters()
    terms = parse_query(question, drop_stopwords=True)
    rankings: list[list[int]] = []
    weights: list[float] = []

    rankings.append(fts_ranked_ids(conn, terms, filters, limit=40))
    weights.append(1.0)

    if query_vector is not None and embedding_model:
        from app.services.embeddings import vector_ranked_ids

        rankings.append(vector_ranked_ids(conn, query_vector, embedding_model, filters, limit=40))
        weights.append(1.0)

    if not filters.contract_ids:
        named = matching_contracts(conn, terms)
        if named:
            # OR query inside the named contracts: the company name itself rarely
            # appears in clause text, the other words decide the ranking there.
            scoped = filters.model_copy(update={"contract_ids": named})
            rankings.append(fts_ranked_ids(conn, terms, scoped, limit=20))
            weights.append(1.5)

    fused = rrf_fuse([r for r in rankings if r], weights=[w for r, w in zip(rankings, weights, strict=True) if r])
    return fetch_chunks(conn, fused[:k])


def contract_chunks(conn: sqlite3.Connection, contract_id: str, limit: int) -> list[dict[str, Any]]:
    ids = [
        int(r[0])
        for r in conn.execute(
            "SELECT id FROM chunks WHERE contract_id = ? ORDER BY is_attachment, file_id, ordinal LIMIT ?",
            (contract_id, limit),
        )
    ]
    return fetch_chunks(conn, ids)
