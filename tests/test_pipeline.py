from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from app.db import connect, migrate
from app.ingest.pipeline import SYNC_LOCK, SyncAlreadyRunning, reindex, run_sync
from app.locks import acquire_lock, lock_holder, release_lock
from tests.conftest import make_settings
from tests.pdfgen import make_pdf, write_sample_contracts


def setup(tmp_path: Path, **overrides):
    contracts = tmp_path / "contracts"
    write_sample_contracts(contracts)
    settings = make_settings(tmp_path, LOCAL_CONTRACTS_DIR=contracts, **overrides)
    return settings, contracts


def db(settings):
    conn = connect(settings.db_path)
    migrate(conn)
    return conn


def test_full_pipeline_and_incremental_sync(tmp_path: Path) -> None:
    settings, contracts = setup(tmp_path)
    stats = run_sync(settings)
    assert (stats.status, stats.new, stats.updated, stats.errors) == ("success", 2, 0, [])
    conn = db(settings)
    assert conn.execute("SELECT COUNT(*) FROM contract_files").fetchone()[0] == 3
    refs = [
        r[0]
        for r in conn.execute("SELECT clause_ref FROM chunks WHERE contract_id='local-abc-bayilik' ORDER BY ordinal")
    ]
    assert refs == ["Giriş", "Madde 1", "Madde 2", "Madde 3", "Madde 4"]
    ek = conn.execute("SELECT clause_ref, is_attachment FROM chunks WHERE file_id='ek-1-fiyat-listesi'").fetchall()
    assert [tuple(r) for r in ek] == [("Ek-1", 1)]
    # FTS on normalized text works with Turkish characters folded.
    hits = conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'gizlilik'").fetchone()[0]
    assert hits >= 1
    # Stored PDF exists and is content-addressed.
    path = conn.execute("SELECT storage_path FROM contract_files LIMIT 1").fetchone()[0]
    assert (settings.DATA_DIR / path).is_file()
    run = conn.execute("SELECT status, new_count FROM sync_runs").fetchone()
    assert tuple(run) == ("success", 2)

    # Second run: nothing changed since the last successful start.
    assert (run_sync(settings).new, run_sync(settings).updated) == (0, 0)

    # Change one file: only that contract is updated, chunks replaced.
    time.sleep(0.01)
    target = contracts / "ABC_Bayilik.pdf"
    target.write_bytes(make_pdf(["MADDE 1 - YENİ\nYeni metin.\nMADDE 2 - İKİNCİ\nİkinci."]))
    future = time.time() + 5
    os.utime(target, (future, future))
    stats = run_sync(settings)
    assert (stats.new, stats.updated) == (0, 1)
    refs = [r[0] for r in conn.execute("SELECT clause_ref FROM chunks WHERE contract_id='local-abc-bayilik'")]
    assert refs == ["Madde 1", "Madde 2"]


def test_full_sync_removes_contracts_missing_from_source(tmp_path: Path) -> None:
    settings, contracts = setup(tmp_path)
    run_sync(settings)
    (contracts / "ABC_Bayilik.pdf").unlink()
    stats = run_sync(settings, full=True)
    assert stats.deleted == 1 and stats.unchanged == 1
    conn = db(settings)
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE contract_id='local-abc-bayilik'").fetchone()[0] == 0


def test_scanned_pdf_marked_for_ocr(tmp_path: Path) -> None:
    settings, contracts = setup(tmp_path)
    (contracts / "Taranmis.pdf").write_bytes(make_pdf(["", ""]))
    run_sync(settings)
    row = db(settings).execute("SELECT needs_ocr, ocr_status FROM contract_files WHERE file_id='taranmis'").fetchone()
    assert tuple(row) == (1, "pending")


def test_ocr_used_when_enabled(tmp_path: Path) -> None:
    from app.ingest.pdf_extract import PageText

    class FakeOcr:
        def ocr_pdf(self, data: bytes) -> list[PageText]:
            return [PageText(1, "MADDE 1 - OCR\nTaranmış metin.\nMADDE 2 - İKİ\nİkinci.")]

    settings, contracts = setup(tmp_path, OCR_ENABLED=True)
    (contracts / "Taranmis.pdf").write_bytes(make_pdf([""]))
    run_sync(settings, ocr=FakeOcr())
    conn = db(settings)
    assert conn.execute("SELECT ocr_status FROM contract_files WHERE file_id='taranmis'").fetchone()[0] == "done"
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE file_id='taranmis'").fetchone()[0] == 2


def test_broken_pdf_is_partial_failure(tmp_path: Path) -> None:
    settings, contracts = setup(tmp_path)
    (contracts / "Bozuk.pdf").write_bytes(b"bu bir pdf degil")
    stats = run_sync(settings)
    assert stats.status == "partial" and stats.new == 2
    assert stats.errors == [{"contract_id": "local-bozuk", "message": "Dosya geçerli bir PDF değil."}]
    run = db(settings).execute("SELECT status, error_count, errors_json FROM sync_runs").fetchone()
    assert run["status"] == "partial" and run["error_count"] == 1 and json.loads(run["errors_json"])
    # A partial run does not advance the incremental watermark.
    assert run_sync(settings).unchanged == 2


def test_missing_source_dir_is_failed_run(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, LOCAL_CONTRACTS_DIR=tmp_path / "yok")
    stats = run_sync(settings)
    assert stats.status == "failed" and "bulunamadı" in stats.errors[0]["message"]
    assert lock_holder(db(settings), SYNC_LOCK) is None  # lock released


def test_lock_prevents_concurrent_sync(tmp_path: Path) -> None:
    settings, _ = setup(tmp_path)
    conn = db(settings)
    assert acquire_lock(conn, SYNC_LOCK, "other-process", 60)
    with pytest.raises(SyncAlreadyRunning):
        run_sync(settings)
    release_lock(conn, SYNC_LOCK, "other-process")
    assert run_sync(settings).status == "success"


def test_expired_lock_can_be_taken_over(tmp_path: Path) -> None:
    settings, _ = setup(tmp_path)
    conn = db(settings)
    assert acquire_lock(conn, "x", "a", -1)  # already expired
    assert acquire_lock(conn, "x", "b", 60)
    assert not acquire_lock(conn, "x", "a", 60)


def test_reindex(tmp_path: Path) -> None:
    settings, _ = setup(tmp_path)
    run_sync(settings)
    conn = db(settings)
    before = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    conn.execute("DELETE FROM chunks")
    counts = reindex(settings)
    assert counts == {"files": 3, "chunks": before, "errors": 0}
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'fesih'").fetchone()[0] >= 1
