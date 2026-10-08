from __future__ import annotations

import json
import logging
from pathlib import Path

from app.db import connect, migrate
from app.logging_conf import JsonFormatter, SecretMasker, write_audit


def _format(masker: SecretMasker, msg: str, **extra: object) -> dict:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return json.loads(JsonFormatter(masker).format(record))


def test_known_secret_values_are_masked() -> None:
    out = _format(SecretMasker(["abcdef123456"]), "calling with abcdef123456")
    assert "abcdef123456" not in out["msg"]


def test_credential_patterns_are_masked() -> None:
    out = _format(SecretMasker(), "Authorization: Bearer eyJhbGciOi.payload.sig api_key=xyz987 ok")
    assert "eyJhbGciOi" not in out["msg"]
    assert "xyz987" not in out["msg"]
    assert out["msg"].endswith("ok")


def test_extra_fields_are_emitted() -> None:
    out = _format(SecretMasker(), "request", path="/health", status=200)
    assert out["path"] == "/health" and out["status"] == 200


def test_write_audit(tmp_path: Path) -> None:
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    write_audit(conn, "contract.view", user_id="ali", target_type="contract", target_ids=["c1"])
    row = conn.execute("SELECT user_id, action, target_ids_json FROM audit_log").fetchone()
    assert tuple(row) == ("ali", "contract.view", '["c1"]')
