import sqlite3

import pytest

from app.storage.db import Storage
from scripts.archive_tick_audit import archive, plan


def _seed(path, n=12):
    Storage(path)  # real production schema
    conn = sqlite3.connect(path)
    for i in range(1, n + 1):
        ts = f"2026-01-{i:02d}T00:00:00+00:00"
        conn.execute(
            "INSERT INTO tick_audit (id, ts, mode, symbol, timeframe, action, score, executed, "
            "reason, indicators, filters) VALUES (?, ?, 'live', 'BTCUSDT', '1d', 'HOLD', 10, 0, "
            "'r', '{}', '{}')",
            (i, ts),
        )
    conn.commit()
    conn.close()


def _ids(path, table="tick_audit"):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute(f"SELECT id FROM {table} ORDER BY id")]
    finally:
        conn.close()


def test_archive_moves_only_old_rows_and_is_idempotent(tmp_path):
    src, arc = tmp_path / "src.db", tmp_path / "arc.db"
    _seed(src)
    cutoff = "2026-01-08T00:00:00+00:00"
    ro = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    assert plan(ro, cutoff)["rows_to_archive"] == 7
    ro.close()

    result = archive(src, arc, cutoff, batch=3, pause_s=0)
    assert result == {"copied": 7, "deleted": 7, "batches": 3}
    assert _ids(src) == list(range(8, 13))
    assert _ids(arc) == list(range(1, 8))
    assert archive(src, arc, cutoff, batch=3, pause_s=0) == {"copied": 0, "deleted": 0, "batches": 0}


def test_archive_never_deletes_rows_missing_from_archive(tmp_path):
    src, arc = tmp_path / "src.db", tmp_path / "arc.db"
    _seed(src)
    # Archive silently refuses id 5 (INSERT OR IGNORE skips CHECK failures).
    conn = sqlite3.connect(arc)
    conn.execute(
        "CREATE TABLE tick_audit (id INTEGER PRIMARY KEY CHECK (id != 5), ts TEXT, mode TEXT, "
        "symbol TEXT, timeframe TEXT, action TEXT, score INTEGER, executed INTEGER, reason TEXT, "
        "indicators TEXT, filters TEXT)"
    )
    conn.close()

    with pytest.raises(RuntimeError, match="verification failed"):
        archive(src, arc, "2026-01-08T00:00:00+00:00", batch=3, pause_s=0)
    # Batch 1-3 moved; batch 4-6 failed verification so none of it was deleted.
    assert _ids(src) == list(range(4, 13))
    assert 5 not in _ids(arc)


def test_archive_never_deletes_a_row_whose_archived_copy_differs(tmp_path):
    src, arc = tmp_path / "src.db", tmp_path / "arc.db"
    _seed(src, n=3)
    cutoff = "2026-01-08T00:00:00+00:00"
    archive(src, arc, cutoff, batch=10, pause_s=0, max_batches=0)  # creates the archive table only
    conn = sqlite3.connect(arc)
    conn.execute("INSERT INTO tick_audit (id, ts, mode, symbol, timeframe, action, score, executed, "
                 "reason, indicators, filters) VALUES (2, '2026-01-02T00:00:00+00:00', 'live', "
                 "'BTCUSDT', '1d', 'HOLD', 10, 0, 'stale', '{}', '{}')")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="verification failed"):
        archive(src, arc, cutoff, batch=10, pause_s=0)
    assert _ids(src) == [1, 2, 3]
