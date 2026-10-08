"""SQLite access, schema migrations and FTS5 setup.

Connections run in autocommit mode (`isolation_level=None`); use `transaction()`
for multi-statement writes. Migrations are applied under `BEGIN IMMEDIATE` so
several uvicorn workers starting at once do not race each other.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# Each entry is (version, description, sql). Never edit an applied migration;
# append a new one instead.
MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial schema",
        """
        CREATE TABLE contracts (
            contract_id       TEXT PRIMARY KEY,
            contract_no       TEXT,
            title             TEXT,
            parties_json      TEXT NOT NULL DEFAULT '[]',
            parties_norm      TEXT NOT NULL DEFAULT '',
            contract_type     TEXT,
            start_date        TEXT,
            end_date          TEXT,
            status            TEXT,
            source_updated_at TEXT,
            extra_json        TEXT NOT NULL DEFAULT '{}',
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        );
        CREATE INDEX ix_contracts_no ON contracts(contract_no);
        CREATE INDEX ix_contracts_type ON contracts(contract_type);

        CREATE TABLE contract_files (
            contract_id   TEXT NOT NULL REFERENCES contracts(contract_id) ON DELETE CASCADE,
            file_id       TEXT NOT NULL,
            file_name     TEXT NOT NULL,
            is_attachment INTEGER NOT NULL DEFAULT 0,
            mime_type     TEXT NOT NULL DEFAULT 'application/pdf',
            sha256        TEXT NOT NULL,
            storage_path  TEXT,
            page_count    INTEGER NOT NULL DEFAULT 0,
            char_count    INTEGER NOT NULL DEFAULT 0,
            needs_ocr     INTEGER NOT NULL DEFAULT 0,
            ocr_status    TEXT,
            indexed_at    TEXT,
            PRIMARY KEY (contract_id, file_id)
        );

        CREATE TABLE chunks (
            id            INTEGER PRIMARY KEY,
            contract_id   TEXT NOT NULL,
            file_id       TEXT NOT NULL,
            is_attachment INTEGER NOT NULL DEFAULT 0,
            ordinal       INTEGER NOT NULL,
            clause_ref    TEXT,
            clause_title  TEXT,
            page_start    INTEGER,
            page_end      INTEGER,
            text          TEXT NOT NULL,
            text_norm     TEXT NOT NULL,
            FOREIGN KEY (contract_id, file_id)
                REFERENCES contract_files(contract_id, file_id) ON DELETE CASCADE
        );
        CREATE INDEX ix_chunks_contract ON chunks(contract_id, file_id, ordinal);

        -- External-content FTS index over the Turkish-normalized text.
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            text_norm,
            content='chunks',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, text_norm) VALUES (new.id, new.text_norm);
        END;
        CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text_norm) VALUES ('delete', old.id, old.text_norm);
        END;
        CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text_norm) VALUES ('delete', old.id, old.text_norm);
            INSERT INTO chunks_fts(rowid, text_norm) VALUES (new.id, new.text_norm);
        END;

        CREATE TABLE embeddings (
            chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
            model    TEXT NOT NULL,
            dim      INTEGER NOT NULL,
            vector   BLOB NOT NULL
        );

        CREATE TABLE sync_runs (
            id              INTEGER PRIMARY KEY,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            status          TEXT NOT NULL,
            trigger         TEXT NOT NULL,
            triggered_by    TEXT,
            new_count       INTEGER NOT NULL DEFAULT 0,
            updated_count   INTEGER NOT NULL DEFAULT 0,
            unchanged_count INTEGER NOT NULL DEFAULT 0,
            error_count     INTEGER NOT NULL DEFAULT 0,
            errors_json     TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE analysis_jobs (
            id                TEXT PRIMARY KEY,
            created_by        TEXT,
            created_at        TEXT NOT NULL,
            started_at        TEXT,
            finished_at       TEXT,
            status            TEXT NOT NULL,
            cancel_requested  INTEGER NOT NULL DEFAULT 0,
            title             TEXT,
            legislation_text  TEXT NOT NULL,
            extraction_json   TEXT NOT NULL DEFAULT '{}',
            keywords_json     TEXT NOT NULL DEFAULT '[]',
            filters_json      TEXT NOT NULL DEFAULT '{}',
            total             INTEGER NOT NULL DEFAULT 0,
            processed         INTEGER NOT NULL DEFAULT 0,
            error_message     TEXT,
            worker_id         TEXT,
            heartbeat_at      TEXT
        );
        CREATE INDEX ix_jobs_created ON analysis_jobs(created_at);

        CREATE TABLE analysis_results (
            id           INTEGER PRIMARY KEY,
            job_id       TEXT NOT NULL REFERENCES analysis_jobs(id) ON DELETE CASCADE,
            contract_id  TEXT NOT NULL,
            impact_level TEXT NOT NULL,
            confidence   REAL,
            result_json  TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );
        CREATE INDEX ix_results_job ON analysis_results(job_id);

        CREATE TABLE llm_usage (
            id            INTEGER PRIMARY KEY,
            created_at    TEXT NOT NULL,
            day           TEXT NOT NULL,
            model         TEXT NOT NULL,
            purpose       TEXT NOT NULL,
            user_id       TEXT,
            input_tokens  INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens  INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX ix_llm_usage_day ON llm_usage(day);

        CREATE TABLE audit_log (
            id              INTEGER PRIMARY KEY,
            ts              TEXT NOT NULL,
            user_id         TEXT,
            action          TEXT NOT NULL,
            target_type     TEXT,
            target_ids_json TEXT NOT NULL DEFAULT '[]',
            details_json    TEXT NOT NULL DEFAULT '{}',
            ip              TEXT
        );
        CREATE INDEX ix_audit_ts ON audit_log(ts);

        -- Cross-process locks (sync, background jobs) shared by all workers.
        CREATE TABLE app_locks (
            name        TEXT PRIMARY KEY,
            owner       TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        "ingestion details",
        """
        ALTER TABLE contracts ADD COLUMN meta_norm TEXT NOT NULL DEFAULT '';
        ALTER TABLE contracts ADD COLUMN source TEXT;
        ALTER TABLE contract_files ADD COLUMN extract_method TEXT;
        ALTER TABLE contract_files ADD COLUMN low_text_pages_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE contract_files ADD COLUMN error TEXT;
        ALTER TABLE contract_files ADD COLUMN updated_at TEXT;
        CREATE INDEX ix_contracts_end ON contracts(end_date);
        CREATE INDEX ix_files_ocr ON contract_files(needs_ocr, ocr_status);
        CREATE INDEX ix_sync_runs_status ON sync_runs(status, id);
        """,
    ),
    (
        3,
        "impact analysis jobs",
        """
        ALTER TABLE analysis_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE analysis_jobs ADD COLUMN input_kind TEXT NOT NULL DEFAULT 'text';
        ALTER TABLE analysis_results ADD COLUMN error TEXT;
        CREATE INDEX ix_jobs_status ON analysis_jobs(status, created_at);
        """,
    ),
]


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Turkish-aware folding usable in SQL (filters on metadata columns).
    from app.ingest.normalize import fold

    conn.create_function("tr_fold", 1, lambda v: fold(v) if isinstance(v, str) else v, deterministic=True)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def split_sql(script: str) -> list[str]:
    """Split a script into complete statements (trigger bodies stay intact).

    `executescript` would COMMIT implicitly, breaking the migration lock, so
    statements are executed one by one inside our own transaction.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        if not buffer and line.strip().startswith("--"):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise ValueError("Incomplete SQL statement in migration")
    return statements


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations. Returns the versions applied by this call."""
    applied: list[int] = []
    current_version(conn)  # ensure bookkeeping table exists
    with transaction(conn):
        version = current_version(conn)
        for number, description, sql in MIGRATIONS:
            if number <= version:
                continue
            for statement in split_sql(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)",
                (number, description, utcnow_iso()),
            )
            applied.append(number)
    return applied


def check_fts5() -> bool:
    try:
        probe = sqlite3.connect(":memory:")
        probe.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        probe.close()
        return True
    except sqlite3.OperationalError:
        return False


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "contracts",
        "contract_files",
        "chunks",
        "embeddings",
        "sync_runs",
        "analysis_jobs",
        "llm_usage",
        "audit_log",
    ]
    return {t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in tables}  # noqa: S608
