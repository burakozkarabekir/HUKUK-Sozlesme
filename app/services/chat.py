"""RAG chat: question -> (rewrite) -> hybrid retrieval -> prompt with numbered sources."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.db import connect
from app.errors import AppError
from app.llm import prompts
from app.llm.client import LLMClient
from app.models import ChatMessage
from app.services.contracts import ContractFilters
from app.services.search import contract_chunks, retrieve

logger = logging.getLogger(__name__)

SOURCE_SHARE = 0.7  # share of CHAT_MAX_PROMPT_CHARS reserved for sources
MAX_SOURCE_CHARS = 4000  # a single (very long) clause is truncated to this


@dataclass
class ChatPlan:
    instructions: str
    llm_input: list[dict[str, str]]
    sources: list[dict[str, Any]] = field(default_factory=list)
    search_query: str = ""

    @property
    def has_sources(self) -> bool:
        return bool(self.sources)


def build_chat_input(
    history: list[ChatMessage], question: str, settings: Settings, budget: int | None = None
) -> list[dict[str, str]]:
    """Last N history turns that fit the character budget, followed by the question.

    Oldest turns are dropped first; the question itself is always kept (its
    length is validated by the caller).
    """
    budget = (settings.CHAT_MAX_PROMPT_CHARS if budget is None else budget) - len(question)
    recent = history[-settings.CHAT_HISTORY_MESSAGES :] if settings.CHAT_HISTORY_MESSAGES else []
    kept: list[ChatMessage] = []
    for turn in reversed(recent):
        if len(turn.content) > budget:
            break
        kept.append(turn)
        budget -= len(turn.content)
    kept.reverse()
    # Start with a user turn so the model never sees a dangling assistant reply.
    while kept and kept[0].role != "user":
        kept.pop(0)
    return [{"role": t.role, "content": t.content} for t in kept] + [{"role": "user", "content": question}]


def public_source(number: int, chunk: dict[str, Any]) -> dict[str, Any]:
    """Source as sent to the browser (SSE `sources` event)."""
    return {
        "n": number,
        "chunk_id": chunk["chunk_id"],
        "contract_id": chunk["contract_id"],
        "contract_no": chunk["contract_no"],
        "title": chunk["title"],
        "parties": chunk["parties"],
        "clause_ref": chunk["clause_ref"],
        "clause_title": chunk["clause_title"],
        "page_start": chunk["page_start"],
        "page_end": chunk["page_end"],
        "file_id": chunk["file_id"],
        "file_name": chunk["file_name"],
        "is_attachment": chunk["is_attachment"],
        "text": chunk["text"],
    }


def format_sources(sources: list[dict[str, Any]]) -> str:
    blocks = []
    for source in sources:
        pages = source["page_start"]
        if source["page_end"] and source["page_end"] != source["page_start"]:
            pages = f"{source['page_start']}-{source['page_end']}"
        blocks.append(
            prompts.wrap_document(
                source["text"][:MAX_SOURCE_CHARS],
                kaynak=source["n"],
                sozlesme_no=source["contract_no"] or source["contract_id"],
                baslik=source["title"],
                taraflar=", ".join(source["parties"]),
                madde=source["clause_ref"],
                sayfa=pages,
                ek=source["file_name"] if source["is_attachment"] else None,
            )
        )
    return "\n\n".join(blocks)


def select_sources(chunks: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used = 0
    for chunk in chunks:
        size = min(len(chunk["text"]), MAX_SOURCE_CHARS) + 300
        if selected and used + size > budget:
            break
        selected.append(public_source(len(selected) + 1, chunk))
        used += size
    return selected


async def rewrite_query(llm: LLMClient, history: list[ChatMessage], question: str, user: str) -> str:
    recent = "\n".join(f"{m.role}: {m.content[:500]}" for m in history[-4:])
    try:
        result = await llm.complete(
            prompts.QUERY_REWRITE,
            f"Sohbet geçmişi:\n{recent}\n\nSon soru: {question}",
            purpose="chat_rewrite",
            user=user,
        )
    except AppError:
        logger.warning("chat_query_rewrite_failed")
        return question
    rewritten = result.text.strip().splitlines()[0].strip() if result.text.strip() else ""
    return rewritten[:500] or question


async def plan_answer(
    llm: LLMClient,
    settings: Settings,
    question: str,
    history: list[ChatMessage],
    *,
    user: str,
    contract_id: str | None = None,
) -> ChatPlan:
    search_query = question
    if history and settings.CHAT_REWRITE_QUERY:
        search_query = await rewrite_query(llm, history, question, user)

    query_vector = None
    if settings.vector_search_enabled:
        try:
            query_vector = (await llm.embed([search_query], purpose="chat_embedding", user=user))[0]
        except AppError:
            logger.warning("chat_query_embedding_failed")

    filters = ContractFilters(contract_ids=[contract_id] if contract_id else None)

    def _retrieve() -> list[dict[str, Any]]:
        conn = connect(settings.db_path)
        try:
            chunks = retrieve(
                conn,
                search_query,
                k=settings.CHAT_TOP_K,
                filters=filters,
                query_vector=query_vector,
                embedding_model=settings.EMBEDDING_MODEL or None,
            )
            if contract_id and len(chunks) < settings.CHAT_TOP_K:
                # "Only this contract": fill up with the contract's clauses in order.
                known = {c["chunk_id"] for c in chunks}
                extra = [c for c in contract_chunks(conn, contract_id, 200) if c["chunk_id"] not in known]
                chunks += extra
            return chunks
        finally:
            conn.close()

    chunks = await asyncio.to_thread(_retrieve)
    source_budget = int(settings.CHAT_MAX_PROMPT_CHARS * SOURCE_SHARE)
    sources = select_sources(chunks, source_budget)
    final_question = (
        f"<kaynaklar>\n{format_sources(sources)}\n</kaynaklar>\n\nSoru: {question}" if sources else question
    )
    # History gets whatever budget the sources leave.
    llm_input = build_chat_input(history, final_question, settings)
    return ChatPlan(prompts.CHAT_RAG, llm_input, sources, search_query)
