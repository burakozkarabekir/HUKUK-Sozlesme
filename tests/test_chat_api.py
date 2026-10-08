from __future__ import annotations

import json
from pathlib import Path

import openai
from fastapi.testclient import TestClient

from app.db import connect
from app.llm.client import LLMClient
from app.llm.usage import daily_usage, make_db_usage_recorder
from app.main import create_app
from tests import fakes
from tests.conftest import make_settings

P = "/sozlesme"


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def make_client(tmp_path: Path, api: fakes.FakeResponses, **overrides) -> TestClient:
    settings = make_settings(tmp_path, **overrides)
    app = create_app(settings)
    app.state.llm = LLMClient(settings, responses_api=api, usage_recorder=make_db_usage_recorder(settings.db_path))
    return TestClient(app)


def test_chat_streams_sse_and_records_usage_and_audit(tmp_path: Path) -> None:
    api = fakes.FakeResponses(fakes.stream("Fesih ", "süresi ", "bulunamadı."))
    with make_client(tmp_path, api) as client:
        response = client.post(
            f"{P}/api/chat",
            json={"message": "  Fesih süresi ne?  ", "history": [{"role": "user", "content": "önceki"}]},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        events = parse_sse(response.text)

    assert [e for e, _ in events] == ["start", "delta", "delta", "delta", "done"]
    assert "".join(d["text"] for e, d in events if e == "delta") == "Fesih süresi bulunamadı."
    assert events[-1][1]["usage"]["total_tokens"] == 10
    assert api.calls[0]["input"][-1] == {"role": "user", "content": "Fesih süresi ne?"}
    assert "sözleşme arşivine erişimin YOK" in api.calls[0]["instructions"]

    conn = connect(tmp_path / "data" / "app.db")
    assert daily_usage(conn)[0]["total_tokens"] == 10
    audit = conn.execute("SELECT action, user_id, details_json FROM audit_log").fetchone()
    assert audit["action"] == "chat.ask" and audit["user_id"] == "gelistirici"
    assert json.loads(audit["details_json"]) == {"question": "Fesih süresi ne?"}  # question only, no answer


def test_chat_error_during_stream_becomes_error_event(tmp_path: Path) -> None:
    api = fakes.FakeResponses(fakes.timeout_error())
    with make_client(tmp_path, api) as client:
        events = parse_sse(client.post(f"{P}/api/chat", json={"message": "soru"}).text)
    assert events[-1] == (
        "error",
        {
            "code": "llm_timeout",
            "message": "Yapay zeka servisi zamanında yanıt vermedi. Lütfen tekrar deneyin.",
        },
    )


def test_chat_streaming_fallback_still_returns_answer(tmp_path: Path) -> None:
    api = fakes.FakeResponses(
        fakes.api_error(openai.BadRequestError, 400, "stream is not supported"), fakes.response("Tek parça")
    )
    with make_client(tmp_path, api) as client:
        events = parse_sse(client.post(f"{P}/api/chat", json={"message": "soru"}).text)
    assert [e for e, _ in events] == ["start", "delta", "done"]


def test_chat_validation_errors_are_json(tmp_path: Path) -> None:
    with make_client(tmp_path, fakes.FakeResponses(), CHAT_MAX_QUESTION_CHARS=10) as client:
        too_long = client.post(f"{P}/api/chat", json={"message": "x" * 11})
        assert too_long.status_code == 413 and too_long.json()["error"]["code"] == "question_too_long"
        blank = client.post(f"{P}/api/chat", json={"message": "   "})
        assert blank.status_code == 400 and blank.json()["error"]["code"] == "empty_question"
        bad_role = client.post(f"{P}/api/chat", json={"message": "x", "history": [{"role": "system", "content": "y"}]})
        assert bad_role.status_code == 422 and bad_role.json()["error"]["code"] == "validation_error"


def test_chat_without_llm_configuration_returns_503(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        response = client.post(f"{P}/api/chat", json={"message": "soru"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_not_configured"
