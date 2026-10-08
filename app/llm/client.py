"""Azure AI Foundry wrapper around the OpenAI Responses API (`/openai/v1/`).

Features: concurrency limit (asyncio semaphore), SDK retries with backoff for
429/5xx/connection errors, timeouts, structured output with a prompt-based
fallback + one repair round, streaming with a non-streaming fallback, token
usage logging. Prompt contents are never logged.

Tests inject a fake `responses_api` (any object with an async `create(**kwargs)`
that mimics `AsyncOpenAI().responses`), so no HTTP mocking is needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import openai
from pydantic import BaseModel

from app.config import Settings
from app.errors import AppError
from app.llm import prompts
from app.llm.schemas import StructuredSpec
from app.net import build_ssl_context

logger = logging.getLogger(__name__)

LLMInput = str | list[dict[str, str]]

# Phrases in a 400 response that indicate a feature is not supported by the
# model/deployment (as opposed to a genuinely bad request).
_STRUCTURED_HINTS = ("json_schema", "response_format", "text.format", "structured", "strict")
_STREAM_HINTS = ("stream",)
_UNSUPPORTED_HINTS = (
    "not supported",
    "unsupported",
    "not available",
    "does not support",
    "only supported",
    "enabled only",
)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_response(cls, usage: Any) -> Usage:
        if usage is None:
            return cls()
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        total = int(getattr(usage, "total_tokens", 0) or 0) or input_tokens + output_tokens
        return cls(input_tokens, output_tokens, total)

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.total_tokens + other.total_tokens,
        )


@dataclass
class LLMResult:
    text: str
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0
    structured_mode: str | None = None
    streamed: bool = False


UsageRecorder = Callable[[str, str, str | None, Usage], Awaitable[None]]


class LLMError(AppError):
    """LLM failure with a Turkish, user-facing message."""


def map_openai_error(exc: Exception) -> LLMError:
    """Translate SDK exceptions to user-facing errors; log technical detail."""
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    logger.warning(
        "llm_error",
        extra={
            "error_type": type(exc).__name__,
            "status": status,
            "error_code": code,
            "detail": str(exc)[:500],
        },
    )
    if isinstance(exc, openai.APITimeoutError):
        return LLMError("llm_timeout", "Yapay zeka servisi zamanında yanıt vermedi. Lütfen tekrar deneyin.", 504)
    if isinstance(exc, openai.APIConnectionError):
        return LLMError(
            "llm_unreachable",
            "Yapay zeka servisine bağlanılamadı. Ağ, proxy veya SSL sertifikası ayarlarını kontrol edin.",
            502,
        )
    if isinstance(exc, openai.AuthenticationError):
        return LLMError(
            "llm_auth", "Yapay zeka servisi kimlik doğrulamasını reddetti. API anahtarını kontrol edin.", 502
        )
    if isinstance(exc, openai.PermissionDeniedError):
        return LLMError(
            "llm_forbidden",
            "Yapay zeka servisine erişim izni yok (403). Ağ kısıtlarını ve yetkileri kontrol edin.",
            502,
        )
    if isinstance(exc, openai.NotFoundError):
        return LLMError(
            "llm_not_found",
            "Model veya uç nokta bulunamadı. CHAT_MODEL (deployment adı) ve "
            "AZURE_OPENAI_BASE_URL değerlerini kontrol edin.",
            502,
        )
    if isinstance(exc, openai.RateLimitError):
        return LLMError(
            "llm_rate_limited",
            "Yapay zeka servisi şu anda yoğun (kota sınırı). Lütfen biraz sonra tekrar deneyin.",
            503,
        )
    if isinstance(exc, openai.BadRequestError):
        if code == "content_filter" or "content_filter" in str(exc):
            return LLMError(
                "llm_content_filter",
                "İstek, yapay zeka servisinin içerik filtresi tarafından engellendi.",
                422,
            )
        return LLMError("llm_bad_request", "Yapay zeka servisi isteği geçersiz buldu.", 502)
    return LLMError("llm_server_error", "Yapay zeka servisinde geçici bir hata oluştu. Lütfen tekrar deneyin.", 502)


def _looks_unsupported(exc: Exception, feature_hints: tuple[str, ...]) -> bool:
    if not isinstance(exc, openai.BadRequestError | openai.UnprocessableEntityError):
        return False
    text = str(exc).lower()
    return any(h in text for h in feature_hints) and any(h in text for h in _UNSUPPORTED_HINTS)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


def parse_json_model(text: str, model: type[BaseModel]) -> BaseModel:
    """Parse model output as JSON (tolerating code fences / chatter) and validate it."""
    candidate = text.strip()
    fenced = _FENCE_RE.match(candidate)
    if fenced:
        candidate = fenced.group(1)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("Cevap JSON nesnesi içermiyor.") from None
        try:
            data = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"Geçersiz JSON: {exc}") from None
    return model.model_validate(data)


def _as_messages(llm_input: LLMInput) -> list[dict[str, str]]:
    return [{"role": "user", "content": llm_input}] if isinstance(llm_input, str) else list(llm_input)


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        responses_api: Any | None = None,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self.settings = settings
        self._responses = responses_api
        self._client: openai.AsyncOpenAI | None = None
        self._usage_recorder = usage_recorder
        self._semaphore: asyncio.Semaphore | None = None
        # None = auto (probe, then remember what worked).
        self.structured_mode: str | None = (
            None if settings.LLM_STRUCTURED_MODE == "auto" else settings.LLM_STRUCTURED_MODE
        )
        self.streaming_supported: bool | None = {"on": True, "off": False}.get(settings.LLM_STREAMING)

    @property
    def model(self) -> str:
        return self.settings.CHAT_MODEL

    def ensure_configured(self) -> None:
        if self._responses is None and not self.settings.llm_configured:
            raise LLMError(
                "llm_not_configured",
                "Yapay zeka servisi yapılandırılmamış (AZURE_OPENAI_BASE_URL / AZURE_OPENAI_API_KEY). "
                "Lütfen yöneticiye başvurun.",
                503,
            )

    def _api(self) -> Any:
        if self._responses is None:
            self.ensure_configured()
            # VARSAYIM: SDK retries (exponential backoff on 429/5xx/connection errors)
            # satisfy the retry requirement; LLM_MAX_RETRIES is passed straight through.
            http_client = openai.DefaultAsyncHttpxClient(
                verify=build_ssl_context(self.settings), timeout=self.settings.LLM_TIMEOUT_SECONDS
            )
            self._client = openai.AsyncOpenAI(
                base_url=self.settings.AZURE_OPENAI_BASE_URL,
                api_key=self.settings.AZURE_OPENAI_API_KEY.get_secret_value(),
                timeout=self.settings.LLM_TIMEOUT_SECONDS,
                max_retries=self.settings.LLM_MAX_RETRIES,
                http_client=http_client,
            )
            self._responses = self._client.responses
        return self._responses

    def _sem(self) -> asyncio.Semaphore:
        # VARSAYIM: The limit is per process; with 2 uvicorn workers the effective
        # maximum is 2 x LLM_MAX_CONCURRENCY.
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.settings.LLM_MAX_CONCURRENCY)
        return self._semaphore

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()

    def _kwargs(self, instructions: str, llm_input: LLMInput, **extra: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": llm_input,
            # Do not keep prompts/answers on the provider side.
            "store": False,
        }
        if self.settings.LLM_MAX_OUTPUT_TOKENS:
            kwargs["max_output_tokens"] = self.settings.LLM_MAX_OUTPUT_TOKENS
        if self.settings.LLM_REASONING_EFFORT.strip():
            kwargs["reasoning"] = {"effort": self.settings.LLM_REASONING_EFFORT.strip()}
        kwargs.update(extra)
        return kwargs

    async def _finish(self, purpose: str, user: str | None, result: LLMResult) -> None:
        logger.info(
            "llm_call",
            extra={
                "purpose": purpose,
                "model": self.model,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "total_tokens": result.usage.total_tokens,
                "latency_ms": round(result.latency_ms, 1),
                "streamed": result.streamed,
                "structured_mode": result.structured_mode,
            },
        )
        if self._usage_recorder is not None:
            try:
                await self._usage_recorder(self.model, purpose, user, result.usage)
            except Exception:
                logger.exception("llm_usage_record_failed")

    async def _create_raw(self, kwargs: dict[str, Any]) -> LLMResult:
        """One non-streaming call. Raises raw SDK exceptions (callers decide on fallback)."""
        api = self._api()
        started = time.perf_counter()
        async with self._sem():
            response = await api.create(**kwargs)
        return LLMResult(
            text=getattr(response, "output_text", "") or "",
            usage=Usage.from_response(getattr(response, "usage", None)),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def complete(
        self, instructions: str, llm_input: LLMInput, *, purpose: str, user: str | None = None
    ) -> LLMResult:
        try:
            result = await self._create_raw(self._kwargs(instructions, llm_input))
        except openai.OpenAIError as exc:
            raise map_openai_error(exc) from exc
        await self._finish(purpose, user, result)
        return result

    async def generate_json(
        self,
        instructions: str,
        llm_input: LLMInput,
        spec: StructuredSpec,
        *,
        purpose: str,
        user: str | None = None,
        mode: str | None = None,
    ) -> tuple[BaseModel, LLMResult]:
        """Structured output. `mode` forces "json_schema" or "prompt" (no fallback)."""
        forced = mode or self.structured_mode
        current = forced or "json_schema"
        result: LLMResult | None = None
        call_instructions = instructions

        if current == "json_schema":
            text_format = {"format": {"type": "json_schema", "name": spec.name, "schema": spec.schema, "strict": True}}
            kwargs = self._kwargs(instructions, llm_input, text=text_format)
            try:
                result = await self._create_raw(kwargs)
            except openai.OpenAIError as exc:
                if forced or not _looks_unsupported(exc, _STRUCTURED_HINTS):
                    raise map_openai_error(exc) from exc
                logger.warning("llm_structured_output_unsupported_fallback_to_prompt")
                self.structured_mode = current = "prompt"

        if current == "prompt":
            call_instructions = prompts.json_only_instructions(instructions, spec.schema)
            kwargs = self._kwargs(call_instructions, llm_input)
            try:
                result = await self._create_raw(kwargs)
            except openai.OpenAIError as exc:
                raise map_openai_error(exc) from exc

        if result is None:  # unreachable: one of the branches above always runs
            raise RuntimeError(f"unknown structured mode: {current}")
        result.structured_mode = current
        try:
            parsed = parse_json_model(result.text, spec.model)
        except ValueError as first_error:
            logger.warning("llm_json_invalid_repairing", extra={"purpose": purpose, "error": str(first_error)[:300]})
            repair_input = [
                *_as_messages(llm_input),
                {"role": "assistant", "content": result.text[:8000]},
                {
                    "role": "user",
                    "content": prompts.JSON_REPAIR.format(error=str(first_error)[:1000], previous=result.text[:8000]),
                },
            ]
            try:
                repaired = await self._create_raw({**kwargs, "input": repair_input})
            except openai.OpenAIError as exc:
                raise map_openai_error(exc) from exc
            result = LLMResult(
                text=repaired.text,
                usage=result.usage + repaired.usage,
                latency_ms=result.latency_ms + repaired.latency_ms,
                structured_mode=current,
            )
            try:
                parsed = parse_json_model(result.text, spec.model)
            except ValueError as second_error:
                await self._finish(purpose, user, result)
                logger.error("llm_json_invalid_after_repair", extra={"error": str(second_error)[:300]})
                raise LLMError(
                    "llm_invalid_output",
                    "Yapay zeka servisinden beklenen biçimde cevap alınamadı. Lütfen tekrar deneyin.",
                    502,
                ) from second_error
        await self._finish(purpose, user, result)
        return parsed, result

    def stream_text(
        self,
        instructions: str,
        llm_input: LLMInput,
        *,
        purpose: str,
        user: str | None = None,
        allow_fallback: bool = True,
    ) -> TextStream:
        return TextStream(self, self._kwargs(instructions, llm_input), purpose, user, allow_fallback)


class TextStream:
    """Async iterator of text deltas. `result` is filled once iteration completes."""

    def __init__(
        self, client: LLMClient, kwargs: dict[str, Any], purpose: str, user: str | None, allow_fallback: bool
    ) -> None:
        self._client = client
        self._kwargs = kwargs
        self._purpose = purpose
        self._user = user
        self._allow_fallback = allow_fallback
        self.result: LLMResult | None = None

    def __aiter__(self) -> AsyncIterator[str]:
        return self._run()

    async def _fallback(self) -> AsyncIterator[str]:
        try:
            result = await self._client._create_raw(self._kwargs)
        except openai.OpenAIError as exc:
            raise map_openai_error(exc) from exc
        self.result = result
        await self._client._finish(self._purpose, self._user, result)
        if result.text:
            yield result.text

    async def _run(self) -> AsyncIterator[str]:
        client = self._client
        if client.streaming_supported is False and self._allow_fallback:
            async for chunk in self._fallback():
                yield chunk
            return

        api = client._api()
        started = time.perf_counter()
        parts: list[str] = []
        usage = Usage()
        use_fallback = False
        async with client._sem():
            try:
                stream = await api.create(**self._kwargs, stream=True)
            except openai.OpenAIError as exc:
                can_fallback = self._allow_fallback and client.streaming_supported is None
                if not (can_fallback and _looks_unsupported(exc, _STREAM_HINTS)):
                    raise map_openai_error(exc) from exc
                logger.warning("llm_streaming_unsupported_fallback")
                client.streaming_supported = False
                use_fallback = True
            else:
                try:
                    async for event in stream:
                        event_type = getattr(event, "type", "")
                        if event_type == "response.output_text.delta":
                            delta = getattr(event, "delta", "") or ""
                            if delta:
                                parts.append(delta)
                                yield delta
                        elif event_type in ("response.completed", "response.incomplete"):
                            usage = Usage.from_response(getattr(getattr(event, "response", None), "usage", None))
                        elif event_type in ("response.failed", "error"):
                            logger.warning("llm_stream_failed_event", extra={"event_type": event_type})
                            raise LLMError(
                                "llm_stream_failed",
                                "Yapay zeka servisi cevabı tamamlayamadı. Lütfen tekrar deneyin.",
                                502,
                            )
                except openai.OpenAIError as exc:
                    raise map_openai_error(exc) from exc
                finally:
                    close = getattr(stream, "close", None)
                    if close is not None:
                        await close()
                if client.streaming_supported is None:
                    client.streaming_supported = True

        if use_fallback:
            async for chunk in self._fallback():
                yield chunk
            return

        self.result = LLMResult(
            text="".join(parts), usage=usage, latency_ms=(time.perf_counter() - started) * 1000, streamed=True
        )
        await client._finish(self._purpose, self._user, self.result)
