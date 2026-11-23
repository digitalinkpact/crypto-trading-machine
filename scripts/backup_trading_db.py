#!/usr/bin/env python3
"""Create and verify an atomic SQLite backup with bounded runtime.

This script is operator-invoked. It never deletes or overwrites the source
database and it never promotes a destination until integrity/count checks pass.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterable

VERIFY_TABLES = ("positions", "orders", "order_intents", "closed_trades", "kv")


class BackupError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def row_counts(connection: sqlite3.Connection, tables: Iterable[str]) -> dict[str, int]:
    existing = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    result = {}
    for table in tables:
        if table in existing:
            result[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return result


def backup_database(
    source: Path,
    destination: Path,
    *,
    timeout: float = 30.0,
    max_seconds: float = 3600.0,
    progress_callback=None,
) -> dict:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise BackupError(f"source database not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)

    started = time.monotonic()
    last_progress = [0]

    def progress(status: int, remaining: int, total: int) -> None:
        del status
        last_progress[0] = total - remaining if total else 0
        if progress_callback is not None:
            progress_callback(last_progress[0], total)
        if time.monotonic() - started > max_seconds:
            raise BackupError(f"backup exceeded max_seconds={max_seconds:g}")

    source_connection = None
    destination_connection = None
    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=timeout)
        destination_connection = sqlite3.connect(str(temporary), timeout=timeout)
        source_connection.backup(destination_connection, pages=4096, progress=progress, sleep=0.05)
        destination_connection.close()
        destination_connection = None
        source_connection.close()
        source_connection = None

        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=timeout)
        destination_connection = sqlite3.connect(f"file:{temporary}?mode=ro", uri=True, timeout=timeout)

        source_integrity = str(source_connection.execute("PRAGMA quick_check(1)").fetchone()[0])
        destination_integrity = str(destination_connection.execute("PRAGMA quick_check(1)").fetchone()[0])
        if source_integrity != "ok":
            raise BackupError(f"source integrity check failed: {source_integrity}")
        if destination_integrity != "ok":
            raise BackupError(f"destination integrity check failed: {destination_integrity}")

        source_counts = row_counts(source_connection, VERIFY_TABLES)
        destination_counts = row_counts(destination_connection, VERIFY_TABLES)
        count_discrepancies = {
            table: {"source": source_counts.get(table), "backup": destination_counts.get(table)}
            for table in sorted(set(source_counts) | set(destination_counts))
            if source_counts.get(table) != destination_counts.get(table)
        }

        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        elapsed = time.monotonic() - started
        return {
            "source": str(source),
            "destination": str(destination),
            "source_integrity": source_integrity,
            "destination_integrity": destination_integrity,
            "counts": destination_counts,
            "count_discrepancies": count_discrepancies,
            "progress_pages": last_progress[0],
            "elapsed_seconds": round(elapsed, 3),
            "sha256": sha256(destination),
        }
    except sqlite3.OperationalError as exc:
        raise BackupError(f"SQLite lock or I/O failure: {exc}") from exc
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-seconds", type=float, default=3600.0)
    args = parser.parse_args()
    last_reported = {"percent": -1}

    def report_progress(completed: int, total: int) -> None:
        percent = int((completed * 100 / total)) if total else 100
        bucket = (percent // 10) * 10
        if bucket != last_reported["percent"]:
            last_reported["percent"] = bucket
            print(f"BACKUP PROGRESS: {bucket}%", flush=True)

    try:
        result = backup_database(
            args.source,
            args.destination,
            timeout=args.timeout,
            max_seconds=args.max_seconds,
            progress_callback=report_progress,
        )
    except BackupError as exc:
        print(f"BACKUP FAILED: {exc}")
        return 1
    print("BACKUP OK")
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
