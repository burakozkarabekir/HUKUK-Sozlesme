from __future__ import annotations

import base64
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.sources.base import ContractFile, ContractMeta, SourceError, parse_date
from app.sources.local_source import LocalSource, is_attachment_name
from app.sources.mock_source import MockSource
from app.sources.turkuaz_mapping import get_path, map_contract, map_file
from tests.pdfgen import write_sample_contracts


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-03-31", date(2024, 3, 31)),
        ("2024-03-31T10:00:00Z", date(2024, 3, 31)),
        ("31.03.2024", date(2024, 3, 31)),
        ("31/03/2024", date(2024, 3, 31)),
        ("", None),
        (None, None),
    ],
)
def test_parse_date(value, expected) -> None:
    assert parse_date(value) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Ek-1_Fiyat.pdf", True),
        ("EK2.pdf", True),
        ("ek 3 liste.pdf", True),
        ("Ek_IV.pdf", True),
        ("Annex A.pdf", True),
        ("Ekipman_Sozlesmesi.pdf", False),
        ("Ana_Sozlesme.pdf", False),
    ],
)
def test_attachment_names(name: str, expected: bool) -> None:
    assert is_attachment_name(name) is expected


def test_local_source(tmp_path: Path) -> None:
    write_sample_contracts(tmp_path)
    source = LocalSource(tmp_path)
    metas = {m.contract_id: m for m in source.list_contracts()}
    assert set(metas) == {"local-abc-bayilik", "local-xyz-servis"}
    xyz = metas["local-xyz-servis"]
    assert xyz.contract_no == "SZL-2024-007" and xyz.parties == ["Doğuş Otomotiv", "XYZ Servis Ltd."]
    assert xyz.end_date == date(2027, 12, 31)
    assert metas["local-abc-bayilik"].title == "ABC Bayilik"
    files = {f.file_name: f for f in source.get_contract_document("local-xyz-servis")}
    assert files["Ek-1_Fiyat_Listesi.pdf"].is_attachment and not files["Ana_Sozlesme.pdf"].is_attachment
    assert files["Ana_Sozlesme.pdf"].read_bytes().startswith(b"%PDF")
    # Incremental: nothing changed after "now".
    assert source.list_contracts(datetime.now(UTC) + timedelta(seconds=5)) == []


def test_local_source_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(SourceError):
        LocalSource(tmp_path / "yok").list_contracts()


def test_mock_source_goes_through_turkuaz_mapping(tmp_path: Path) -> None:
    write_sample_contracts(tmp_path)
    source = MockSource(tmp_path)
    metas = {m.contract_id: m for m in source.list_contracts()}
    meta = metas["TRK-local-xyz-servis"]
    assert meta.contract_no == "SZL-2024-007" and meta.contract_type == "Yetkili Servis Sözleşmesi"
    assert meta.extra == {"department": "Hukuk"}  # unmapped Turkuaz field kept in extra
    abc = metas["TRK-local-abc-bayilik"]
    assert abc.parties[0].startswith("Doğuş Otomotiv") and abc.start_date and abc.status
    files = source.get_contract_document("TRK-local-xyz-servis")
    assert {f.is_attachment for f in files} == {True, False}
    assert all(f.content_base64 and f.read_bytes().startswith(b"%PDF") for f in files)


def test_get_path() -> None:
    raw = {"a": {"b": 1}, "parties": [{"name": "X"}, {"name": ""}, {"name": "Y"}], "tags": ["t1"]}
    assert get_path(raw, "a.b") == 1
    assert get_path(raw, "parties[].name") == ["X", "Y"]
    assert get_path(raw, "tags[]") == ["t1"]
    assert get_path(raw, "a.missing") is None and get_path(raw, "x.y") is None


def test_map_contract_and_file() -> None:
    meta = map_contract(
        {
            "id": 42,
            "contractNo": "S-1",
            "name": "Kira",
            "parties": [{"name": "A"}],
            "type": "Kira",
            "startDate": "01.02.2024",
            "endDate": None,
            "status": "Aktif",
            "lastModified": "2026-01-01T00:00:00",
            "owner": "Hukuk",
        }
    )
    assert isinstance(meta, ContractMeta) and meta.contract_id == "42"
    assert meta.start_date == date(2024, 2, 1) and meta.updated_at.tzinfo is not None
    assert meta.extra == {"owner": "Hukuk"}
    file = map_file(
        {
            "documentId": 7,
            "fileName": "x.pdf",
            "documentType": "EK",
            "contentBase64": base64.b64encode(b"%PDF-1.4").decode(),
        }
    )
    assert isinstance(file, ContractFile) and file.file_id == "7" and file.is_attachment
    assert file.read_bytes() == b"%PDF-1.4"


def test_file_without_content_raises() -> None:
    with pytest.raises(SourceError):
        ContractFile(file_id="1", file_name="a.pdf").read_bytes()
