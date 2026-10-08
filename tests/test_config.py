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
    users = "ali:$2b$12$abcdefghijklmnopqrstuuF0XyZ7z6r5Ue8m6a1pQx3o1Y5x0aB2C"
    assert make_settings(tmp_path, APP_ENV="prod", AUTH_MODE="basic", BASIC_AUTH_USERS=users).APP_ENV == "prod"


def test_basic_requires_users(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="BASIC_AUTH_USERS"):
        make_settings(tmp_path, AUTH_MODE="basic")
    with pytest.raises(ValidationError, match="geçersiz"):
        make_settings(tmp_path, AUTH_MODE="basic", BASIC_AUTH_USERS="ali:plaintext")


def test_entra_requires_settings_and_strong_secret(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="ENTRA_TENANT_ID"):
        make_settings(tmp_path, AUTH_MODE="entra")
    full = {
        "ENTRA_TENANT_ID": "t",
        "ENTRA_CLIENT_ID": "c",
        "ENTRA_CLIENT_SECRET": "s",
        "ENTRA_REDIRECT_URI": "https://x/sozlesme/auth/callback",
    }
    with pytest.raises(ValidationError, match="32"):
        make_settings(tmp_path, AUTH_MODE="entra", SESSION_SECRET_KEY="kisa", **full)
    assert make_settings(tmp_path, AUTH_MODE="entra", SESSION_SECRET_KEY="x" * 40, **full).AUTH_MODE == "entra"


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
