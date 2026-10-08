from __future__ import annotations

from pathlib import Path

import pytest

import cli
from app.config import get_settings


def test_migrate_and_stats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    try:
        assert cli.main(["migrate"]) == 0
        assert cli.main(["stats"]) == 0
        out = capsys.readouterr().out
        assert "contracts" in out
        assert (tmp_path / "data" / "app.db").exists()
    finally:
        get_settings.cache_clear()


def test_sync_and_reindex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from tests.pdfgen import write_sample_contracts

    write_sample_contracts(tmp_path / "contracts")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCAL_CONTRACTS_DIR", str(tmp_path / "contracts"))
    get_settings.cache_clear()
    try:
        assert cli.main(["sync"]) == 0
        assert "Yeni: 2" in capsys.readouterr().out
        assert cli.main(["reindex"]) == 0
        assert "3 dosya" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


def test_demo_data_and_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCAL_CONTRACTS_DIR", str(tmp_path / "contracts"))
    get_settings.cache_clear()
    try:
        assert cli.main(["demo-data"]) == 0
        assert cli.main(["sync"]) == 0
        out = capsys.readouterr().out
        assert "Yeni: 6" in out
        from app.db import connect

        conn = connect(tmp_path / "data" / "app.db")
        refs = {r[0] for r in conn.execute("SELECT clause_ref FROM chunks WHERE contract_id LIKE '%anadolu%'")}
        assert {"Madde 5.1", "Madde 5.2", "Ek-1"} <= refs or {"Madde 5", "Ek-1"} <= refs
        assert conn.execute("SELECT COUNT(*) FROM contract_files WHERE ocr_status = 'pending'").fetchone()[0] == 1
    finally:
        get_settings.cache_clear()


def test_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import sqlite3

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    try:
        assert cli.main(["migrate"]) == 0
        out = tmp_path / "yedek.db"
        assert cli.main(["backup", "--out", str(out)]) == 0
        assert sqlite3.connect(out).execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 3
    finally:
        get_settings.cache_clear()
