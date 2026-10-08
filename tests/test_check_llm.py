from __future__ import annotations

import asyncio
from pathlib import Path

import openai

import cli
from app.llm.client import LLMClient
from tests import fakes
from tests.conftest import make_settings

GOOD_JSON = '{"city": "İstanbul", "plate_code": 34}'


def run_check(tmp_path: Path, api: fakes.FakeResponses) -> int:
    settings = make_settings(tmp_path, AZURE_OPENAI_API_KEY="abcd-secret-1234")
    return asyncio.run(cli.run_check_llm(settings, LLMClient(settings, responses_api=api)))


def test_check_llm_all_supported(tmp_path: Path, capsys) -> None:
    api = fakes.FakeResponses(
        fakes.response("TAMAM"),
        fakes.stream("1 ", "2 ", "3"),
        fakes.response(GOOD_JSON),
        fakes.response(GOOD_JSON),
    )
    assert run_check(tmp_path, api) == 0
    out = capsys.readouterr().out
    assert "LLM_STREAMING=on" in out and "LLM_STRUCTURED_MODE=json_schema" in out
    assert "abcd-secret-1234" not in out  # key is masked


def test_check_llm_recommends_fallbacks(tmp_path: Path, capsys) -> None:
    api = fakes.FakeResponses(
        fakes.response("TAMAM"),
        fakes.api_error(openai.BadRequestError, 400, "stream is not supported"),
        fakes.api_error(openai.BadRequestError, 400, "json_schema is not supported"),
        fakes.response(GOOD_JSON),
    )
    assert run_check(tmp_path, api) == 0
    out = capsys.readouterr().out
    assert "LLM_STREAMING=off" in out and "LLM_STRUCTURED_MODE=prompt" in out


def test_check_llm_stops_when_basic_call_fails(tmp_path: Path, capsys) -> None:
    api = fakes.FakeResponses(fakes.api_error(openai.AuthenticationError, 401, "bad key"))
    assert run_check(tmp_path, api) == 1
    out = capsys.readouterr().out
    assert "API anahtarını kontrol edin" in out and "atlandı" in out


def test_check_llm_not_configured(tmp_path: Path, capsys) -> None:
    assert asyncio.run(cli.run_check_llm(make_settings(tmp_path))) == 1
    assert "yapılandırılmamış" in capsys.readouterr().out
