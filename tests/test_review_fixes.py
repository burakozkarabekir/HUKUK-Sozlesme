"""Regression tests for issues found in the independent code review."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import respx
from fastapi.testclient import TestClient

from app.db import connect, migrate
from app.ingest.pipeline import run_sync
from app.llm.client import LLMClient
from app.main import create_app
from app.services.impact import RunRequest, create_job, run_job
from tests import fakes
from tests.conftest import make_settings
from tests.pdfgen import make_pdf, write_sample_contracts
from tests.test_turkuaz import BASE, PDF_B64, record, settings_for

P = "/sozlesme"


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


# --- 1. full sync must not wipe the archive on a bad/partial listing ---


def _mock_documents(mock) -> None:
    mock.get(url__regex=rf"{BASE}/contracts/C\d+/documents").respond(
        json={"documents": [{"documentId": "d1", "fileName": "a.pdf", "contentBase64": PDF_B64}]}
    )


def _synced_turkuaz(tmp_path: Path, n: int = 4):
    settings = settings_for(tmp_path)
    with respx.mock() as mock:
        mock.get(f"{BASE}/contracts").respond(json={"items": [record(i) for i in range(1, n + 1)]})
        _mock_documents(mock)
        assert run_sync(settings).new == n
    return settings


def test_error_body_is_not_an_empty_listing(tmp_path: Path) -> None:
    settings = _synced_turkuaz(tmp_path)
    with respx.mock() as mock:
        mock.get(f"{BASE}/contracts").respond(json={"error": "maintenance"})
        stats = run_sync(settings, full=True)
    assert stats.status == "failed" and stats.deleted == 0
    assert "alanı yok" in stats.errors[0]["message"]
    assert count(connect(settings.db_path), "contracts") == 4


def test_mass_deletion_is_refused(tmp_path: Path) -> None:
    settings = _synced_turkuaz(tmp_path)
    with respx.mock() as mock:
        mock.get(f"{BASE}/contracts").respond(json={"items": [record(1)]})
        _mock_documents(mock)
        stats = run_sync(settings, full=True)
    assert stats.status == "partial" and stats.deleted == 0 and "güvenlik nedeniyle" in stats.errors[-1]["message"]
    assert count(connect(settings.db_path), "contracts") == 4


def test_skipped_records_make_listing_incomplete(tmp_path: Path) -> None:
    settings = _synced_turkuaz(tmp_path)
    with respx.mock() as mock:
        mock.get(f"{BASE}/contracts").respond(json={"items": [record(1), record(2), record(3), {"name": "id yok"}]})
        _mock_documents(mock)
        stats = run_sync(settings, full=True)
    assert stats.status == "partial" and stats.deleted == 0
    assert any("atlandı" in e["message"] for e in stats.errors)


def test_full_sync_still_deletes_a_few_missing(tmp_path: Path) -> None:
    settings = _synced_turkuaz(tmp_path)
    with respx.mock() as mock:
        mock.get(f"{BASE}/contracts").respond(json={"items": [record(1), record(2), record(3)]})
        _mock_documents(mock)
        stats = run_sync(settings, full=True)
    assert (stats.status, stats.deleted) == ("success", 1)


# --- 2. credentials never leave the Turkuaz origin ---


def test_download_url_on_other_host_gets_no_credentials(tmp_path: Path) -> None:
    from app.sources.turkuaz_source import TurkuazSource

    pdf = make_pdf(["x"])
    with respx.mock() as mock:
        mock.get(f"{BASE}/contracts/C1/documents").respond(
            json={"documents": [{"documentId": "d1", "fileName": "a.pdf", "downloadUrl": "https://evil.example/f.pdf"}]}
        )
        evil = mock.get("https://evil.example/f.pdf").respond(content=pdf)
        TurkuazSource(settings_for(tmp_path, "bearer")).get_contract_document("C1")
    headers = evil.calls[0].request.headers
    assert "authorization" not in headers and "x-api-key" not in headers


def test_redirect_to_other_host_drops_custom_api_key_header(tmp_path: Path) -> None:
    from app.sources.turkuaz_source import TurkuazSource

    with respx.mock() as mock:
        mock.get(f"{BASE}/contracts").respond(302, headers={"Location": "https://evil.example/steal"})
        evil = mock.get("https://evil.example/steal").respond(json={"items": []})
        same = mock.get(f"{BASE}/files/1").respond(302, headers={"Location": "/api/files/2"})
        target = mock.get(f"{BASE}/files/2").respond(content=b"ok")
        source = TurkuazSource(settings_for(tmp_path))
        source.list_contracts()
        source.request("GET", f"{BASE}/files/1")
    assert "x-api-key" not in evil.calls[0].request.headers
    assert same.called and target.calls[0].request.headers["x-api-key"] == "key-123456"


# --- 3. a crashing evaluation does not leave other tasks running ---


def test_unexpected_error_in_one_contract_is_isolated(tmp_path: Path, monkeypatch) -> None:
    import app.services.impact as impact

    contracts = tmp_path / "contracts"
    write_sample_contracts(contracts)
    settings = make_settings(tmp_path, LOCAL_CONTRACTS_DIR=contracts)
    run_sync(settings)
    conn = connect(settings.db_path)
    migrate(conn)
    job_id = create_job(conn, RunRequest(text="mevzuat", keywords=["fesih", "cezai"]), "ali")
    calls: list[str] = []

    async def fake_evaluate(llm, settings, ctx, contract, chunks):
        calls.append(contract["contract_id"])
        if contract["contract_id"] == "local-abc-bayilik":
            raise KeyError("boom")
        await asyncio.sleep(0.05)
        return {"contract_id": contract["contract_id"], "impact_level": "ETKILENMEZ", "affected_clauses": []}

    monkeypatch.setattr(impact, "evaluate_contract", fake_evaluate)
    status = asyncio.run(run_job(job_id, LLMClient(settings, responses_api=fakes.FakeResponses()), settings))
    assert status == "completed"
    levels = dict(conn.execute("SELECT contract_id, impact_level FROM analysis_results").fetchall())
    assert levels == {"local-abc-bayilik": "DEGERLENDIRILEMEDI", "local-xyz-servis": "ETKILENMEZ"}


def test_job_tasks_are_cancelled_when_run_job_fails(tmp_path: Path, monkeypatch) -> None:
    import app.services.impact as impact

    contracts = tmp_path / "contracts"
    write_sample_contracts(contracts)
    settings = make_settings(tmp_path, LOCAL_CONTRACTS_DIR=contracts)
    run_sync(settings)
    conn = connect(settings.db_path)
    migrate(conn)
    job_id = create_job(conn, RunRequest(text="mevzuat", keywords=["fesih", "cezai"]), "ali")
    finished: list[str] = []

    async def slow_evaluate(llm, settings, ctx, contract, chunks):
        await asyncio.sleep(0.3)
        finished.append(contract["contract_id"])
        return {"contract_id": contract["contract_id"], "impact_level": "ETKILENMEZ", "affected_clauses": []}

    monkeypatch.setattr(impact, "evaluate_contract", slow_evaluate)
    monkeypatch.setattr(impact, "_cancel_requested", lambda conn, job_id: (_ for _ in ()).throw(RuntimeError("db")))

    async def scenario() -> str:
        status = await run_job(job_id, LLMClient(settings, responses_api=fakes.FakeResponses()), settings)
        await asyncio.sleep(0.5)  # anything still running would finish now
        return status

    assert asyncio.run(scenario()) == "failed"
    assert finished == []


# --- 4. invalid filter dates are rejected up front ---


def test_invalid_filter_date_is_422(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))
    app.state.llm = LLMClient(make_settings(tmp_path), responses_api=fakes.FakeResponses())
    with TestClient(app) as client:
        bad = client.post(
            f"{P}/api/impact/run", json={"text": "x", "keywords": ["a"], "filters": {"date_from": "31.12.2024"}}
        )
        assert bad.status_code == 422
        ok = client.post(
            f"{P}/api/impact/run",
            json={"text": "x", "keywords": ["a"], "filters": {"date_from": "", "date_to": "2025-01-01"}},
        )
        assert ok.status_code == 202


# --- 5. huge numbers are validation errors, not 500s ---


def test_huge_page_and_ids(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        for url in (
            "/api/contracts?page=99999999999999999999",
            "/api/search?q=a&page=99999999999999999999",
            "/api/chunks/99999999999999999999",
        ):
            assert client.get(P + url).status_code == 422, url


# --- 6. chunk ids are never reused after a re-sync ---


def test_chunk_ids_not_reused(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    pdf = contracts / "Tek.pdf"
    pdf.write_bytes(make_pdf(["MADDE 1 - A\nbir\nMADDE 2 - B\niki"]))
    settings = make_settings(tmp_path, LOCAL_CONTRACTS_DIR=contracts)
    run_sync(settings)
    conn = connect(settings.db_path)
    old_ids = {r[0] for r in conn.execute("SELECT id FROM chunks")}
    pdf.write_bytes(make_pdf(["MADDE 1 - YENİ\nüç\nMADDE 2 - YENİ\ndört"]))
    future = time.time() + 5
    import os

    os.utime(pdf, (future, future))
    assert run_sync(settings).updated == 1
    new_ids = {r[0] for r in conn.execute("SELECT id FROM chunks")}
    assert new_ids and not (old_ids & new_ids)


# --- 7. vectors missing after a failed embedding are filled by the next sync ---


def test_missing_embeddings_are_backfilled(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts"
    write_sample_contracts(contracts)
    settings = make_settings(tmp_path, LOCAL_CONTRACTS_DIR=contracts, EMBEDDING_MODEL="emb")

    def failing(texts):
        raise RuntimeError("embedding service down")

    first = run_sync(settings, embedder=failing)
    assert first.status == "partial"
    conn = connect(settings.db_path)
    assert count(conn, "embeddings") == 0
    second = run_sync(settings, embedder=lambda texts: [[1.0, 0.0] for _ in texts])
    assert second.status == "success" and count(conn, "embeddings") == count(conn, "chunks")


# --- 8. bcrypt does not block the event loop ---


def test_basic_auth_runs_in_thread(tmp_path: Path, monkeypatch) -> None:
    import threading

    import bcrypt

    import app.auth.basic as basic_module

    main_thread = threading.get_ident()
    threads: list[int] = []
    original = bcrypt.checkpw

    def spy(password, hashed):
        threads.append(threading.get_ident())
        return original(password, hashed)

    monkeypatch.setattr(basic_module.bcrypt, "checkpw", spy)
    hashed = bcrypt.hashpw(b"sifre-12345", bcrypt.gensalt(rounds=4)).decode()
    settings = make_settings(tmp_path, AUTH_MODE="basic", BASIC_AUTH_USERS=f"ali:{hashed}")
    with TestClient(create_app(settings)) as client:
        assert client.get(f"{P}/api/me", auth=("ali", "sifre-12345")).status_code == 200
    assert threads and all(t != main_thread for t in threads)
