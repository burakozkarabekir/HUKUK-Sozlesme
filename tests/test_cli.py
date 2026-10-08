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


def test_unimplemented_command_returns_2(capsys) -> None:
    assert cli.main(["sync"]) == 2
    assert "Faz 3" in capsys.readouterr().err
