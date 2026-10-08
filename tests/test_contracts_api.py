from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.ingest.pipeline import run_sync
from app.main import create_app
from tests.conftest import make_settings
from tests.pdfgen import write_sample_contracts

P = "/sozlesme"


def synced_client(tmp_path: Path) -> TestClient:
    contracts = tmp_path / "contracts"
    write_sample_contracts(contracts)
    settings = make_settings(tmp_path, LOCAL_CONTRACTS_DIR=contracts)
    run_sync(settings)
    return TestClient(create_app(settings))


def test_list_and_filters(tmp_path: Path) -> None:
    with synced_client(tmp_path) as client:
        body = client.get(f"{P}/api/contracts").json()
        assert body["total"] == 2
        xyz = next(i for i in body["items"] if i["contract_id"] == "local-xyz-servis")
        assert xyz["file_count"] == 2 and xyz["chunk_count"] >= 4 and xyz["is_active"]
        assert client.get(f"{P}/api/contracts", params={"q": "xyz"}).json()["total"] == 1
        assert client.get(f"{P}/api/contracts", params={"party": "XYZ SERVİS"}).json()["total"] == 1
        assert (
            client.get(f"{P}/api/contracts", params={"contract_type": "yetkili servis sözleşmesi"}).json()["total"] == 1
        )
        assert client.get(f"{P}/api/contracts", params={"date_to": "2023-12-31"}).json()["total"] == 1
        assert client.get(f"{P}/api/contracts", params={"page_size": 1, "page": 2}).json()["items"]
        options = client.get(f"{P}/api/contracts/filters").json()
        assert options["contract_types"] == ["Yetkili Servis Sözleşmesi"] and options["statuses"] == ["Aktif"]


def test_detail_file_and_chunk(tmp_path: Path) -> None:
    with synced_client(tmp_path) as client:
        detail = client.get(f"{P}/api/contracts/local-xyz-servis").json()
        assert detail["contract_no"] == "SZL-2024-007"
        assert [f["is_attachment"] for f in detail["files"]] == [False, True]
        clause = next(c for c in detail["clauses"] if c["clause_ref"] == "Madde 3")
        assert "cezai" in clause["text"].lower()

        pdf = client.get(f"{P}/api/contracts/local-xyz-servis/files/ana-sozlesme")
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.headers["content-disposition"].startswith("inline")
        assert "content-security-policy" not in pdf.headers

        chunk = client.get(f"{P}/api/chunks/{clause['id']}").json()
        assert chunk["contract_no"] == "SZL-2024-007" and chunk["file_name"] == "Ana_Sozlesme.pdf"

        assert client.get(f"{P}/api/contracts/yok").status_code == 404
        assert client.get(f"{P}/api/contracts/local-xyz-servis/files/yok").status_code == 404
        actions = [r for r in client.get(f"{P}/api/contracts/local-xyz-servis").json()["files"]]
        assert actions


def test_admin_sync_and_status(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts"
    write_sample_contracts(contracts)
    settings = make_settings(tmp_path, LOCAL_CONTRACTS_DIR=contracts)
    with TestClient(create_app(settings)) as client:
        response = client.post(f"{P}/api/admin/sync", json={"full": False})
        assert response.status_code == 202
        deadline = time.time() + 20
        status = {}
        while time.time() < deadline:
            status = client.get(f"{P}/api/admin/status").json()
            if status["sync_runs"] and status["sync_runs"][0]["status"] != "running":
                break
            time.sleep(0.1)
        assert status["sync_runs"][0]["status"] == "success"
        assert status["sync_runs"][0]["triggered_by"] == "gelistirici"
        assert status["counts"]["contracts"] == 2 and status["counts"]["attachments"] == 1
        assert status["config"]["contract_source"] == "local"
