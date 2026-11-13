"""Archive old ``tick_audit`` rows into a separate SQLite file, in small batches.

DRY-RUN BY DEFAULT: without ``--execute`` the source database is opened
read-only and only counts / size estimates are printed.

Safety model (``--execute``):
  * Two phases per id-range batch, each its own short transaction:
      1. copy the batch into the archive DB (INSERT OR IGNORE, PK = id) and commit;
      2. re-count the batch in the archive, and only if every source row is
         present, delete exactly those ids from the source and commit.
    A crash between phases can only leave duplicates in the archive (harmless,
    ignored on re-run) — never a row that exists in neither file.
  * Never runs VACUUM. Freed pages go to the SQLite freelist and are reused by
    new inserts, so the file stops growing instead of shrinking.
  * Short batches + busy_timeout keep the live bot's writes unblocked.

Usage:
  python -m scripts.archive_tick_audit --db data/cache/trading.db --older-than-days 30
  python -m scripts.archive_tick_audit --db ... --archive /var/backups/tick_audit_archive.db \
      --older-than-days 30 --execute
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("archive_tick_audit")

TABLE = "tick_audit"


def _cutoff_iso(older_than_days: int, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=older_than_days)).isoformat()


def plan(conn: sqlite3.Connection, cutoff: str) -> dict:
    """Read-only summary of what an archive run would move."""
    row = conn.execute(
        f"SELECT COUNT(*), MIN(id), MAX(id), MIN(ts), MAX(ts) FROM {TABLE} WHERE ts < ?",
        (cutoff,),
    ).fetchone()
    total = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    avg = conn.execute(
        f"SELECT AVG(LENGTH(indicators) + LENGTH(filters) + LENGTH(reason) + 80) "
        f"FROM (SELECT indicators, filters, reason FROM {TABLE} WHERE ts < ? LIMIT 2000)",
        (cutoff,),
    ).fetchone()[0] or 0
    return {
        "cutoff": cutoff, "rows_total": total, "rows_to_archive": row[0],
        "min_id": row[1], "max_id": row[2], "oldest_ts": row[3], "newest_ts": row[4],
        "est_bytes": int(row[0] * avg),
    }


def _ensure_archive_table(conn: sqlite3.Connection) -> list[str]:
    cols = [r[1] for r in conn.execute(f"PRAGMA main.table_info({TABLE})")]
    sql = conn.execute(
        "SELECT sql FROM main.sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()[0]
    archive_sql, n = re.subn(
        rf"^\s*CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?[\"\[`]?{TABLE}[\"\]`]?",
        f"CREATE TABLE IF NOT EXISTS archive.{TABLE}", sql, count=1, flags=re.IGNORECASE,
    )
    if n != 1:
        raise RuntimeError(f"unexpected {TABLE} schema: {sql[:80]!r}")
    conn.execute(archive_sql)
    archive_cols = {r[1] for r in conn.execute(f"PRAGMA archive.table_info({TABLE})")}
    for col in cols:
        if col not in archive_cols:  # source gained a column (e.g. tick_id) since archive creation
            conn.execute(f"ALTER TABLE archive.{TABLE} ADD COLUMN {col}")
    conn.execute(f"CREATE INDEX IF NOT EXISTS archive.ix_archive_tick_audit_ts ON {TABLE}(ts)")
    conn.commit()
    return cols


def archive(db_path: Path, archive_path: Path, cutoff: str, batch: int = 5000,
            pause_s: float = 0.2, max_batches: int | None = None) -> dict:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("ATTACH DATABASE ? AS archive", (str(archive_path),))
    copied = deleted = batches = 0
    try:
        conn.execute("BEGIN")
        cols = _ensure_archive_table(conn)
        col_list = ", ".join(cols)
        bounds = conn.execute(
            f"SELECT MIN(id), MAX(id) FROM {TABLE} WHERE ts < ?", (cutoff,)
        ).fetchone()
        if bounds[0] is None:
            return {"copied": 0, "deleted": 0, "batches": 0}
        lo, hi = bounds
        while lo <= hi:
            if max_batches is not None and batches >= max_batches:
                break
            top = lo + batch - 1
            where = "id BETWEEN ? AND ? AND ts < ?"
            args = (lo, top, cutoff)
            # Phase 1: copy + commit.
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                f"INSERT OR IGNORE INTO archive.{TABLE} ({col_list}) "
                f"SELECT {col_list} FROM main.{TABLE} WHERE {where}", args,
            )
            copied += max(cur.rowcount, 0)
            conn.execute("COMMIT")
            # Phase 2: verify + delete only rows proven present in the archive.
            conn.execute("BEGIN IMMEDIATE")
            src = conn.execute(f"SELECT COUNT(*) FROM main.{TABLE} WHERE {where}", args).fetchone()[0]
            same_row = " AND ".join(f"a.{col} IS m.{col}" for col in cols)
            present = conn.execute(
                f"SELECT COUNT(*) FROM main.{TABLE} m JOIN archive.{TABLE} a ON a.id = m.id "
                f"WHERE m.id BETWEEN ? AND ? AND m.ts < ? AND {same_row}", args,
            ).fetchone()[0]
            if present != src:
                conn.execute("ROLLBACK")
                raise RuntimeError(
                    f"archive verification failed for ids {lo}-{top}: {present}/{src} present; "
                    "nothing deleted for this batch"
                )
            cur = conn.execute(
                f"DELETE FROM main.{TABLE} AS m WHERE m.id BETWEEN ? AND ? AND m.ts < ? "
                f"AND EXISTS (SELECT 1 FROM archive.{TABLE} a WHERE a.id = m.id AND {same_row})",
                args,
            )
            deleted += max(cur.rowcount, 0)
            conn.execute("COMMIT")
            batches += 1
            lo = top + 1
            if pause_s:
                time.sleep(pause_s)
        return {"copied": copied, "deleted": deleted, "batches": batches}
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.execute("DETACH DATABASE archive")
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--archive", type=Path, help="archive SQLite file (required with --execute)")
    parser.add_argument("--older-than-days", type=int, default=30)
    parser.add_argument("--batch", type=int, default=5000)
    parser.add_argument("--pause", type=float, default=0.2, help="seconds between batches")
    parser.add_argument("--max-batches", type=int, default=None)
    parser.add_argument("--execute", action="store_true", help="actually move rows (default: dry run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.older_than_days < 7:
        parser.error("--older-than-days must be >= 7 (diagnostics rely on recent tick history)")
    cutoff = _cutoff_iso(args.older_than_days)
    ro = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        summary = plan(ro, cutoff)
    finally:
        ro.close()
    print(f"plan: {summary}")
    if not args.execute:
        print("DRY RUN — nothing written. Re-run with --archive PATH --execute to move rows.")
        return
    if not args.archive:
        parser.error("--archive is required with --execute")
    if args.archive.resolve() == args.db.resolve():
        parser.error("--archive must be a different file from --db")
    result = archive(args.db, args.archive, cutoff, args.batch, args.pause, args.max_batches)
    print(f"done: {result} (no VACUUM: freed pages are reused by new rows)")


if __name__ == "__main__":
    main()
