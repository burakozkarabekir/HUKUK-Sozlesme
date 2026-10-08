"""Chat endpoint streaming the answer as Server-Sent Events.

Events: `start`, `delta` {"text"}, `done` {"usage"}, `error` {"code", "message"}.
Errors detected before streaming starts (validation, missing configuration)
return the normal JSON error format instead.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_db
from app.auth import get_current_user
from app.config import Settings, get_settings
from app.errors import AppError
from app.llm import prompts
from app.llm.client import LLMClient, TextStream
from app.logging_conf import write_audit
from app.models import ChatRequest, CurrentUser
from app.services.chat import build_chat_input

router = APIRouter(prefix="/api", tags=["chat"])
logger = logging.getLogger(__name__)


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def sse_answer(stream: TextStream) -> AsyncIterator[str]:
    yield sse("start", {})
    try:
        async for delta in stream:
            yield sse("delta", {"text": delta})
    except AppError as exc:
        yield sse("error", {"code": exc.code, "message": exc.message})
        return
    except Exception:
        logger.exception("chat_stream_failed")
        yield sse(
            "error",
            {"code": "internal_error", "message": "Beklenmeyen bir hata oluştu. Lütfen tekrar deneyin."},
        )
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

    # Audit: user question and IDs only, never answers or contract text.
    write_audit(
        conn,
        "chat.ask",
        user_id=user.username,
        target_type="contract" if body.contract_id else None,
        target_ids=[body.contract_id] if body.contract_id else [],
        details={"question": question},
        ip=request.client.host if request.client else None,
    )
    stream = llm.stream_text(
        prompts.CHAT_NO_RAG,
        build_chat_input(body.history, question, settings),
        purpose="chat",
        user=user.username,
    )
    return StreamingResponse(
        sse_answer(stream),
        media_type="text/event-stream",
        # X-Accel-Buffering: nginx must not buffer SSE.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
