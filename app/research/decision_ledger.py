"""Stage 6 — Decision ledger for weekly verdict cards.

Persists every ``VerdictCard`` produced by Stage 5 into an ``experiment_decisions``
SQLite table so a human can audit the sequence of recommendations over time.
The ledger writes ONLY to its own table and never touches ``orders``,
``closed_trades``, or any configuration.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.config import get_settings
from app.logging_setup import get_logger
from app.notify import notify as notify_fn
from app.research.experiment_verdict import VerdictCard

log = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    overall TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    concerns_json TEXT NOT NULL,
    recommendations_json TEXT NOT NULL,
    inputs_present_json TEXT NOT NULL,
    notification_channel TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_experiment_decisions_ts ON experiment_decisions(ts);
CREATE INDEX IF NOT EXISTS ix_experiment_decisions_overall ON experiment_decisions(overall);
"""

# Only these outcomes cause a notification. KEEP_TESTING would spam every week.
_NOTIFY_ON = {"PAUSE", "REJECT", "PROMOTE"}


@dataclass
class DecisionRow:
    id: int
    ts: str
    overall: str
    reasons: list[str]
    concerns: list[str]
    recommendations: list[str]
    inputs_present: dict[str, bool]
    notification_channel: str


class DecisionLedger:
    """Append-only ledger of Stage 5 verdicts."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or (get_settings().data_cache_dir / "trading.db")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._lock, self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._path, isolation_level=None)
        c.row_factory = sqlite3.Row
        return c

    def record(self, card: VerdictCard, *, channel: str = "") -> int:
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO experiment_decisions
                    (ts, overall, reasons_json, concerns_json,
                     recommendations_json, inputs_present_json,
                     notification_channel)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    card.overall,
                    json.dumps(card.reasons),
                    json.dumps(card.concerns),
                    json.dumps(card.recommendations),
                    json.dumps(card.inputs_present),
                    channel,
                ),
            )
            return int(cur.lastrowid or 0)

    def list_recent(self, limit: int = 20) -> list[DecisionRow]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT id, ts, overall, reasons_json, concerns_json, "
                "recommendations_json, inputs_present_json, notification_channel "
                "FROM experiment_decisions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            DecisionRow(
                id=int(r["id"]),
                ts=str(r["ts"]),
                overall=str(r["overall"]),
                reasons=list(json.loads(r["reasons_json"] or "[]")),
                concerns=list(json.loads(r["concerns_json"] or "[]")),
                recommendations=list(json.loads(r["recommendations_json"] or "[]")),
                inputs_present=dict(json.loads(r["inputs_present_json"] or "{}")),
                notification_channel=str(r["notification_channel"] or ""),
            )
            for r in rows
        ]


def format_notification(card: VerdictCard) -> tuple[str, str]:
    """Human-readable subject + body pair for a verdict card."""
    subject = f"[trading-machine] Weekly verdict: {card.overall}"
    lines = [f"Verdict: {card.overall}", ""]
    if card.reasons:
        lines.append("Reasons:")
        lines += [f"  - {r}" for r in card.reasons]
        lines.append("")
    if card.concerns:
        lines.append("Concerns:")
        lines += [f"  - {c}" for c in card.concerns]
        lines.append("")
    if card.recommendations:
        lines.append("Recommended actions (human decides):")
        for i, r in enumerate(card.recommendations, 1):
            lines.append(f"  {i}. {r}")
        lines.append("")
    missing = [k for k, v in card.inputs_present.items() if not v]
    if missing:
        lines.append(f"Missing inputs: {', '.join(missing)}")
    return subject, "\n".join(lines).rstrip() + "\n"


def record_and_notify(
    card: VerdictCard,
    *,
    ledger: Optional[DecisionLedger] = None,
    notifier: Any = None,
) -> DecisionRow:
    """Persist the card, notify on notable outcomes, and return the stored row.

    Notification is skipped for ``KEEP_TESTING`` to avoid weekly spam. The
    notifier callable defaults to ``app.notify.notify`` and must never raise.
    """
    subject, body = format_notification(card)
    channel = ""
    if card.overall in _NOTIFY_ON:
        try:
            channel = (notifier or notify_fn)(subject, body) or "log"
        except Exception as exc:  # noqa: BLE001
            log.warning("notifier raised (%s); recording as 'error'", exc)
            channel = "error"

    ledger = ledger or DecisionLedger()
    row_id = ledger.record(card, channel=channel)
    log.info(
        "decision ledger: id=%s overall=%s channel=%s",
        row_id, card.overall, channel or "(none)",
    )
    latest = ledger.list_recent(limit=1)
    return latest[0]
