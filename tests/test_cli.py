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
