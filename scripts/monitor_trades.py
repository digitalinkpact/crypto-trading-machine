#!/usr/bin/env python3
"""Poll trade decisions every 30s (read-only): `python -m scripts.monitor_trades`.

Filled orders, orders submitted but not filled, and internal gate rejections
are counted separately. Pytest fixture rows are excluded.
"""
import argparse
import asyncio
import sys
from collections import deque
from datetime import datetime, timezone

from app.logging_setup import get_logger
from app.trading import audit_labels

log = get_logger(__name__)


class TradeFeed:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.last_id = 0
        self.counts = {stage: 0 for stage in audit_labels.STAGE_LABELS}
        self.recent: deque[str] = deque(maxlen=20)

    def poll(self, conn) -> None:
        rows = audit_labels.fetch_trade_audit(conn, mode=self.mode, after_id=self.last_id, limit=500)
        for row in reversed(rows):
            stage = audit_labels.execution_stage(row)
            self.counts[stage] += 1
            detail = (audit_labels.rejection_reason(row) if stage == audit_labels.INTERNAL_REJECTED
                      else row.get("binance_response"))
            self.recent.append(f"{row.get('ts', '')} [{row.get('symbol', '?')}] {row.get('signal', '?'):4} "
                               f"{audit_labels.STAGE_LABELS[stage]} ({detail})")
            if stage == audit_labels.FILLED:
                log.info("FILLED: %s %s", row.get("symbol"), row.get("signal"))
            self.last_id = max(self.last_id, int(row["id"]))

    def render(self) -> list[str]:
        c = self.counts
        lines = [
            f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC] mode={self.mode}",
            f"  filled on Binance.US:            {c[audit_labels.FILLED]}",
            f"  submitted, not filled:           {c[audit_labels.SUBMITTED_NOT_FILLED]}",
            f"  internal gate (not submitted):   {c[audit_labels.INTERNAL_REJECTED]}",
            f"  simulated (paper/dry-run):       {c[audit_labels.SIMULATED]}",
        ]
        if self.recent:
            lines.append("  Recent activity:")
            lines.extend(f"    {entry}" for entry in list(self.recent)[-5:])
        else:
            lines.append("  (no decisions yet)")
        return lines


async def monitor(mode: str, interval: int) -> None:
    feed = TradeFeed(mode)
    while True:
        try:
            with audit_labels.connect_readonly() as conn:
                feed.poll(conn)
            print("\n" + "\n".join(feed.render()))
        except Exception as exc:  # noqa: BLE001 - keep monitoring through transient DB errors
            log.error("Monitor error: %s", exc)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="live", choices=("live", "paper"))
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()
    print("=" * 70 + "\nTRADE EXECUTION MONITOR (read-only)\n" + "=" * 70)
    try:
        asyncio.run(monitor(args.mode, args.interval))
    except KeyboardInterrupt:
        sys.exit(0)
