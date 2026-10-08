from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import connect, migrate
from app.ingest.pipeline import run_sync
from app.main import create_app
from app.services.contracts import ContractFilters
from app.services.search import (
    Term,
    fts_expression,
    highlight_segments,
    keyword_search,
    parse_query,
    retrieve,
    rrf_fuse,
)
from tests.conftest import make_settings
from tests.pdfgen import write_sample_contracts

P = "/sozlesme"


@pytest.fixture
def synced(tmp_path: Path):
    contracts = tmp_path / "contracts"
    write_sample_contracts(contracts)
    settings = make_settings(tmp_path, LOCAL_CONTRACTS_DIR=contracts)
    run_sync(settings)
    conn = connect(settings.db_path)
    migrate(conn)
    return settings, conn


def test_parse_query_phrases_prefix_and_stopwords() -> None:
    terms = parse_query('"Haklı Nedenle Fesih" SÖZLEŞME ve 90')
    assert terms[0] == Term(("hakli", "nedenle", "fesih"), phrase=True)
    assert [t.tokens for t in terms[1:]] == [("sozlesme",), ("ve",), ("90",)]
    assert terms[1].prefix and not terms[3].prefix
    assert fts_expression(terms, "and") == '"hakli nedenle fesih" AND "sozlesme"* AND "ve" AND "90"'
    assert [t.tokens for t in parse_query("fesih süresi nedir ve ne kadar", drop_stopwords=True)] == [
        ("fesih",),
        ("suresi",),
    ]
    assert parse_query('"" ...') == []


def test_highlight_on_original_text() -> None:
    text = "Taraflardan her biri 90 gün önceden yazılı bildirimde bulunarak sözleşmeyi FESHEDEBİLİR."
    segments = highlight_segments(text, parse_query("bildirim feshedebilir"))
    hits = [s["text"] for s in segments if s["hit"]]
    assert hits == ["bildirimde", "FESHEDEBİLİR"]
    assert "".join(s["text"] for s in segments) == text


def test_highlight_window_and_ellipsis() -> None:
    text = "başlangıç " * 60 + "gizlilik yükümlülüğü " + "son " * 100
    segments = highlight_segments(text, parse_query('"gizlilik yükümlülüğü"'))
    assert segments[0] == {"text": "…", "hit": False} and segments[-1] == {"text": "…", "hit": False}
    assert [s["text"] for s in segments if s["hit"]] == ["gizlilik yükümlülüğü"]


def test_keyword_search_and_or_and_turkish_insensitive(synced) -> None:
    _, conn = synced
    result = keyword_search(conn, "FESİH bildirim")
    assert result["total"] >= 1 and result["items"][0]["clause_ref"] == "Madde 3"
    assert result["items"][0]["contract_no"] == "ABC_Bayilik" and result["items"][0]["page_start"] == 2
    assert any(s["hit"] for s in result["items"][0]["snippet"])
    assert keyword_search(conn, "sozlesme")["total"] >= 3  # ASCII query finds "sözleşme"
    both = keyword_search(conn, "gizli cezai", mode="and")["total"]
    either = keyword_search(conn, "gizli cezai", mode="or")["total"]
    assert both == 0 and either >= 2
    assert keyword_search(conn, '"yazılı bildirimde bulunarak"')["total"] == 1
    assert keyword_search(conn, '"bildirimde yazılı"')["total"] == 0


def test_keyword_search_filters(synced) -> None:
    _, conn = synced
    assert keyword_search(conn, "madde", filters=ContractFilters(party="xyz"))["total"] >= 1
    assert keyword_search(conn, "fesih", filters=ContractFilters(party="xyz"))["total"] == 0
    assert (
        keyword_search(conn, "cezai", filters=ContractFilters(contract_type="Yetkili Servis Sözleşmesi"))["total"] == 1
    )


def test_retrieve_boosts_named_company(synced) -> None:
    _, conn = synced
    chunks = retrieve(conn, "XYZ sözleşmesinde süre ne kadar?", k=3)
    assert chunks[0]["contract_id"] == "local-xyz-servis"
    assert retrieve(conn, "ve ile bu", k=3) == []


def test_rrf() -> None:
    assert rrf_fuse([[1, 2, 3], [3, 1]]) == [1, 3, 2]
    assert rrf_fuse([[1, 2], [2]], weights=[1.0, 5.0])[0] == 2


def test_search_api(synced) -> None:
    settings, _ = synced
    with TestClient(create_app(settings)) as client:
        body = client.get(f"{P}/api/search", params={"q": "gizlilik", "mode": "or"}).json()
        assert body["total"] >= 1 and body["terms"] == ["gizlilik"]
        item = body["items"][0]
        assert {"chunk_id", "contract_no", "clause_ref", "page_start", "snippet", "file_id"} <= set(item)
        assert client.get(f"{P}/api/search", params={"q": "..."}).status_code == 400
        assert client.get(f"{P}/api/search").status_code == 422
        assert client.get(f"{P}/api/search", params={"q": "fesih", "party": "xyz"}).json()["total"] == 0
