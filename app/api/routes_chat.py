"""Chat endpoint streaming the answer as Server-Sent Events.

Events: `start`, `sources` {"sources": [...]}, `delta` {"text"}, `done` {"usage"},
`error` {"code", "message"}. Errors before streaming starts (validation,
missing configuration, retrieval) return the normal JSON error format.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.deps import audit, get_db
from app.auth import get_current_user
from app.config import Settings, get_settings
from app.errors import AppError
from app.llm import prompts
from app.llm.client import LLMClient
from app.models import ChatRequest, CurrentUser
from app.services.chat import ChatPlan, plan_answer

router = APIRouter(prefix="/api", tags=["chat"])
logger = logging.getLogger(__name__)


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def sse_answer(llm: LLMClient, plan: ChatPlan, user: str) -> AsyncIterator[str]:
    yield sse("start", {"search_query": plan.search_query})
    yield sse("sources", {"sources": plan.sources})
    if not plan.has_sources:
        # Nothing relevant in the archive: answer without calling the model.
        yield sse("delta", {"text": prompts.NOT_FOUND_ANSWER})
        yield sse("done", {"usage": None})
        return
    stream = llm.stream_text(plan.instructions, plan.llm_input, purpose="chat", user=user)
    try:
        async for delta in stream:
            yield sse("delta", {"text": delta})
    except AppError as exc:
        yield sse("error", {"code": exc.code, "message": exc.message})
        return
    except Exception:
        logger.exception("chat_stream_failed")
        yield sse("error", {"code": "internal_error", "message": "Beklenmeyen bir hata oluştu. Lütfen tekrar deneyin."})
        return
    usage = stream.result.usage if stream.result else None
    yield sse("done", {"usage": usage.__dict__ if usage else None})


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    llm: LLMClient = Depends(get_llm),
    conn: sqlite3.Connection = Depends(get_db),
) -> StreamingResponse:
    question = body.message.strip()
    if not question:
        raise AppError("empty_question", "Lütfen bir soru yazın.", 400)
    if len(question) > settings.CHAT_MAX_QUESTION_CHARS:
        raise AppError("question_too_long", f"Soru en fazla {settings.CHAT_MAX_QUESTION_CHARS} karakter olabilir.", 413)
    llm.ensure_configured()
    if (
        body.contract_id
        and conn.execute("SELECT 1 FROM contracts WHERE contract_id = ?", (body.contract_id,)).fetchone() is None
    ):
        raise AppError("contract_not_found", "Seçilen sözleşme bulunamadı.", 404)

    plan = await plan_answer(llm, settings, question, body.history, user=user.username, contract_id=body.contract_id)
    # Audit: the question and the contracts whose clauses were shown - no texts.
    audit(
        conn,
        request,
        user,
        "chat.ask",
        target_type="contract",
        target_ids=sorted({s["contract_id"] for s in plan.sources}),
        details={
            "question": question,
            "scope_contract_id": body.contract_id,
            "source_chunks": [s["chunk_id"] for s in plan.sources],
        },
    )
    return StreamingResponse(
        sse_answer(llm, plan, user.username),
        media_type="text/event-stream",
        # X-Accel-Buffering: nginx must not buffer SSE.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
