"""SQLite persistence — orders, positions, closed trades, agent stats.

All writes go through `Storage` so they're easy to mock and observe. The DB
file lives under `data/trading.db` next to the OHLCV cache.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    client_order_id TEXT,
    agents TEXT
);
CREATE INDEX IF NOT EXISTS ix_orders_ts ON orders(ts);

CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_ts TEXT NOT NULL,
    agents TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS closed_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    pnl REAL NOT NULL,
    pnl_pct REAL NOT NULL,
    entry_ts TEXT NOT NULL,
    exit_ts TEXT NOT NULL,
    agents TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_closed_trades_exit ON closed_trades(exit_ts);

CREATE TABLE IF NOT EXISTS agent_stats (
    agent TEXT PRIMARY KEY,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    total_pnl REAL NOT NULL DEFAULT 0,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS paper_balances (
    asset TEXT PRIMARY KEY,
    qty REAL NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(x: Any) -> float:
    if isinstance(x, Decimal):
        return float(x)
    return float(x)


class Storage:
    """Thread-safe SQLite helper. Synchronous; call from async via to_thread."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or (get_settings().data_cache_dir / "trading.db")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._path, isolation_level=None)  # autocommit
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA foreign_keys=ON;")
        return c

    def _init(self) -> None:
        with self._lock, self._conn() as c:
            c.executescript(_SCHEMA)

    # ── KV (used for autopilot state) ────────────────────────────────
    def kv_set(self, key: str, value: Any) -> None:
        payload = json.dumps(value, default=str)
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, payload),
            )

    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    # ── Orders ───────────────────────────────────────────────────────
    def record_order(
        self,
        *,
        mode: str,
        symbol: str,
        side: str,
        qty: Decimal | float,
        price: Decimal | float,
        fee: Decimal | float = 0,
        client_order_id: Optional[str] = None,
        agents: Optional[Iterable[str]] = None,
    ) -> int:
        agents_json = json.dumps(list(agents or []))
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO orders(ts,mode,symbol,side,qty,price,fee,client_order_id,agents) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (_now(), mode, symbol, side, _f(qty), _f(price), _f(fee),
                 client_order_id, agents_json),
            )
            return int(cur.lastrowid or 0)

    def recent_orders(self, limit: int = 100) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Positions ────────────────────────────────────────────────────
    def open_position(
        self,
        *,
        symbol: str,
        mode: str,
        qty: Decimal | float,
        entry_price: Decimal | float,
        agents: Iterable[str],
    ) -> None:
        agents_json = json.dumps(list(agents))
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO positions(symbol,mode,qty,entry_price,entry_ts,agents) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET qty=qty+excluded.qty, "
                "entry_price=((qty*entry_price + excluded.qty*excluded.entry_price)/"
                "(qty+excluded.qty)) ",
                (symbol, mode, _f(qty), _f(entry_price), _now(), agents_json),
            )

    def get_position(self, symbol: str) -> Optional[dict]:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
        return dict(row) if row else None

    def all_positions(self) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM positions").fetchall()
        return [dict(r) for r in rows]

    def close_position(
        self,
        *,
        symbol: str,
        exit_price: Decimal | float,
    ) -> Optional[dict]:
        """Close a position fully. Records to closed_trades and updates agent stats."""
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
            if not row:
                return None
            qty = float(row["qty"])
            entry = float(row["entry_price"])
            exit_p = _f(exit_price)
            pnl = (exit_p - entry) * qty
            pnl_pct = ((exit_p - entry) / entry * 100) if entry else 0.0
            agents_json = row["agents"]
            agents_list = json.loads(agents_json or "[]")
            entry_ts = row["entry_ts"]
            mode = row["mode"]
            now = _now()
            c.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
            c.execute(
                "INSERT INTO closed_trades(mode,symbol,qty,entry_price,exit_price,pnl,"
                "pnl_pct,entry_ts,exit_ts,agents) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (mode, symbol, qty, entry, exit_p, pnl, pnl_pct,
                 entry_ts, now, agents_json),
            )
            won = pnl > 0
            for agent in agents_list:
                c.execute(
                    "INSERT INTO agent_stats(agent,wins,losses,total_pnl,last_updated) "
                    "VALUES(?,?,?,?,?) "
                    "ON CONFLICT(agent) DO UPDATE SET "
                    "wins=wins+excluded.wins, losses=losses+excluded.losses, "
                    "total_pnl=total_pnl+excluded.total_pnl, last_updated=excluded.last_updated",
                    (agent, 1 if won else 0, 0 if won else 1, pnl, now),
                )
        return {
            "symbol": symbol, "qty": qty, "entry_price": entry,
            "exit_price": exit_p, "pnl": pnl, "pnl_pct": pnl_pct,
            "agents": agents_list, "entry_ts": entry_ts, "exit_ts": now,
            "mode": mode,
        }

    # ── Agent stats ──────────────────────────────────────────────────
    def agent_stats(self) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM agent_stats ORDER BY total_pnl DESC"
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            n = d["wins"] + d["losses"]
            d["win_rate"] = (d["wins"] / n) if n else 0.0
            d["total_trades"] = n
            out.append(d)
        return out

    def closed_trades(self, limit: int = 100) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM closed_trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Paper balances ───────────────────────────────────────────────
    def paper_balance_set(self, asset: str, qty: Decimal | float) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO paper_balances(asset,qty) VALUES(?,?) "
                "ON CONFLICT(asset) DO UPDATE SET qty=excluded.qty",
                (asset, _f(qty)),
            )

    def paper_balance_add(self, asset: str, delta: Decimal | float) -> float:
        delta_f = _f(delta)
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO paper_balances(asset,qty) VALUES(?,?) "
                "ON CONFLICT(asset) DO UPDATE SET qty=qty+excluded.qty",
                (asset, delta_f),
            )
            row = c.execute("SELECT qty FROM paper_balances WHERE asset=?", (asset,)).fetchone()
        return float(row["qty"]) if row else 0.0

    def paper_balance_get(self, asset: str) -> float:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT qty FROM paper_balances WHERE asset=?", (asset,)).fetchone()
        return float(row["qty"]) if row else 0.0

    def paper_balances(self) -> dict[str, float]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM paper_balances WHERE qty > 0").fetchall()
        return {r["asset"]: float(r["qty"]) for r in rows}

    def paper_reset(self, starting_usdt: float = 10_000.0) -> None:
        """Wipe paper balances and seed with USDT. Does not touch trade history."""
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM paper_balances")
            c.execute("INSERT INTO paper_balances(asset,qty) VALUES('USDT', ?)",
                      (starting_usdt,))


storage = Storage()
