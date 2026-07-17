"""SQLite persistence — orders, positions, closed trades, agent stats.

All writes go through `Storage` so they're easy to mock and observe. The DB
file lives under `data/trading.db` next to the OHLCV cache.
"""
from __future__ import annotations

import json
import pickle
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
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_ts TEXT NOT NULL,
    agents TEXT NOT NULL,
    PRIMARY KEY (symbol, mode)
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

CREATE TABLE IF NOT EXISTS ml_signal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence REAL NOT NULL,
    entry_price REAL NOT NULL,
    atr_pct REAL,
    rsi_14 REAL,
    ema_gap_pct REAL,
    agent_count INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,
    resolved_ts TEXT,
    horizon_minutes INTEGER,
    outcome_return_pct REAL,
    outcome_win INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ml_signal_events_pending ON ml_signal_events(resolved, ts);

CREATE TABLE IF NOT EXISTS ml_models (
    name TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    trained_at TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    metrics TEXT NOT NULL,
    payload BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_balances (
    asset TEXT PRIMARY KEY,
    qty REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    total_usdt REAL NOT NULL,
    cash_usdt REAL NOT NULL,
    invested_usdt REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_equity_ts ON equity_snapshots(ts);

-- ── Auth: single-owner login, sessions, tokens, audit log ───────────
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    email_verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    purpose TEXT NOT NULL,            -- 'verify' | 'reset'
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_auth_tokens_user ON auth_tokens(user_id, purpose);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id INTEGER,
    ip TEXT,
    action TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);
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
            # Historical schema keyed positions by symbol only, which let paper
            # and live rows collide and made live sell eligibility depend on
            # whichever mode wrote last. Migrate in place to mode-scoped rows.
            cols = c.execute("PRAGMA table_info(positions)").fetchall()
            pk_cols = [r["name"] for r in cols if int(r["pk"] or 0) > 0]
            if pk_cols == ["symbol"]:
                c.execute("ALTER TABLE positions RENAME TO positions_old")
                c.execute(
                    "CREATE TABLE positions ("
                    "symbol TEXT NOT NULL,"
                    "mode TEXT NOT NULL,"
                    "qty REAL NOT NULL,"
                    "entry_price REAL NOT NULL,"
                    "entry_ts TEXT NOT NULL,"
                    "agents TEXT NOT NULL,"
                    "PRIMARY KEY (symbol, mode)"
                    ")"
                )
                c.execute(
                    "INSERT INTO positions(symbol, mode, qty, entry_price, entry_ts, agents) "
                    "SELECT symbol, mode, qty, entry_price, entry_ts, agents FROM positions_old"
                )
                c.execute("DROP TABLE positions_old")

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

    # ── Cross-process mutex (kv-backed) ──────────────────────────────
    def try_acquire_lock(self, name: str, ttl_seconds: float, owner: str) -> bool:
        """Best-effort cross-process mutex stored in the kv table.

        Returns ``True`` if the lock was acquired (or a previous holder's lease
        expired), ``False`` if another owner currently holds it. The ``BEGIN
        IMMEDIATE`` transaction makes acquisition atomic across processes so two
        app instances sharing this DB cannot both run a guarded section (e.g. an
        autopilot tick) at the same time. ``ttl_seconds`` is a crash safety net;
        callers should still release explicitly in a ``finally`` block.
        """
        import time

        key = f"lock:{name}"
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
                if row:
                    try:
                        data = json.loads(row["value"])
                        if float(data.get("expires", 0)) > now and data.get("owner") != owner:
                            c.execute("COMMIT")
                            return False
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass  # corrupt lock row — treat as free
                payload = json.dumps({"owner": owner, "expires": now + ttl_seconds})
                c.execute(
                    "INSERT INTO kv(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, payload),
                )
                c.execute("COMMIT")
                return True
            except Exception:
                c.execute("ROLLBACK")
                raise

    def release_lock(self, name: str, owner: str) -> None:
        """Release a lock previously acquired by ``owner`` (no-op otherwise)."""
        key = f"lock:{name}"
        with self._lock, self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if not row:
                return
            try:
                data = json.loads(row["value"])
                if data.get("owner") != owner:
                    return  # held by someone else — don't steal it
            except json.JSONDecodeError:
                pass
            c.execute("DELETE FROM kv WHERE key=?", (key,))

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
                "ON CONFLICT(symbol,mode) DO UPDATE SET qty=qty+excluded.qty, "
                "entry_price=((qty*entry_price + excluded.qty*excluded.entry_price)/"
                "(qty+excluded.qty)) ",
                (symbol, mode, _f(qty), _f(entry_price), _now(), agents_json),
            )

    def get_position(self, symbol: str) -> Optional[dict]:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM positions WHERE symbol=? ORDER BY CASE mode WHEN 'live' THEN 0 ELSE 1 END LIMIT 1",
                (symbol,),
            ).fetchone()
        return dict(row) if row else None

    def all_positions(self) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM positions").fetchall()
        return [dict(r) for r in rows]

    def close_position(
        self,
        *,
        symbol: str,
        mode: Optional[str] = None,
        exit_price: Decimal | float,
    ) -> Optional[dict]:
        """Close a position fully. Records to closed_trades and updates agent stats."""
        with self._lock, self._conn() as c:
            if mode is None:
                row = c.execute(
                    "SELECT * FROM positions WHERE symbol=? "
                    "ORDER BY CASE mode WHEN 'live' THEN 0 ELSE 1 END LIMIT 1",
                    (symbol,),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT * FROM positions WHERE symbol=? AND mode=?",
                    (symbol, mode),
                ).fetchone()
            if not row:
                return None
            qty = float(row["qty"])
            entry = float(row["entry_price"])
            exit_p = _f(exit_price)
            # Closed-trade PnL should reflect execution costs so diagnostics,
            # adaptive weights, and win/loss labels track real net edge.
            s = get_settings()
            taker_fee = float(s.binance_taker_fee)
            gross_pnl = (exit_p - entry) * qty
            est_fees = (entry * qty * taker_fee) + (exit_p * qty * taker_fee)
            pnl = gross_pnl - est_fees
            entry_notional = entry * qty
            pnl_pct = ((pnl / entry_notional) * 100) if entry_notional else 0.0
            agents_json = row["agents"]
            agents_list = json.loads(agents_json or "[]")
            entry_ts = row["entry_ts"]
            mode = row["mode"]
            now = _now()
            c.execute("DELETE FROM positions WHERE symbol=? AND mode=?", (symbol, mode or row["mode"]))
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

    def agent_win_rates(self, min_trades: int = 5) -> dict[str, float]:
        """Return {agent: win_rate} for agents with >= min_trades closed trades.

        Used by the SignalAggregator to scale each agent's vote by its rolling
        track record. Agents with too few trades return no entry (caller falls
        back to baseline weight).
        """
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT agent, wins, losses FROM agent_stats"
            ).fetchall()
        out: dict[str, float] = {}
        for r in rows:
            n = (r["wins"] or 0) + (r["losses"] or 0)
            if n >= min_trades:
                out[r["agent"]] = (r["wins"] or 0) / n
        return out

    def total_realized_pnl(self, mode: Optional[str] = None) -> float:
        """Sum of pnl from closed_trades. Used for drawdown calc."""
        with self._lock, self._conn() as c:
            if mode:
                row = c.execute(
                    "SELECT COALESCE(SUM(pnl), 0) AS s FROM closed_trades WHERE mode=?",
                    (mode,),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT COALESCE(SUM(pnl), 0) AS s FROM closed_trades"
                ).fetchone()
        return float(row["s"]) if row else 0.0

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

    def paper_balance_debit(self, asset: str, amount: Decimal | float) -> float:
        """Atomically subtract up to `amount` of `asset`, never going below zero.

        Returns the quantity actually debited (``min(amount, balance)``). Uses a
        ``BEGIN IMMEDIATE`` transaction so the read-modify-write is atomic even
        across processes — this prevents two concurrent SELLs from both reading
        the same balance and driving it negative.
        """
        amt = _f(amount)
        if amt <= 0:
            return 0.0
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute(
                    "SELECT qty FROM paper_balances WHERE asset=?", (asset,)
                ).fetchone()
                have = float(row["qty"]) if row else 0.0
                debited = have if amt > have else amt
                if debited > 0:
                    c.execute(
                        "UPDATE paper_balances SET qty=qty-? WHERE asset=?",
                        (debited, asset),
                    )
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return debited

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

    # ── Equity snapshots (for the equity curve) ───────────────────
    def record_equity_snapshot(
        self,
        *,
        mode: str,
        total_usdt: float,
        cash_usdt: float,
        invested_usdt: float,
    ) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO equity_snapshots(ts,mode,total_usdt,cash_usdt,invested_usdt) "
                "VALUES(?,?,?,?,?)",
                (_now(), mode, total_usdt, cash_usdt, invested_usdt),
            )

    def equity_curve(self, limit: int = 90) -> list[dict]:
        """Most recent N equity snapshots (oldest → newest)."""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT ts, total_usdt, cash_usdt, invested_usdt, mode "
                "FROM equity_snapshots ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ── Risk events (stop-loss / take-profit / trailing / max-hold exits) ───
    def recent_risk_events(self, limit: int = 25) -> list[dict]:
        """Closed trades where any contributing agent label starts with 'risk:'."""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM closed_trades "
                "WHERE agents LIKE '%\"risk:%' "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            try:
                agents = json.loads(d["agents"] or "[]")
            except json.JSONDecodeError:
                agents = []
            reason = next(
                (a.split(":", 1)[1] for a in agents if a.startswith("risk:")),
                "unknown",
            )
            d["reason"] = reason
            out.append(d)
        return out

    # ── ML signal events / model artifacts ───────────────────────────────
    def record_signal_event(
        self,
        *,
        mode: str,
        symbol: str,
        timeframe: str,
        action: str,
        confidence: float,
        entry_price: Decimal | float,
        atr_pct: Optional[float] = None,
        rsi_14: Optional[float] = None,
        ema_gap_pct: Optional[float] = None,
        agent_count: int = 0,
    ) -> int:
        """Persist one directional signal for later outcome labeling."""
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO ml_signal_events("
                "ts,mode,symbol,timeframe,action,confidence,entry_price,"
                "atr_pct,rsi_14,ema_gap_pct,agent_count"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _now(),
                    mode,
                    symbol,
                    timeframe,
                    action,
                    float(confidence),
                    _f(entry_price),
                    None if atr_pct is None else float(atr_pct),
                    None if rsi_14 is None else float(rsi_14),
                    None if ema_gap_pct is None else float(ema_gap_pct),
                    int(agent_count),
                ),
            )
            return int(cur.lastrowid or 0)

    def pending_signal_events(self, *, older_than_iso: str, limit: int = 500) -> list[dict]:
        """Unresolved signal events older than a cutoff timestamp."""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM ml_signal_events "
                "WHERE resolved=0 AND ts <= ? "
                "ORDER BY id ASC LIMIT ?",
                (older_than_iso, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_signal_event(
        self,
        *,
        event_id: int,
        horizon_minutes: int,
        outcome_return_pct: float,
        outcome_win: bool,
    ) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE ml_signal_events SET "
                "resolved=1, resolved_ts=?, horizon_minutes=?, "
                "outcome_return_pct=?, outcome_win=? "
                "WHERE id=?",
                (_now(), int(horizon_minutes), float(outcome_return_pct), 1 if outcome_win else 0, event_id),
            )

    def count_resolved_signal_events(self) -> int:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM ml_signal_events WHERE resolved=1"
            ).fetchone()
        return int(row["n"] if row else 0)

    def training_signal_rows(self, limit: int = 50_000) -> list[dict]:
        """Rows suitable for supervised training (resolved BUY/SELL only)."""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT id, mode, symbol, timeframe, action, confidence, entry_price, "
                "atr_pct, rsi_14, ema_gap_pct, agent_count, outcome_return_pct, outcome_win "
                "FROM ml_signal_events "
                "WHERE resolved=1 AND action IN ('BUY','SELL') AND outcome_win IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def latest_model_version(self, name: str) -> int:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT version FROM ml_models WHERE name=?", (name,)).fetchone()
        return int(row["version"] if row else 0)

    def save_model_artifact(
        self,
        *,
        name: str,
        algorithm: str,
        metrics: dict[str, Any],
        model: Any,
    ) -> int:
        version = self.latest_model_version(name) + 1
        payload = pickle.dumps(model)
        metrics_json = json.dumps(metrics, default=float)
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO ml_models(name,version,trained_at,algorithm,metrics,payload) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "version=excluded.version, trained_at=excluded.trained_at, "
                "algorithm=excluded.algorithm, metrics=excluded.metrics, payload=excluded.payload",
                (name, version, _now(), algorithm, metrics_json, payload),
            )
        return version

    def load_model_artifact(self, name: str) -> Optional[dict]:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT name, version, trained_at, algorithm, metrics, payload "
                "FROM ml_models WHERE name=?",
                (name,),
            ).fetchone()
        if not row:
            return None
        try:
            model = pickle.loads(row["payload"])
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to load model artifact %s: %s", name, exc)
            return None
        try:
            metrics = json.loads(row["metrics"])
        except json.JSONDecodeError:
            metrics = {}
        return {
            "name": row["name"],
            "version": int(row["version"]),
            "trained_at": row["trained_at"],
            "algorithm": row["algorithm"],
            "metrics": metrics,
            "model": model,
        }

    # ── Auth: users ──────────────────────────────────────────────────
    def user_count(self) -> int:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"] if row else 0)

    def create_user(self, *, email: str, password_hash: str) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO users(email,password_hash,email_verified,created_at) "
                "VALUES(?,?,?,?)",
                (email.lower(), password_hash, 0, _now()),
            )
            return int(cur.lastrowid or 0)

    def get_user_by_email(self, email: str) -> Optional[dict]:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM users WHERE email=? COLLATE NOCASE", (email.lower(),)
            ).fetchone()
        return dict(row) if row else None

    def get_user(self, user_id: int) -> Optional[dict]:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def update_user_password(self, user_id: int, password_hash: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE users SET password_hash=?, failed_attempts=0, locked_until=NULL "
                "WHERE id=?",
                (password_hash, user_id),
            )

    def mark_email_verified(self, user_id: int) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE users SET email_verified=1 WHERE id=?", (user_id,))

    def record_login_success(self, user_id: int) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE users SET last_login_at=?, failed_attempts=0, locked_until=NULL "
                "WHERE id=?",
                (_now(), user_id),
            )

    def record_login_failure(
        self, user_id: int, *, max_failed: int, lockout_minutes: int
    ) -> dict:
        from datetime import timedelta

        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT failed_attempts FROM users WHERE id=?", (user_id,)
            ).fetchone()
            attempts = int((row["failed_attempts"] if row else 0) or 0) + 1
            locked_until = None
            if attempts >= max_failed:
                locked_until = (
                    datetime.now(timezone.utc) + timedelta(minutes=lockout_minutes)
                ).isoformat()
            c.execute(
                "UPDATE users SET failed_attempts=?, locked_until=? WHERE id=?",
                (attempts, locked_until, user_id),
            )
        return {"attempts": attempts, "locked_until": locked_until}

    # ── Auth: tokens (email verify / password reset) ─────────────────
    def create_auth_token(
        self,
        *,
        user_id: int,
        purpose: str,
        token_hash: str,
        expires_at: str,
    ) -> None:
        with self._lock, self._conn() as c:
            # Invalidate any prior tokens of the same purpose for this user.
            c.execute(
                "DELETE FROM auth_tokens WHERE user_id=? AND purpose=?",
                (user_id, purpose),
            )
            c.execute(
                "INSERT INTO auth_tokens(token_hash,user_id,purpose,created_at,expires_at,used) "
                "VALUES(?,?,?,?,?,0)",
                (token_hash, user_id, purpose, _now(), expires_at),
            )

    def consume_auth_token(self, *, token_hash: str, purpose: str) -> Optional[int]:
        """Return user_id if token is valid+unused+unexpired; mark used. Else None."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT user_id, expires_at, used FROM auth_tokens "
                "WHERE token_hash=? AND purpose=?",
                (token_hash, purpose),
            ).fetchone()
            if not row:
                return None
            if int(row["used"]) == 1:
                return None
            if row["expires_at"] <= now:
                return None
            c.execute(
                "UPDATE auth_tokens SET used=1 WHERE token_hash=?", (token_hash,)
            )
            return int(row["user_id"])

    # ── Auth: sessions ───────────────────────────────────────────────
    def create_session(
        self,
        *,
        token_hash: str,
        user_id: int,
        expires_at: str,
        ip: Optional[str],
        user_agent: Optional[str],
    ) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO sessions(token_hash,user_id,created_at,expires_at,ip,user_agent) "
                "VALUES(?,?,?,?,?,?)",
                (token_hash, user_id, _now(), expires_at, ip, user_agent),
            )

    def get_session(self, token_hash: str) -> Optional[dict]:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM sessions WHERE token_hash=? AND expires_at > ?",
                (token_hash, now),
            ).fetchone()
        return dict(row) if row else None

    def delete_session(self, token_hash: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def delete_user_sessions(self, user_id: int) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def purge_expired_sessions(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            c.execute("DELETE FROM auth_tokens WHERE expires_at <= ?", (now,))

    # ── Auth: audit log ──────────────────────────────────────────────
    def record_audit(
        self,
        *,
        action: str,
        user_id: Optional[int] = None,
        ip: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO audit_log(ts,user_id,ip,action,detail) VALUES(?,?,?,?,?)",
                (_now(), user_id, ip, action, detail),
            )

    def recent_audit(self, limit: int = 100) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT a.id, a.ts, a.user_id, a.ip, a.action, a.detail, u.email "
                "FROM audit_log a LEFT JOIN users u ON u.id=a.user_id "
                "ORDER BY a.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


storage = Storage()
