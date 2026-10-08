"""Chat service. Phase 2: plain LLM chat (no retrieval); phase 4 adds RAG."""

from __future__ import annotations

from app.config import Settings
from app.models import ChatMessage


def build_chat_input(history: list[ChatMessage], question: str, settings: Settings) -> list[dict[str, str]]:
    """Last N history turns that fit the character budget, followed by the question.

    Oldest turns are dropped first; the question itself is always kept (its
    length is validated by the caller).
    """
    budget = settings.CHAT_MAX_PROMPT_CHARS - len(question)
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
