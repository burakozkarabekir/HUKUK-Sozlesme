"""Fake `AsyncOpenAI().responses` used to test the LLM layer without HTTP."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx2
import openai

_REQUEST = httpx2.Request("POST", "https://example.invalid/openai/v1/responses")


def api_error(cls: type[openai.APIStatusError], status: int, message: str, code: str | None = None) -> Exception:
    body = {"error": {"message": message, "code": code}}
    error = cls(message, response=httpx2.Response(status, request=_REQUEST, json=body), body=body)
    error.code = code
    return error


def timeout_error() -> Exception:
    return openai.APITimeoutError(request=_REQUEST)


def response(text: str, input_tokens: int = 10, output_tokens: int = 5) -> SimpleNamespace:
    usage = SimpleNamespace(
        input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=input_tokens + output_tokens
    )
    return SimpleNamespace(output_text=text, usage=usage)


class FakeStream:
    def __init__(self, deltas: list[str], usage: SimpleNamespace, fail_with: Exception | None = None) -> None:
        self.deltas = deltas
        self.usage = usage
        self.fail_with = fail_with
        self.closed = False

    def __aiter__(self) -> AsyncIterator[SimpleNamespace]:
        return self._events()

    async def _events(self) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(type="response.created")
        for delta in self.deltas:
            yield SimpleNamespace(type="response.output_text.delta", delta=delta)
        if self.fail_with is not None:
            raise self.fail_with
        yield SimpleNamespace(type="response.completed", response=SimpleNamespace(usage=self.usage))

    async def close(self) -> None:
        self.closed = True


class FakeResponses:
    """Scripted outcomes, consumed in order. Each item is either an Exception to
    raise, a response object, or a FakeStream (returned for stream=True calls)."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.outcomes:
            raise AssertionError("FakeResponses: no scripted outcome left")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def stream(*deltas: str, input_tokens: int = 7, output_tokens: int = 3) -> FakeStream:
    usage = SimpleNamespace(
        input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=input_tokens + output_tokens
    )
    return FakeStream(list(deltas), usage)


class RuleResponses:
    """Fake whose answer is computed from the request (for concurrent calls)."""

    def __init__(self, responder) -> None:  # responder(kwargs) -> response | Exception
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.responder(kwargs)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
