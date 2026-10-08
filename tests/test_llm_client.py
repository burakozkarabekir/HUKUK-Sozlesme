from __future__ import annotations

import asyncio
from pathlib import Path

import openai
import pytest

from app.llm.client import LLMClient, LLMError, Usage, map_openai_error, parse_json_model
from app.llm.schemas import CITY_PROBE, CityProbe
from tests import fakes
from tests.conftest import make_settings

UNSUPPORTED_SCHEMA = "Invalid parameter: 'text.format' of type 'json_schema' is not supported with this model."
UNSUPPORTED_STREAM = "The 'stream' parameter is not supported for this deployment."


def run(coro):
    return asyncio.run(coro)


def make_client(tmp_path: Path, api: fakes.FakeResponses, recorded: list | None = None, **overrides) -> LLMClient:
    async def recorder(model: str, purpose: str, user: str | None, usage: Usage) -> None:
        if recorded is not None:
            recorded.append((model, purpose, user, usage))

    return LLMClient(make_settings(tmp_path, **overrides), responses_api=api, usage_recorder=recorder)


async def collect(stream) -> list[str]:
    return [chunk async for chunk in stream]


# --- complete ---


def test_complete_sends_expected_kwargs_and_records_usage(tmp_path: Path) -> None:
    api = fakes.FakeResponses(fakes.response("TAMAM", 12, 3))
    recorded: list = []
    client = make_client(tmp_path, api, recorded, LLM_MAX_OUTPUT_TOKENS=500, LLM_REASONING_EFFORT="low")
    result = run(client.complete("talimat", "soru", purpose="test", user="ali"))
    assert result.text == "TAMAM"
    assert result.usage == Usage(12, 3, 15)
    call = api.calls[0]
    assert call["model"] == "gpt-5.6-sol" and call["instructions"] == "talimat" and call["input"] == "soru"
    assert call["store"] is False
    assert call["max_output_tokens"] == 500 and call["reasoning"] == {"effort": "low"}
    assert recorded == [("gpt-5.6-sol", "test", "ali", Usage(12, 3, 15))]


def test_optional_params_not_sent_by_default(tmp_path: Path) -> None:
    api = fakes.FakeResponses(fakes.response("x"))
    run(make_client(tmp_path, api).complete("i", "q", purpose="t"))
    assert "max_output_tokens" not in api.calls[0] and "reasoning" not in api.calls[0]


@pytest.mark.parametrize(
    ("error", "code", "status"),
    [
        (fakes.timeout_error(), "llm_timeout", 504),
        (fakes.api_error(openai.AuthenticationError, 401, "bad key"), "llm_auth", 502),
        (fakes.api_error(openai.PermissionDeniedError, 403, "denied"), "llm_forbidden", 502),
        (fakes.api_error(openai.NotFoundError, 404, "no deployment"), "llm_not_found", 502),
        (fakes.api_error(openai.RateLimitError, 429, "slow down"), "llm_rate_limited", 503),
        (fakes.api_error(openai.InternalServerError, 500, "boom"), "llm_server_error", 502),
        (
            fakes.api_error(openai.BadRequestError, 400, "filtered", code="content_filter"),
            "llm_content_filter",
            422,
        ),
        (fakes.api_error(openai.BadRequestError, 400, "bad"), "llm_bad_request", 502),
    ],
)
def test_errors_are_mapped_to_turkish_app_errors(tmp_path: Path, error, code: str, status: int) -> None:
    client = make_client(tmp_path, fakes.FakeResponses(error))
    with pytest.raises(LLMError) as info:
        run(client.complete("i", "q", purpose="t"))
    assert info.value.code == code and info.value.status_code == status
    assert not info.value.message.isascii()  # Turkish user-facing text


def test_not_configured(tmp_path: Path) -> None:
    client = LLMClient(make_settings(tmp_path))
    with pytest.raises(LLMError) as info:
        client.ensure_configured()
    assert info.value.code == "llm_not_configured" and info.value.status_code == 503


def test_connection_error_message_mentions_proxy() -> None:
    error = openai.APIConnectionError(request=fakes._REQUEST)
    assert "proxy" in map_openai_error(error).message


# --- structured output ---


def test_json_schema_mode(tmp_path: Path) -> None:
    api = fakes.FakeResponses(fakes.response('{"city": "İstanbul", "plate_code": 34}'))
    parsed, result = run(make_client(tmp_path, api).generate_json("i", "q", CITY_PROBE, purpose="t"))
    assert parsed == CityProbe(city="İstanbul", plate_code=34)
    assert result.structured_mode == "json_schema"
    fmt = api.calls[0]["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True and fmt["name"] == "city_probe"


def test_unsupported_json_schema_falls_back_to_prompt_and_is_remembered(tmp_path: Path) -> None:
    api = fakes.FakeResponses(
        fakes.api_error(openai.BadRequestError, 400, UNSUPPORTED_SCHEMA),
        fakes.response('```json\n{"city": "Ankara", "plate_code": 6}\n```'),
        fakes.response('{"city": "İzmir", "plate_code": 35}'),
    )
    client = make_client(tmp_path, api)
    parsed, result = run(client.generate_json("talimat", "q", CITY_PROBE, purpose="t"))
    assert parsed.plate_code == 6 and result.structured_mode == "prompt"
    assert "text" not in api.calls[1]
    assert "JSON şeması" in api.calls[1]["instructions"]
    # Next call goes straight to prompt mode.
    run(client.generate_json("talimat", "q", CITY_PROBE, purpose="t"))
    assert len(api.calls) == 3 and "text" not in api.calls[2]


def test_other_bad_request_does_not_trigger_fallback(tmp_path: Path) -> None:
    api = fakes.FakeResponses(fakes.api_error(openai.BadRequestError, 400, "Invalid value for 'input'."))
    with pytest.raises(LLMError):
        run(make_client(tmp_path, api).generate_json("i", "q", CITY_PROBE, purpose="t"))
    assert len(api.calls) == 1


def test_forced_mode_never_falls_back(tmp_path: Path) -> None:
    api = fakes.FakeResponses(fakes.api_error(openai.BadRequestError, 400, UNSUPPORTED_SCHEMA))
    client = make_client(tmp_path, api)
    with pytest.raises(LLMError):
        run(client.generate_json("i", "q", CITY_PROBE, purpose="t", mode="json_schema"))


def test_invalid_json_is_repaired_once(tmp_path: Path) -> None:
    recorded: list = []
    api = fakes.FakeResponses(
        fakes.response('{"city": "Bursa"}', 10, 5),  # missing plate_code
        fakes.response('{"city": "Bursa", "plate_code": 16}', 20, 6),
    )
    parsed, result = run(
        make_client(tmp_path, api, recorded, LLM_STRUCTURED_MODE="prompt").generate_json(
            "i", "q", CITY_PROBE, purpose="t"
        )
    )
    assert parsed.plate_code == 16
    repair_input = api.calls[1]["input"]
    assert repair_input[0] == {"role": "user", "content": "q"}
    assert repair_input[1]["role"] == "assistant" and "plate_code" in repair_input[2]["content"]
    assert result.usage == Usage(30, 11, 41)
    assert len(recorded) == 1  # one usage record for the whole logical call


def test_invalid_json_after_repair_raises(tmp_path: Path) -> None:
    api = fakes.FakeResponses(fakes.response("üzgünüm"), fakes.response("{bozuk"))
    with pytest.raises(LLMError) as info:
        run(make_client(tmp_path, api).generate_json("i", "q", CITY_PROBE, purpose="t"))
    assert info.value.code == "llm_invalid_output"


def test_parse_json_model_tolerates_chatter() -> None:
    assert parse_json_model('Sonuç: {"city": "Van", "plate_code": 65} umarım yardımcı olur', CityProbe).city == "Van"
    with pytest.raises(ValueError):
        parse_json_model("JSON yok", CityProbe)


# --- streaming ---


def test_streaming_yields_deltas_and_usage(tmp_path: Path) -> None:
    recorded: list = []
    fake_stream = fakes.stream("Mer", "ha", "ba")
    client = make_client(tmp_path, fakes.FakeResponses(fake_stream), recorded)
    stream = client.stream_text("i", "q", purpose="chat", user="ali")
    assert run(collect(stream)) == ["Mer", "ha", "ba"]
    assert stream.result.text == "Merhaba" and stream.result.streamed
    assert stream.result.usage == Usage(7, 3, 10)
    assert recorded[0][1:3] == ("chat", "ali")
    assert fake_stream.closed and client.streaming_supported is True


def test_streaming_unsupported_falls_back_to_single_response(tmp_path: Path) -> None:
    api = fakes.FakeResponses(
        fakes.api_error(openai.BadRequestError, 400, UNSUPPORTED_STREAM),
        fakes.response("Tam cevap"),
        fakes.response("İkinci"),
    )
    client = make_client(tmp_path, api)
    assert run(collect(client.stream_text("i", "q", purpose="chat"))) == ["Tam cevap"]
    assert client.streaming_supported is False
    # Remembered: the next call does not try streaming again.
    assert run(collect(client.stream_text("i", "q", purpose="chat"))) == ["İkinci"]
    assert [c.get("stream") for c in api.calls] == [True, None, None]


def test_streaming_off_setting(tmp_path: Path) -> None:
    api = fakes.FakeResponses(fakes.response("x"))
    client = make_client(tmp_path, api, LLM_STREAMING="off")
    assert run(collect(client.stream_text("i", "q", purpose="chat"))) == ["x"]
    assert "stream" not in api.calls[0]


def test_streaming_error_mid_stream_is_mapped(tmp_path: Path) -> None:
    broken = fakes.stream("Kısmi")
    broken.fail_with = fakes.api_error(openai.InternalServerError, 500, "boom")
    client = make_client(tmp_path, fakes.FakeResponses(broken))
    chunks: list[str] = []

    async def consume() -> None:
        async for chunk in client.stream_text("i", "q", purpose="chat"):
            chunks.append(chunk)

    with pytest.raises(LLMError) as info:
        run(consume())
    assert chunks == ["Kısmi"] and info.value.code == "llm_server_error" and broken.closed


def test_concurrency_is_limited(tmp_path: Path) -> None:
    active = 0
    peak = 0

    class SlowApi:
        async def create(self, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return fakes.response("ok")

    client = LLMClient(make_settings(tmp_path, LLM_MAX_CONCURRENCY=2), responses_api=SlowApi())

    async def many() -> None:
        await asyncio.gather(*(client.complete("i", "q", purpose="t") for _ in range(6)))

    run(many())
    assert peak == 2
