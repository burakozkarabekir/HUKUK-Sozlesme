"""Optional vector search: embeddings stored in SQLite, cosine similarity with numpy.

Active only when EMBEDDING_MODEL is set (an Azure embedding deployment name).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

import numpy as np
import openai

from app.config import Settings
from app.llm.client import map_openai_error
from app.net import build_ssl_context
from app.services.contracts import ContractFilters

logger = logging.getLogger(__name__)
BATCH_SIZE = 64
SyncEmbedder = Callable[[list[str]], list[list[float]]]


def to_blob(vector: list[float]) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def store_vectors(conn: sqlite3.Connection, model: str, ids: list[int], vectors: list[list[float]]) -> None:
    conn.executemany(
        "INSERT INTO embeddings (chunk_id, model, dim, vector) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(chunk_id) DO UPDATE SET model = excluded.model, dim = excluded.dim, vector = excluded.vector",
        [(chunk_id, model, len(vec), to_blob(vec)) for chunk_id, vec in zip(ids, vectors, strict=True)],
    )


def make_sync_embedder(
    settings: Settings, client: openai.OpenAI | None = None
) -> Callable[[list[str]], list[list[float]]]:
    """Blocking embedder for the sync pipeline (runs in a worker thread / CLI)."""
    if client is None:
        client = openai.OpenAI(
            base_url=settings.AZURE_OPENAI_BASE_URL,
            api_key=settings.AZURE_OPENAI_API_KEY.get_secret_value(),
            timeout=settings.LLM_TIMEOUT_SECONDS,
            max_retries=settings.LLM_MAX_RETRIES,
            http_client=openai.DefaultHttpxClient(verify=build_ssl_context(settings)),
        )

    def embed(texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = [t[:8000] for t in texts[start : start + BATCH_SIZE]]
            try:
                response = client.embeddings.create(model=settings.EMBEDDING_MODEL, input=batch)
            except openai.OpenAIError as exc:
                raise map_openai_error(exc) from exc
            vectors.extend(item.embedding for item in sorted(response.data, key=lambda d: d.index))
        return vectors

    return embed


def vector_ranked_ids(
    conn: sqlite3.Connection,
    query_vector: list[float],
    model: str,
    filters: ContractFilters,
    limit: int,
) -> list[int]:
    where, params = filters.to_sql("c")
    rows = conn.execute(
        "SELECT e.chunk_id, e.vector FROM embeddings e JOIN chunks k ON k.id = e.chunk_id"  # noqa: S608
        f" JOIN contracts c ON c.contract_id = k.contract_id WHERE e.model = ? AND {where}",
        [model, *params],
    ).fetchall()
    if not rows:
        return []
    query = np.asarray(query_vector, dtype=np.float32)
    matrix = np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows if len(r["vector"]) == query.nbytes])
    ids = [r["chunk_id"] for r in rows if len(r["vector"]) == query.nbytes]
    if not ids:
        return []
    norms = np.linalg.norm(matrix, axis=1) * (np.linalg.norm(query) or 1.0)
    scores = matrix @ query / np.where(norms == 0, 1.0, norms)
    order = np.argsort(-scores)[:limit]
    return [int(ids[i]) for i in order]


def embed_missing(
    conn: sqlite3.Connection, settings: Settings, embedder: Callable[[list[str]], list[list[float]]]
) -> int:
    rows = conn.execute(
        "SELECT k.id, k.text FROM chunks k LEFT JOIN embeddings e ON e.chunk_id = k.id AND e.model = ?"
        " WHERE e.chunk_id IS NULL",
        (settings.EMBEDDING_MODEL,),
    ).fetchall()
    for start in range(0, len(rows), BATCH_SIZE * 4):
        batch = rows[start : start + BATCH_SIZE * 4]
        store_vectors(conn, settings.EMBEDDING_MODEL, [r["id"] for r in batch], embedder([r["text"] for r in batch]))
    return len(rows)
