from __future__ import annotations

from pathlib import Path

from app.db import MIGRATIONS, connect, current_version, migrate, split_sql, transaction


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "app.db")
    assert migrate(conn) == [m[0] for m in MIGRATIONS]
    assert migrate(conn) == []
    assert current_version(conn) == MIGRATIONS[-1][0]


def test_fts_triggers_keep_index_in_sync(tmp_path: Path) -> None:
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    with transaction(conn):
        conn.execute("INSERT INTO contracts (contract_id, created_at, updated_at) VALUES ('c1', 'x', 'x')")
        conn.execute(
            "INSERT INTO contract_files (contract_id, file_id, file_name, sha256)"
            " VALUES ('c1', 'f1', 'a.pdf', 'h')"
        )
        conn.execute(
            "INSERT INTO chunks (contract_id, file_id, ordinal, text, text_norm)"
            " VALUES ('c1','f1',0,'Fesih bildirimi','fesih bildirimi')"
        )

    def hits(term: str) -> int:
        return conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (term,)).fetchone()[0]

    assert hits("fesih") == 1
    conn.execute("UPDATE chunks SET text_norm = 'sure uzatimi' WHERE contract_id = 'c1'")
    assert hits("fesih") == 0
    assert hits("uzatimi") == 1
    # Deleting the contract cascades to files and chunks, and the FTS index follows.
    conn.execute("DELETE FROM contracts WHERE contract_id = 'c1'")
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert hits("uzatimi") == 0


def test_transaction_rolls_back(tmp_path: Path) -> None:
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    try:
        with transaction(conn):
            conn.execute("INSERT INTO audit_log (ts, action) VALUES ('t', 'x')")
            raise RuntimeError
    except RuntimeError:
        pass
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_split_sql_keeps_trigger_body() -> None:
    sql = """
    CREATE TABLE a (x);
    -- comment
    CREATE TRIGGER t AFTER INSERT ON a BEGIN
        INSERT INTO a VALUES (1);
        INSERT INTO a VALUES (2);
    END;
    """
    statements = split_sql(sql)
    assert len(statements) == 2
    assert statements[1].startswith("CREATE TRIGGER") and statements[1].endswith("END;")
