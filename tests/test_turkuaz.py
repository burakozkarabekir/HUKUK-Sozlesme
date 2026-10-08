from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
import respx

import cli
from app.sources import turkuaz_mapping as mapping
from app.sources.base import SourceError
from app.sources.turkuaz_source import TurkuazSource, _OAuthClientCredentials
from tests.conftest import make_settings
from tests.pdfgen import make_pdf

BASE = "https://turkuaz.test/api"
PDF = make_pdf(["MADDE 1 - KONU\nTest."])
PDF_B64 = base64.b64encode(PDF).decode()


def record(i: int) -> dict:
    return {
        "id": f"C{i}",
        "contractNo": f"SZL-{i:03d}",
        "name": f"Sözleşme {i}",
        "parties": [{"name": "Doğuş Otomotiv"}, {"name": f"Firma {i}"}],
        "type": "Bayilik Sözleşmesi",
        "startDate": "2024-01-01",
        "endDate": "31.12.2026",
        "status": "Aktif",
        "lastModified": "2026-09-01T10:00:00Z",
        "region": "Marmara",
    }


def settings_for(tmp_path: Path, mode: str = "api_key", **overrides):
    values = {
        "CONTRACT_SOURCE": "turkuaz",
        "TURKUAZ_BASE_URL": BASE,
        "TURKUAZ_AUTH_MODE": mode,
        "TURKUAZ_API_KEY": "key-123456",
        "TURKUAZ_API_KEY_HEADER": "x-api-key",
        "TURKUAZ_BEARER_TOKEN": "static-token-abc",
        "TURKUAZ_TOKEN_URL": "https://login.test/token",
        "TURKUAZ_CLIENT_ID": "client",
        "TURKUAZ_CLIENT_SECRET": "secret-xyz",
        "TURKUAZ_SCOPE": "api://turkuaz/.default",
        "TURKUAZ_USERNAME": "ali",
        "TURKUAZ_PASSWORD": "sifre",
    }
    values.update(overrides)
    return make_settings(tmp_path, **values)


def source(settings, sleeps: list | None = None) -> TurkuazSource:
    return TurkuazSource(settings, sleep=(sleeps.append if sleeps is not None else lambda s: None))


@pytest.fixture
def api():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture(autouse=True)
def default_pagination(monkeypatch):
    monkeypatch.setitem(mapping.PAGINATION, "style", "page")
    monkeypatch.setitem(mapping.PAGINATION, "page_size", 2)


# --- configuration & auth ---


def test_missing_settings(tmp_path: Path) -> None:
    with pytest.raises(SourceError) as info:
        TurkuazSource(make_settings(tmp_path, TURKUAZ_AUTH_MODE="bearer"))
    assert "TURKUAZ_BASE_URL" in info.value.message and "TURKUAZ_BEARER_TOKEN" in info.value.message


@pytest.mark.parametrize(
    ("mode", "header", "value"),
    [
        ("api_key", "x-api-key", "key-123456"),
        ("bearer", "authorization", "Bearer static-token-abc"),
        ("basic", "authorization", "Basic " + base64.b64encode(b"ali:sifre").decode()),
    ],
)
def test_static_auth_modes(tmp_path: Path, api, mode: str, header: str, value: str) -> None:
    route = api.get(f"{BASE}/contracts").respond(json={"items": [record(1)], "totalCount": 1})
    metas = source(settings_for(tmp_path, mode)).list_contracts()
    assert [m.contract_id for m in metas] == ["C1"]
    assert route.calls[0].request.headers[header] == value


def test_custom_api_key_header(tmp_path: Path, api) -> None:
    route = api.get(f"{BASE}/contracts").respond(json={"items": []})
    source(settings_for(tmp_path, TURKUAZ_API_KEY_HEADER="Ocp-Apim-Subscription-Key")).list_contracts()
    assert route.calls[0].request.headers["Ocp-Apim-Subscription-Key"] == "key-123456"


def test_oauth_token_is_cached_and_refreshed(tmp_path: Path, api) -> None:
    tokens = iter(["tok-1", "tok-2", "tok-3"])
    token_route = api.post("https://login.test/token").mock(
        side_effect=lambda request: httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})
    )
    list_route = api.get(f"{BASE}/contracts").respond(json={"items": []})
    turkuaz = source(settings_for(tmp_path, "oauth_client_credentials"))
    now = [1000.0]
    assert isinstance(turkuaz.auth, _OAuthClientCredentials)
    turkuaz.auth._clock = lambda: now[0]

    turkuaz.list_contracts()
    turkuaz.list_contracts()
    assert token_route.call_count == 1  # cached
    body = token_route.calls[0].request.content.decode()
    assert "grant_type=client_credentials" in body and "client_id=client" in body and "scope=api" in body
    assert list_route.calls[-1].request.headers["authorization"] == "Bearer tok-1"

    now[0] += 3600 - 30  # inside the 60 s safety window -> refresh
    turkuaz.list_contracts()
    assert token_route.call_count == 2
    assert list_route.calls[-1].request.headers["authorization"] == "Bearer tok-2"


def test_oauth_refreshes_once_on_401(tmp_path: Path, api) -> None:
    tokens = iter(["old", "new"])
    api.post("https://login.test/token").mock(
        side_effect=lambda r: httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})
    )
    api.get(f"{BASE}/contracts").mock(
        side_effect=lambda r: (
            httpx.Response(401)
            if r.headers["authorization"] == "Bearer old"
            else httpx.Response(200, json={"items": [record(1)]})
        )
    )
    assert len(source(settings_for(tmp_path, "oauth_client_credentials")).list_contracts()) == 1


def test_oauth_token_failure(tmp_path: Path, api) -> None:
    api.post("https://login.test/token").respond(400, json={"error": "invalid_client"})
    with pytest.raises(SourceError) as info:
        source(settings_for(tmp_path, "oauth_client_credentials")).list_contracts()
    assert info.value.code == "turkuaz_token_failed" and "TURKUAZ_CLIENT_ID" in info.value.message


# --- errors & retries ---


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "turkuaz_unauthorized"),
        (403, "turkuaz_forbidden"),
        (404, "turkuaz_not_found"),
        (400, "turkuaz_bad_request"),
    ],
)
def test_client_errors_are_not_retried(tmp_path: Path, api, status: int, code: str) -> None:
    route = api.get(f"{BASE}/contracts").respond(status)
    with pytest.raises(SourceError) as info:
        source(settings_for(tmp_path)).list_contracts()
    assert info.value.code == code and route.call_count == 1
    assert not info.value.message.isascii()


def test_5xx_retried_with_backoff_then_success(tmp_path: Path, api) -> None:
    responses = iter([httpx.Response(502), httpx.Response(503), httpx.Response(200, json={"items": [record(1)]})])
    api.get(f"{BASE}/contracts").mock(side_effect=lambda r: next(responses))
    sleeps: list[float] = []
    assert len(source(settings_for(tmp_path), sleeps).list_contracts()) == 1
    assert sleeps == [2.0, 4.0]


def test_429_honours_retry_after_and_gives_up(tmp_path: Path, api) -> None:
    route = api.get(f"{BASE}/contracts").respond(429, headers={"Retry-After": "7"})
    sleeps: list[float] = []
    with pytest.raises(SourceError) as info:
        source(settings_for(tmp_path, TURKUAZ_MAX_RETRIES=2), sleeps).list_contracts()
    assert info.value.code == "turkuaz_rate_limited" and route.call_count == 3 and sleeps == [7.0, 7.0]


def test_timeout_and_connection_errors(tmp_path: Path, api) -> None:
    api.get(f"{BASE}/contracts").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(SourceError) as info:
        source(settings_for(tmp_path, TURKUAZ_MAX_RETRIES=1)).list_contracts()
    assert info.value.code == "turkuaz_timeout"
    api.get(f"{BASE}/contracts").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(SourceError) as info:
        source(settings_for(tmp_path, TURKUAZ_MAX_RETRIES=0)).list_contracts()
    assert info.value.code == "turkuaz_unreachable" and "HTTPS_PROXY" in info.value.message


# --- pagination ---


def test_page_pagination_and_updated_since(tmp_path: Path, api) -> None:
    from datetime import UTC, datetime

    pages = {1: [record(1), record(2)], 2: [record(3)]}
    route = api.get(f"{BASE}/contracts").mock(
        side_effect=lambda r: httpx.Response(200, json={"items": pages.get(int(r.url.params["page"]), [])})
    )
    metas = source(settings_for(tmp_path)).list_contracts(datetime(2026, 1, 1, tzinfo=UTC))
    assert [m.contract_id for m in metas] == ["C1", "C2", "C3"]
    assert route.call_count == 2  # second page was short -> stop
    params = route.calls[0].request.url.params
    assert params["updatedSince"] == "2026-01-01T00:00:00Z" and params["pageSize"] == "2"
    # Client-side guard: records older than updated_since are dropped.
    assert source(settings_for(tmp_path)).list_contracts(datetime(2026, 12, 1, tzinfo=UTC)) == []


def test_offset_pagination_with_total(tmp_path: Path, api, monkeypatch) -> None:
    monkeypatch.setitem(mapping.PAGINATION, "style", "offset")
    data = [record(i) for i in range(1, 5)]
    route = api.get(f"{BASE}/contracts").mock(
        side_effect=lambda r: httpx.Response(
            200, json={"items": data[int(r.url.params["offset"]) : int(r.url.params["offset"]) + 2], "totalCount": 4}
        )
    )
    assert len(source(settings_for(tmp_path)).list_contracts()) == 4
    assert route.call_count == 2  # stopped by totalCount


def test_cursor_pagination(tmp_path: Path, api, monkeypatch) -> None:
    monkeypatch.setitem(mapping.PAGINATION, "style", "cursor")
    api.get(f"{BASE}/contracts").mock(
        side_effect=lambda r: (
            httpx.Response(200, json={"items": [record(2)], "nextCursor": None})
            if r.url.params.get("cursor") == "abc"
            else httpx.Response(200, json={"items": [record(1)], "nextCursor": "abc"})
        )
    )
    assert [m.contract_id for m in source(settings_for(tmp_path)).list_contracts()] == ["C1", "C2"]


def test_no_pagination_list_response(tmp_path: Path, api, monkeypatch) -> None:
    monkeypatch.setitem(mapping.PAGINATION, "style", "none")
    monkeypatch.setitem(mapping.PAGINATION, "items_path", "")
    api.get(f"{BASE}/contracts").respond(json=[record(1), {"name": "id yok"}])
    assert [m.contract_id for m in source(settings_for(tmp_path)).list_contracts()] == ["C1"]  # invalid skipped


def test_unexpected_format(tmp_path: Path, api) -> None:
    api.get(f"{BASE}/contracts").respond(json={"items": {"not": "a list"}})
    with pytest.raises(SourceError) as info:
        source(settings_for(tmp_path)).list_contracts()
    assert info.value.code == "turkuaz_unexpected_format"


# --- documents ---


def test_documents_inline_and_download(tmp_path: Path, api) -> None:
    api.get(f"{BASE}/contracts/C%2F1/documents").respond(
        json={
            "documents": [
                {"documentId": "d1", "fileName": "Ana.pdf", "isAttachment": False, "contentBase64": PDF_B64},
                {"documentId": "d2", "fileName": "Ek-1.pdf", "documentType": "EK", "downloadUrl": f"{BASE}/files/d2"},
            ]
        }
    )
    download = api.get(f"{BASE}/files/d2").respond(content=PDF, headers={"Content-Type": "application/pdf"})
    files = source(settings_for(tmp_path)).get_contract_document("C/1")
    assert [(f.file_id, f.is_attachment) for f in files] == [("d1", False), ("d2", True)]
    assert files[1].read_bytes() == PDF and download.calls[0].request.headers["x-api-key"] == "key-123456"


def test_sync_through_turkuaz(tmp_path: Path, api) -> None:
    from app.db import connect
    from app.ingest.pipeline import run_sync

    settings = settings_for(tmp_path)
    api.get(f"{BASE}/contracts").respond(json={"items": [record(1)]})
    api.get(f"{BASE}/contracts/C1/documents").respond(
        json={"documents": [{"documentId": "d1", "fileName": "Ana.pdf", "contentBase64": PDF_B64}]}
    )
    stats = run_sync(settings)
    assert (stats.status, stats.new) == ("success", 1)
    row = connect(settings.db_path).execute("SELECT source, extra_json, end_date FROM contracts").fetchone()
    assert row["source"] == "turkuaz" and json.loads(row["extra_json"]) == {"region": "Marmara"}
    assert row["end_date"] == "2026-12-31"


# --- cli check-turkuaz ---


def test_check_turkuaz_cli(tmp_path: Path, api, capsys) -> None:
    api.get(f"{BASE}/contracts").respond(json={"items": [record(1), record(2)], "totalCount": 2})
    api.get(f"{BASE}/contracts/C1/documents").respond(
        json={"documents": [{"documentId": "d1", "fileName": "Ana.pdf", "contentBase64": PDF_B64}]}
    )
    out = tmp_path / "ornek.json"
    assert cli.run_check_turkuaz(settings_for(tmp_path), out=str(out)) == 0
    text = capsys.readouterr().out
    assert "parties[].name" in text and "contractNo" in text
    assert "Eşleştirilmemiş alanlar (extra): region" in text
    assert "base64 olabilir" in text and "key-123456" not in text
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["records"][0]["id"] == "C1" and "karakter" in saved["documents"]["documents"][0]["contentBase64"]


def test_check_turkuaz_reports_errors(tmp_path: Path, api, capsys) -> None:
    api.get(f"{BASE}/contracts").respond(403)
    assert cli.run_check_turkuaz(settings_for(tmp_path)) == 1
    assert "403" in capsys.readouterr().out
    assert cli.run_check_turkuaz(make_settings(tmp_path)) == 1
    assert "Eksik ayarlar" in capsys.readouterr().out
