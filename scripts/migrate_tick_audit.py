"""One-off migration helper to ensure tick_audit exists.

Usage:
    python -m scripts.migrate_tick_audit
"""
from __future__ import annotations

from app.storage import storage


def main() -> None:
    # Trigger DB init path and assert the table is queryable.
    rows = storage.recent_tick_audit(limit=1)
    print(f"tick_audit ready (rows={len(rows)})")


if __name__ == "__main__":
    main()
