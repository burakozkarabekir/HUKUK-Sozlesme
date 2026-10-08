from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import openai
from fastapi.testclient import TestClient

from app.db import connect
from app.ingest.pipeline import run_sync
from app.llm.client import LLMClient
from app.llm.usage import daily_usage, make_db_usage_recorder
from app.main import create_app
from tests import fakes
from tests.conftest import make_settings
from tests.pdfgen import write_sample_contracts

P = "/sozlesme"


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def make_client(tmp_path: Path, api: fakes.FakeResponses, *, sync: bool = True, embeddings=None, **overrides):
    contracts = tmp_path / "contracts"
    write_sample_contracts(contracts)
    settings = make_settings(tmp_path, LOCAL_CONTRACTS_DIR=contracts, **overrides)
    if sync:
        run_sync(settings)
    app = create_app(settings)
    app.state.llm = LLMClient(
        settings, responses_api=api, usage_recorder=make_db_usage_recorder(settings.db_path), embeddings_api=embeddings
    )
    return TestClient(app), settings


def test_rag_chat_streams_sources_and_answer(tmp_path: Path) -> None:
    api = fakes.FakeResponses(fakes.stream("Bildirim süresi ", "90 gündür [1]."))
    client, settings = make_client(tmp_path, api)
    with client:
        response = client.post(f"{P}/api/chat", json={"message": "ABC bayilik sözleşmesinde fesih bildirim süresi ne?"})
        assert response.status_code == 200
        assert response.headers["x-accel-buffering"] == "no"
        events = parse_sse(response.text)

    names = [e for e, _ in events]
    assert names[:2] == ["start", "sources"] and names[-1] == "done"
    sources = events[1][1]["sources"]
    assert sources[0]["clause_ref"] == "Madde 3" and sources[0]["contract_id"] == "local-abc-bayilik"
    assert sources[0]["n"] == 1 and sources[0]["page_start"] == 2 and "90 gün" in sources[0]["text"]
    assert "".join(d["text"] for e, d in events if e == "delta") == "Bildirim süresi 90 gündür [1]."

    call = api.calls[0]
    assert "YALNIZCA bu kaynaklardaki" in call["instructions"]
    final = call["input"][-1]["content"]
    assert final.startswith("<kaynaklar>") and '<sozlesme_metni kaynak="1"' in final and final.endswith("ne?")

    conn = connect(settings.db_path)
    assert daily_usage(conn)[0]["total_tokens"] == 10
    audit = conn.execute(
        "SELECT action, target_ids_json, details_json FROM audit_log WHERE action='chat.ask'"
    ).fetchone()
    assert "local-abc-bayilik" in json.loads(audit["target_ids_json"])
    details = json.loads(audit["details_json"])
    assert details["question"].startswith("ABC") and "text" not in json.dumps(details)


def test_no_sources_answers_not_found_without_llm(tmp_path: Path) -> None:
    api = fakes.FakeResponses()  # any LLM call would fail the test
    client, _ = make_client(tmp_path, api, CHAT_REWRITE_QUERY=False)
    with client:
        events = parse_sse(client.post(f"{P}/api/chat", json={"message": "uzay mekiği kiralama bedeli"}).text)
    assert events[1] == ("sources", {"sources": []})
    assert events[2] == ("delta", {"text": "Sözleşmelerde bu bilgi bulunamadı."})
    assert api.calls == []


def test_follow_up_question_is_rewritten(tmp_path: Path) -> None:
    api = fakes.FakeResponses(fakes.response("XYZ servis sözleşmesi cezai şart tutarı"), fakes.stream("100.000 TL [1]"))
    client, _ = make_client(tmp_path, api)
    history = [
        {"role": "user", "content": "XYZ sözleşmesi ne zaman bitiyor?"},
        {"role": "assistant", "content": "2027"},
    ]
    with client:
        events = parse_sse(
            client.post(f"{P}/api/chat", json={"message": "Peki cezai şart ne kadar?", "history": history}).text
        )
    assert events[0][1]["search_query"] == "XYZ servis sözleşmesi cezai şart tutarı"
    assert events[1][1]["sources"][0]["contract_id"] == "local-xyz-servis"
    assert api.calls[1]["input"][0] == {"role": "user", "content": "XYZ sözleşmesi ne zaman bitiyor?"}


def test_scope_to_one_contract(tmp_path: Path) -> None:
    api = fakes.FakeResponses(fakes.stream("cevap"))
    client, _ = make_client(tmp_path, api, CHAT_REWRITE_QUERY=False)
    with client:
        events = parse_sse(
            client.post(f"{P}/api/chat", json={"message": "taraflar kim?", "contract_id": "local-xyz-servis"}).text
        )
        missing = client.post(f"{P}/api/chat", json={"message": "x", "contract_id": "yok"})
    assert {s["contract_id"] for s in events[1][1]["sources"]} == {"local-xyz-servis"}
    assert missing.status_code == 404


def test_vector_search_used_when_enabled(tmp_path: Path) -> None:
    class FakeEmbeddings:
        def __init__(self) -> None:
            self.calls = []

        async def create(self, model, input):
            self.calls.append((model, input))
            return SimpleNamespace(
                data=[SimpleNamespace(index=0, embedding=[1.0, 0.0])], usage=SimpleNamespace(total_tokens=3)
            )

    from app.services.embeddings import store_vectors

    embeddings = FakeEmbeddings()
    api = fakes.FakeResponses(fakes.stream("ok"))
    client, settings = make_client(
        tmp_path, api, embeddings=embeddings, EMBEDDING_MODEL="emb", CHAT_REWRITE_QUERY=False, sync=False
    )
    run_sync(settings, embedder=lambda texts: [[0.0, 1.0] for _ in texts])
    conn = connect(settings.db_path)
    target = conn.execute("SELECT id FROM chunks WHERE clause_ref = 'Madde 4'").fetchone()[0]
    store_vectors(conn, "emb", [target], [[1.0, 0.0]])  # the only vector close to the query
    with client:
        events = parse_sse(client.post(f"{P}/api/chat", json={"message": "zzzz qqqq"}).text)
    assert embeddings.calls[0] == ("emb", ["zzzz qqqq"])
    assert events[1][1]["sources"][0]["chunk_id"] == target


def test_chat_error_during_stream_becomes_error_event(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path, fakes.FakeResponses(fakes.timeout_error()), CHAT_REWRITE_QUERY=False)
    with client:
        events = parse_sse(client.post(f"{P}/api/chat", json={"message": "fesih"}).text)
    assert events[-1] == (
        "error",
        {"code": "llm_timeout", "message": "Yapay zeka servisi zamanında yanıt vermedi. Lütfen tekrar deneyin."},
    )


def test_chat_streaming_fallback_still_returns_answer(tmp_path: Path) -> None:
    api = fakes.FakeResponses(
        fakes.api_error(openai.BadRequestError, 400, "stream is not supported"), fakes.response("Tek parça")
    )
    client, _ = make_client(tmp_path, api, CHAT_REWRITE_QUERY=False)
    with client:
        events = parse_sse(client.post(f"{P}/api/chat", json={"message": "fesih"}).text)
    assert [e for e, _ in events] == ["start", "sources", "delta", "done"]


def test_chat_validation_errors_are_json(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path, fakes.FakeResponses(), sync=False, CHAT_MAX_QUESTION_CHARS=10)
    with client:
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
