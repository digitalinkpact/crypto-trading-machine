from __future__ import annotations

import shutil
import sqlite3

from scripts.backup_trading_db import BackupError, backup_database


def test_backup_database_creates_verified_atomic_destination(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    connection = sqlite3.connect(source)
    connection.executescript(
        "CREATE TABLE positions (symbol TEXT);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY);"
        "CREATE TABLE order_intents (id INTEGER PRIMARY KEY);"
        "CREATE TABLE closed_trades (id INTEGER PRIMARY KEY);"
        "CREATE TABLE kv (key TEXT PRIMARY KEY);"
        "INSERT INTO positions VALUES ('BTCUSDT');"
        "INSERT INTO orders DEFAULT VALUES;"
        "INSERT INTO kv VALUES ('x');"
    )
    connection.commit()
    connection.close()

    result = backup_database(source, destination, timeout=5, max_seconds=30)

    assert destination.exists()
    assert result["source_integrity"] == "ok"
    assert result["destination_integrity"] == "ok"
    assert result["counts"] == {
        "positions": 1,
        "orders": 1,
        "order_intents": 0,
        "closed_trades": 0,
        "kv": 1,
    }
    assert (tmp_path / "backup.db.tmp").exists() is False


def test_backup_can_be_restored_and_reopened_by_storage(tmp_path):
    source = tmp_path / "restore-source.db"
    backup = tmp_path / "restore-backup.db"
    restored = tmp_path / "restored" / "trading.db"

    connection = sqlite3.connect(source)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        "CREATE TABLE positions (symbol TEXT, mode TEXT, qty REAL, entry_price REAL, entry_ts TEXT, agents TEXT);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, ts TEXT, mode TEXT, symbol TEXT, side TEXT, qty REAL, price REAL, fee REAL);"
        "CREATE TABLE order_intents (id INTEGER PRIMARY KEY, client_order_id TEXT);"
        "CREATE TABLE closed_trades (id INTEGER PRIMARY KEY, mode TEXT, symbol TEXT);"
        "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);"
        "INSERT INTO positions VALUES ('SOLUSDT','live',1,100,'2026-01-01','[]');"
        "INSERT INTO orders(ts,mode,symbol,side,qty,price,fee) VALUES ('2026-01-01','live','SOLUSDT','BUY',1,100,0);"
        "INSERT INTO closed_trades(mode,symbol) VALUES ('live','BTCUSDT');"
        "INSERT INTO kv VALUES ('test','ok');"
    )
    connection.commit()
    connection.close()

    result = backup_database(source, backup, timeout=5, max_seconds=30)
    restored.parent.mkdir(parents=True)
    shutil.copy2(backup, restored)

    with sqlite3.connect(f"file:{restored}?mode=ro", uri=True) as restored_db:
        assert restored_db.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
        assert restored_db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
        assert restored_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
        assert restored_db.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0] == 1

    assert len(result["sha256"]) == 64


def test_backup_database_wal_active_writes_and_sha256(tmp_path):
    source = tmp_path / "wal-source.db"
    destination = tmp_path / "wal-backup.db"
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        "CREATE TABLE positions (symbol TEXT);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY);"
        "CREATE TABLE order_intents (id INTEGER PRIMARY KEY);"
        "CREATE TABLE closed_trades (id INTEGER PRIMARY KEY);"
        "CREATE TABLE kv (key TEXT PRIMARY KEY);"
    )
    connection.commit()
    connection.execute("INSERT INTO kv VALUES ('before')")
    connection.commit()

    result = backup_database(source, destination, timeout=5, max_seconds=30)

    connection.execute("INSERT INTO kv VALUES ('after')")
    connection.commit()

    with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as backup:
        assert backup.execute("PRAGMA journal_mode").fetchone()[0] in {"delete", "wal"}
        assert backup.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
        assert backup.execute("SELECT COUNT(*) FROM kv").fetchone()[0] >= 1
    assert len(result["sha256"]) == 64
    connection.close()


def test_backup_allows_legitimate_live_write_count_drift(tmp_path):
    source = tmp_path / "drift-source.db"
    destination = tmp_path / "drift-backup.db"
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        "CREATE TABLE positions (symbol TEXT);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY);"
        "CREATE TABLE order_intents (id INTEGER PRIMARY KEY);"
        "CREATE TABLE closed_trades (id INTEGER PRIMARY KEY);"
        "CREATE TABLE kv (key TEXT PRIMARY KEY);"
        "INSERT INTO kv VALUES ('before');"
    )
    connection.commit()

    result = backup_database(source, destination, timeout=5, max_seconds=30)

    connection.execute("INSERT INTO kv VALUES ('after')")
    connection.commit()

    assert result["destination_integrity"] == "ok"
    assert destination.exists()
    assert (tmp_path / "drift-backup.db.tmp").exists() is False
    connection.close()


def test_backup_database_timeout_and_cleanup(tmp_path):
    source = tmp_path / "timeout-source.db"
    destination = tmp_path / "timeout-backup.db"
    connection = sqlite3.connect(source)
    connection.executescript(
        "CREATE TABLE positions (symbol TEXT);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY);"
        "CREATE TABLE order_intents (id INTEGER PRIMARY KEY);"
        "CREATE TABLE closed_trades (id INTEGER PRIMARY KEY);"
        "CREATE TABLE kv (key TEXT PRIMARY KEY);"
    )
    connection.commit()

    try:
        backup_database(source, destination, timeout=0.01, max_seconds=1e-9)
    except BackupError:
        pass
    else:
        raise AssertionError("excessively short max_seconds must fail")
    assert not (tmp_path / "timeout-backup.db.tmp").exists()
    assert not destination.exists()
    connection.close()


def test_failed_backup_preserves_existing_valid_destination(tmp_path):
    source = tmp_path / "missing.db"
    destination = tmp_path / "valid-backup.db"
    destination.write_bytes(b"existing-valid-placeholder")

    try:
        backup_database(source, destination)
    except BackupError:
        pass
    else:
        raise AssertionError("missing source must fail")
    assert destination.read_bytes() == b"existing-valid-placeholder"


def test_backup_database_rejects_missing_source(tmp_path):
    try:
        backup_database(tmp_path / "missing.db", tmp_path / "backup.db")
    except BackupError as exc:
        assert "source database not found" in str(exc)
    else:
        raise AssertionError("missing source must fail")
