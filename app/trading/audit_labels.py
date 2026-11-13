"""Honest classification of `trade_audit` rows for monitoring and dashboards.

`trade_audit.binance_response` historically held `REJECTED` for decisions the
bot's own gates refused (risk manager, ML gate, regime, halts...). None of
those ever reached Binance.US. Monitoring must never present them as exchange
rejections, and pytest fixture rows (detail.agents == ["test"]) that older test
runs wrote into the live database must never count as live activity.

Execution stages (in pipeline order):
  TEST_FIXTURE        pytest row; excluded from live reporting
  INTERNAL_REJECTED   blocked by an internal gate; nothing sent to the exchange
  SIMULATED           paper / dry-run order; nothing sent to the exchange
  SUBMITTED_NOT_FILLED  reached Binance.US but did not fill (NEW/CANCELED/
                      EXPIRED/exchange REJECTED); fills are never assumed
  FILLED              local order path observed an exchange fill; independent
                      confirmation comes from app/trading/fill_reconcile.py
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Optional

TEST_FIXTURE = "TEST_FIXTURE"
INTERNAL_REJECTED = "INTERNAL_REJECTED"
SIMULATED = "SIMULATED"
SUBMITTED_NOT_FILLED = "SUBMITTED_NOT_FILLED"
FILLED = "FILLED"

STAGE_LABELS = {
    TEST_FIXTURE: "test fixture (not live)",
    INTERNAL_REJECTED: "internal gate - not sent to Binance.US",
    SIMULATED: "simulated (paper/dry-run) - not sent to Binance.US",
    SUBMITTED_NOT_FILLED: "submitted to Binance.US - not filled",
    FILLED: "filled on Binance.US",
}

# Agents used by operators for deliberate, REAL exchange test orders. They are
# genuine fills, so they are flagged rather than hidden.
OPERATOR_TEST_AGENTS = frozenset({"manual_test", "tick_test"})
PYTEST_FIXTURE_AGENTS = ["test"]

# SQL predicate matching pytest fixture rows in trade_audit (alias-free).
# `IS` (not `=`) so a missing agents key yields false rather than NULL; with
# NULL, `NOT (...)` would silently drop every genuine row lacking the key.
SQL_IS_TEST_FIXTURE = "(json_valid(detail) AND json_extract(detail, '$.agents') IS '[\"test\"]')"

_FILLED_RESPONSES = {"FILLED", "PARTIALLY_FILLED", "SUCCESS"}
_SIMULATED_RESPONSES = {"DRY_RUN"}


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _detail(row: Mapping[str, Any]) -> dict:
    raw = _get(row, "detail")
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _agents(value: Any) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value]
    return list(value) if isinstance(value, (list, tuple)) else []


def is_test_fixture(row: Mapping[str, Any]) -> bool:
    return _agents(_detail(row).get("agents")) == PYTEST_FIXTURE_AGENTS


def is_operator_test_order(row: Mapping[str, Any]) -> bool:
    """True for an `orders` row placed by an operator's deliberate live test."""
    return bool(OPERATOR_TEST_AGENTS.intersection(_agents(_get(row, "agents"))))


def execution_stage(row: Mapping[str, Any]) -> str:
    """Map one trade_audit row (dict/sqlite Row) to an execution stage."""
    if is_test_fixture(row):
        return TEST_FIXTURE
    response = str(_get(row, "binance_response") or "").strip().upper()
    attempted = bool(_get(row, "execution_attempted"))
    mode = str(_get(row, "mode") or "")
    if response in _SIMULATED_RESPONSES:
        return SIMULATED
    if not attempted:
        # Legacy rows used REJECTED/""/NONE for internal decisions; newer rows
        # write NOT_SUBMITTED. Without an attempt nothing reached the exchange.
        return INTERNAL_REJECTED
    if response in _FILLED_RESPONSES:
        return SIMULATED if mode == "paper" else FILLED
    if mode == "paper":
        return SIMULATED
    return SUBMITTED_NOT_FILLED


def stage_label(row: Mapping[str, Any]) -> str:
    return STAGE_LABELS[execution_stage(row)]


def rejection_reason(row: Mapping[str, Any]) -> str:
    """Gate name for an internal rejection (e.g. `risk_manager`)."""
    outcome = str(_get(row, "final_outcome") or "")
    if outcome.startswith("rejected:"):
        outcome = outcome.split(":", 1)[1].strip()
    return outcome.split(":", 1)[0].strip() or "unknown"


def summarize(rows) -> dict[str, int]:
    """Count rows per stage; test fixtures are counted separately, never as live."""
    counts = {stage: 0 for stage in STAGE_LABELS}
    for row in rows:
        counts[execution_stage(row)] += 1
    counts["submitted_total"] = counts[SUBMITTED_NOT_FILLED] + counts[FILLED]
    return counts


def default_db_path() -> Path:
    """Same location `app.storage.Storage` uses, without opening it for writing."""
    from app.config import get_settings

    settings = get_settings()
    directory = settings.data_cache_dir
    if settings.paper_trading:
        directory = directory / "simulation"
    return Path(directory) / "trading.db"


def connect_readonly(path: Optional[Path] = None) -> sqlite3.Connection:
    """Open the trading DB read-only so monitors can never migrate or write it."""
    db = Path(path or default_db_path()).resolve()
    if not db.exists():
        raise FileNotFoundError(f"trading DB not found: {db}")
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_trade_audit(
    conn: sqlite3.Connection,
    *,
    mode: Optional[str],
    after_id: int = 0,
    limit: int = 100,
    include_test: bool = False,
    signal: Optional[str] = None,
    symbol: Optional[str] = None,
) -> list[dict]:
    """Newest-first trade_audit rows; pytest fixture rows hidden unless asked."""
    clauses = ["id > ?"]
    params: list[Any] = [int(after_id)]
    if mode:
        clauses.append("mode = ?")
        params.append(mode)
    if signal:
        clauses.append("signal = ?")
        params.append(signal)
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)
    if not include_test:
        clauses.append(f"NOT {SQL_IS_TEST_FIXTURE}")
    params.append(int(limit))
    rows = conn.execute(
        f"SELECT * FROM trade_audit WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?", params,
    ).fetchall()
    return [dict(row) for row in rows]


def count_test_fixture_rows(conn: sqlite3.Connection, mode: Optional[str] = None) -> int:
    """Count pytest rows. Uses the detail JSON, so it scans; call it occasionally."""
    sql = f"SELECT COUNT(*) FROM trade_audit WHERE {SQL_IS_TEST_FIXTURE}"
    params: tuple = ()
    if mode:
        sql += " AND mode = ?"
        params = (mode,)
    return int(conn.execute(sql, params).fetchone()[0])
