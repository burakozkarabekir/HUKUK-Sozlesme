from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# `app.main` builds a module-level app on import; keep its files out of the repo.
_BOOT_DIR = Path(tempfile.mkdtemp(prefix="sozlesme-test-"))
os.environ.setdefault("DATA_DIR", str(_BOOT_DIR / "data"))
os.environ.setdefault("LOG_DIR", str(_BOOT_DIR / "logs"))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "APP_ENV": "dev",
        "AUTH_MODE": "none",
        "DATA_DIR": tmp_path / "data",
        "LOG_DIR": tmp_path / "logs",
        "ROOT_PATH": "/sozlesme",
    }
    values.update(overrides)
    # _env_file=None: never read a developer's real .env during tests.
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client
