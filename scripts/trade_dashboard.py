#!/usr/bin/env python3
"""Read-only trade dashboard: `python -m scripts.trade_dashboard [--mode live]`.

BUY/SELL counts are exchange fills only. Orders submitted but not filled are
counted separately, and internal gate rejections are labelled as internal,
never as Binance rejections. Pytest fixture rows are excluded.
"""
import argparse
import asyncio
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone

from app.logging_setup import get_logger
from app.trading import audit_labels
from app.trading.win_rate import format_win_rate_block, open_position_keys, win_rate_summary

log = get_logger(__name__)


def win_rate_lines(conn, mode: str) -> list[str]:
    trades = [dict(r) for r in conn.execute(
        "SELECT * FROM closed_trades WHERE mode=? ORDER BY id DESC LIMIT 5000", (mode,))]
    positions = [dict(r) for r in conn.execute("SELECT * FROM positions WHERE mode=?", (mode,))]
    return format_win_rate_block(win_rate_summary(trades, open_position_keys(positions)))


class TradeMonitor:
    def __init__(self, mode: str = "live"):
        self.mode = mode
        self.last_id = 0
        self.filled = defaultdict(int)          # signal -> filled count
        self.not_filled = defaultdict(int)      # exchange status -> count
        self.internal_reasons = defaultdict(int)
        self.recent = deque(maxlen=10)

    def ingest(self, rows) -> None:
        for row in reversed(rows):
            stage = audit_labels.execution_stage(row)
            if stage == audit_labels.FILLED:
                self.filled[row.get("signal")] += 1
            elif stage == audit_labels.SUBMITTED_NOT_FILLED:
                self.not_filled[str(row.get("binance_response"))] += 1
            elif stage == audit_labels.INTERNAL_REJECTED:
                self.internal_reasons[audit_labels.rejection_reason(row)] += 1
            self.recent.append((row.get("symbol", "?"), row.get("signal", "?"), stage))
            self.last_id = max(self.last_id, int(row["id"]))

    def render(self, summary_lines=()) -> list[str]:
        lines = [f"[{datetime.now(timezone.utc):%H:%M:%S} UTC] TRADE EXECUTION STATS mode={self.mode}",
                 f"  BUY filled on Binance.US:   {self.filled['BUY']:3}",
                 f"  SELL filled on Binance.US:  {self.filled['SELL']:3}",
                 f"  Submitted, not filled:      {sum(self.not_filled.values()):3} {dict(self.not_filled) or ''}",
                 ""]
        lines.extend(f"  {line}" for line in summary_lines)
        lines.append("\n  Top internal gate reasons (not sent to Binance.US):")
        for reason, count in sorted(self.internal_reasons.items(), key=lambda x: -x[1])[:5]:
            lines.append(f"    - {reason:28} {count:5}x")
        if self.recent:
            lines.append("\n  Recent activity (last 5):")
            for sym, sig, stage in list(self.recent)[-5:]:
                lines.append(f"    {sym:12} {sig:4} {audit_labels.STAGE_LABELS[stage]}")
        return lines

    async def run(self, interval: int = 30):
        while True:
            try:
                with audit_labels.connect_readonly() as conn:
                    self.ingest(audit_labels.fetch_trade_audit(
                        conn, mode=self.mode, after_id=self.last_id, limit=1000))
                    summary = win_rate_lines(conn, self.mode)
                print("\n" + "\n".join(self.render(summary)))
            except Exception as exc:  # noqa: BLE001 - keep monitoring through transient DB errors
                log.error("Error: %s", exc)
            await asyncio.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="live", choices=("live", "paper"))
    args = parser.parse_args()
    print("=" * 70 + "\nTRADE EXECUTION DASHBOARD (read-only)\n" + "=" * 70)
    try:
        asyncio.run(TradeMonitor(args.mode).run())
    except KeyboardInterrupt:
        sys.exit(0)
