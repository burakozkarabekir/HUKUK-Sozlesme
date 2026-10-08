from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from app.net import build_ssl_context
from tests.conftest import make_settings


def test_default_context_uses_system_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    context = build_ssl_context(make_settings(tmp_path))
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_missing_ca_file_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="CA sertifika"):
        build_ssl_context(make_settings(tmp_path, REQUESTS_CA_BUNDLE=str(tmp_path / "yok.pem")))
