"""Monitoring honesty: test rows excluded, internal gates never shown as Binance
rejections, submitted vs filled counted separately, monitors read-only."""
from __future__ import annotations

import sqlite3
import warnings
from decimal import Decimal

import pytest

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="'crypt' is deprecated", category=DeprecationWarning)
    from app.api.routes import _audit_stage_text
from app.exchange import Order, OrderSide, OrderStatus, OrderType
from app.storage.db import Storage
from app.trading import audit_labels, risk
from app.trading.autopilot import Autopilot
from scripts import monitor_tick_events
from scripts.trade_dashboard import TradeMonitor

LEGACY_INTERNAL = {"mode": "live", "execution_attempted": 0, "binance_response": "REJECTED",
                   "final_outcome": "risk_manager", "detail": "{}"}
LEGACY_FILLED = {"mode": "live", "execution_attempted": 1, "binance_response": "SUCCESS",
                 "final_outcome": "executed_buy", "detail": "{}"}
FIXTURE = dict(LEGACY_FILLED, detail='{"agents": ["test"]}')


@pytest.mark.parametrize("row, stage", [
    (LEGACY_INTERNAL, audit_labels.INTERNAL_REJECTED),
    (dict(LEGACY_INTERNAL, binance_response=""), audit_labels.INTERNAL_REJECTED),
    (dict(LEGACY_INTERNAL, binance_response="NOT_SUBMITTED"), audit_labels.INTERNAL_REJECTED),
    (LEGACY_FILLED, audit_labels.FILLED),
    (dict(LEGACY_FILLED, binance_response="FILLED"), audit_labels.FILLED),
    (dict(LEGACY_FILLED, binance_response="EXPIRED"), audit_labels.SUBMITTED_NOT_FILLED),
    (dict(LEGACY_FILLED, binance_response="REJECTED"), audit_labels.SUBMITTED_NOT_FILLED),
    (dict(LEGACY_FILLED, mode="paper"), audit_labels.SIMULATED),
    (dict(LEGACY_INTERNAL, binance_response="DRY_RUN"), audit_labels.SIMULATED),
    (FIXTURE, audit_labels.TEST_FIXTURE),
])
def test_execution_stage_classifies_legacy_and_new_rows(row, stage):
    assert audit_labels.execution_stage(row) == stage


def test_internal_rejection_text_never_mentions_a_binance_rejection():
    text = _audit_stage_text(LEGACY_INTERNAL)
    assert "internal gate" in text and "risk_manager" in text
    assert "Binance.US" not in text.replace("not sent to Binance.US", "")


@pytest.fixture
def audit_db(tmp_path):
    db = Storage(path=tmp_path / "trading.db")
    base = dict(mode="live", symbol="BTCUSDT", confidence=0.9, risk_passed=True, position_exists=False,
                available_balance=Decimal("10"), min_notional_passed=True, exception=None)
    db.record_trade_audit(**base, signal="BUY", execution_attempted=False, binance_response="REJECTED",
                          final_outcome="ml_gate", detail={})
    db.record_trade_audit(**base, signal="BUY", execution_attempted=True, binance_response="SUCCESS",
                          final_outcome="executed_buy", detail={"agents": ["test"]})
    db.record_trade_audit(**base, signal="SELL", execution_attempted=True, binance_response="FILLED",
                          final_outcome="executed", detail={})
    db.record_trade_audit(**base, signal="BUY", execution_attempted=True, binance_response="EXPIRED",
                          final_outcome="rejected: exchange status EXPIRED", detail={})
    db.record_order(mode="live", symbol="BTCUSDT", side="BUY", qty=1, price=100,
                    client_order_id="ctm-real", agents=["manual_test"])
    return tmp_path / "trading.db"


def test_readonly_monitor_connection_excludes_test_rows_and_cannot_write(audit_db):
    conn = audit_labels.connect_readonly(audit_db)
    rows = audit_labels.fetch_trade_audit(conn, mode="live")
    assert len(rows) == 3
    assert not any(audit_labels.is_test_fixture(row) for row in rows)
    assert len(audit_labels.fetch_trade_audit(conn, mode="live", include_test=True)) == 4
    assert audit_labels.count_test_fixture_rows(conn, "live") == 1
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM trade_audit")


def test_dashboard_counts_fills_separately_from_unfilled_and_internal(audit_db):
    monitor = TradeMonitor("live")
    monitor.ingest(audit_labels.fetch_trade_audit(audit_labels.connect_readonly(audit_db), mode="live"))
    assert monitor.filled == {"SELL": 1}
    assert monitor.not_filled == {"EXPIRED": 1}
    assert monitor.internal_reasons == {"ml_gate": 1}


def test_tick_event_monitor_labels_stages_and_flags_operator_test_orders(audit_db, capsys):
    monitor_tick_events.report(audit_labels.connect_readonly(audit_db), mode="live")
    out = capsys.readouterr().out
    assert "internal gate - not sent to Binance.US" in out
    assert "submitted to Binance.US - not filled" in out
    assert "executed_buy" not in out  # pytest fixture row hidden
    assert "[OPERATOR TEST ORDER]" in out


def test_order_outcome_labels_do_not_blame_the_exchange_for_internal_refusals():
    ap = Autopilot()
    ap.state.mode = "live"
    assert ap._order_outcome_labels(None) == (False, "NOT_SUBMITTED", "rejected: not submitted (internal)")
    expired = Order(symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.MARKET, quantity=Decimal("1"),
                    client_order_id="c", status=OrderStatus.EXPIRED)
    assert ap._order_outcome_labels(expired) == (True, "EXPIRED", "rejected: exchange status EXPIRED")
    kill_switch = expired.model_copy(update={"status": OrderStatus.REJECTED})
    assert ap._order_outcome_labels(kill_switch) == (
        False, "NOT_SUBMITTED", "rejected: not submitted (client kill switch)")
    exchange_rejected = kill_switch.model_copy(update={"exchange_order_id": "991"})
    assert ap._order_outcome_labels(exchange_rejected)[:2] == (True, "REJECTED")


def test_live_risk_state_is_mirrored_to_legacy_keys_for_rollback():
    risk._put("tp1:live:BTCUSDT", True)
    risk._put("tp1:paper:ETHUSDT", True)
    assert risk.storage.kv_get("tp1:BTCUSDT") is True
    assert risk.storage.kv_get("tp1:ETHUSDT") is None
