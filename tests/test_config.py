from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import BASE_DIR, apply_network_env
from tests.conftest import make_settings


def test_prod_with_auth_none_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="AUTH_MODE=none"):
        make_settings(tmp_path, APP_ENV="prod", AUTH_MODE="none")


def test_prod_with_basic_auth_is_allowed(tmp_path: Path) -> None:
    assert make_settings(tmp_path, APP_ENV="prod", AUTH_MODE="basic").APP_ENV == "prod"


@pytest.mark.parametrize(
    ("raw", "expected"), [("/sozlesme", "/sozlesme"), ("sozlesme/", "/sozlesme"), ("/", ""), ("", "")]
)
def test_root_path_normalized(tmp_path: Path, raw: str, expected: str) -> None:
    assert make_settings(tmp_path, ROOT_PATH=raw).ROOT_PATH == expected


def test_relative_dirs_resolve_against_project_root(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, DATA_DIR="./data")
    assert settings.DATA_DIR == BASE_DIR / "data"
    assert settings.db_path == BASE_DIR / "data" / "app.db"


def test_secrets_hidden_in_repr(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, AZURE_OPENAI_API_KEY="super-secret-key-123")
    assert "super-secret-key-123" not in repr(settings)
    assert "super-secret-key-123" in settings.secret_values()


def test_csv_helpers(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, ALLOWED_GROUP_IDS=" a, b ,,c", EMBEDDING_MODEL=" ")
    assert settings.allowed_group_ids == ["a", "b", "c"]
    assert settings.vector_search_enabled is False


def test_apply_network_env_does_not_override_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://from-env:8080")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    settings = make_settings(tmp_path, HTTPS_PROXY="http://from-dotenv:3128", SSL_CERT_FILE="/tmp/ca.pem")
    apply_network_env(settings)
    assert os.environ["HTTPS_PROXY"] == "http://from-env:8080"
    assert os.environ["SSL_CERT_FILE"] == "/tmp/ca.pem"
