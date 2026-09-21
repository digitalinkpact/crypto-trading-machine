"""Tests for Stage 6 — decision ledger + notifier (READ-ONLY)."""
from __future__ import annotations

from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from app.notify.core import Notifier
from app.research.decision_ledger import (
    DecisionLedger,
    format_notification,
    record_and_notify,
)
from app.research.experiment_verdict import (
    KEEP_TESTING,
    PAUSE,
    PROMOTE,
    REJECT,
    VerdictCard,
)


# ── Ledger ──────────────────────────────────────────────────────────────────

def test_ledger_records_and_lists_in_reverse_chronological_order(tmp_path):
    ledger = DecisionLedger(path=tmp_path / "t.db")
    ledger.record(VerdictCard(overall=KEEP_TESTING, reasons=["one"]))
    ledger.record(VerdictCard(overall=PAUSE, reasons=["two"]))

    rows = ledger.list_recent(limit=10)
    assert len(rows) == 2
    assert rows[0].overall == PAUSE
    assert rows[1].overall == KEEP_TESTING
    assert rows[0].reasons == ["two"]


def test_ledger_serializes_all_fields_round_trip(tmp_path):
    ledger = DecisionLedger(path=tmp_path / "t.db")
    card = VerdictCard(
        overall=REJECT,
        reasons=["a"],
        concerns=["b", "c"],
        recommendations=["do x"],
        inputs_present={"stage2_promotion": True},
    )
    ledger.record(card, channel="smtp")
    row = ledger.list_recent(limit=1)[0]
    assert row.overall == REJECT
    assert row.reasons == ["a"]
    assert row.concerns == ["b", "c"]
    assert row.recommendations == ["do x"]
    assert row.inputs_present == {"stage2_promotion": True}
    assert row.notification_channel == "smtp"


# ── Notification formatter ──────────────────────────────────────────────────

def test_format_notification_includes_all_present_fields():
    card = VerdictCard(
        overall=PAUSE,
        reasons=["exec drift"],
        concerns=["30bps spread"],
        recommendations=["halve size"],
        inputs_present={"stage2_promotion": False},
    )
    subject, body = format_notification(card)
    assert "PAUSE" in subject
    assert "exec drift" in body
    assert "30bps spread" in body
    assert "halve size" in body
    assert "stage2_promotion" in body  # listed as missing


# ── record_and_notify ───────────────────────────────────────────────────────

def test_keep_testing_verdict_does_not_notify(tmp_path):
    calls: list[tuple[str, str]] = []
    ledger = DecisionLedger(path=tmp_path / "t.db")

    def fake_notifier(subject: str, body: str) -> str:
        calls.append((subject, body))
        return "smtp"

    row = record_and_notify(
        VerdictCard(overall=KEEP_TESTING),
        ledger=ledger,
        notifier=fake_notifier,
    )
    assert calls == []
    assert row.notification_channel == ""


def test_pause_verdict_notifies_and_records_channel(tmp_path):
    calls: list[tuple[str, str]] = []
    ledger = DecisionLedger(path=tmp_path / "t.db")

    def fake_notifier(subject: str, body: str) -> str:
        calls.append((subject, body))
        return "smtp"

    row = record_and_notify(
        VerdictCard(overall=PAUSE, reasons=["r"], recommendations=["cut size"]),
        ledger=ledger,
        notifier=fake_notifier,
    )
    assert len(calls) == 1
    assert calls[0][0].endswith("PAUSE")
    assert row.notification_channel == "smtp"


def test_notifier_exception_records_error_channel_without_raising(tmp_path):
    def boom(subject: str, body: str) -> str:
        raise RuntimeError("smtp broken")

    ledger = DecisionLedger(path=tmp_path / "t.db")
    row = record_and_notify(
        VerdictCard(overall=REJECT),
        ledger=ledger,
        notifier=boom,
    )
    assert row.notification_channel == "error"


# ── Notifier ────────────────────────────────────────────────────────────────

def test_notifier_falls_back_to_log_when_smtp_not_configured():
    from app.config import Settings
    settings = Settings(_env_file=None)  # smtp_host is "" by default
    channel = Notifier(settings=settings).notify("subj", "body")
    assert channel == "log"


def test_notifier_uses_smtp_when_configured():
    from app.config import Settings
    settings = Settings(
        _env_file=None,
        smtp_host="mail.example.com",
        smtp_port=587,
        smtp_user="alerts@example.com",
        smtp_from="alerts@example.com",
        smtp_starttls=True,
    )

    sent: list[EmailMessage] = []

    class FakeSMTP:
        def __init__(self, host, port):
            self.host, self.port = host, port
        def starttls(self): pass
        def login(self, u, p): pass
        def send_message(self, msg): sent.append(msg)
        def quit(self): pass

    channel = Notifier(
        settings=settings, smtp_factory=lambda h, p: FakeSMTP(h, p)
    ).notify("hello", "world")

    assert channel == "smtp"
    assert len(sent) == 1
    assert sent[0]["Subject"] == "hello"
    assert sent[0]["From"] == "alerts@example.com"


def test_notifier_swallows_smtp_errors_and_reports_error_channel():
    from app.config import Settings
    settings = Settings(
        _env_file=None,
        smtp_host="mail.example.com",
        smtp_from="alerts@example.com",
    )

    class BrokenSMTP:
        def __init__(self, *a, **k): raise ConnectionRefusedError("nope")

    channel = Notifier(
        settings=settings, smtp_factory=lambda h, p: BrokenSMTP()
    ).notify("subj", "body")

    assert channel == "error"


# ── Scheduler wiring ────────────────────────────────────────────────────────

def test_scheduler_registers_weekly_verdict_job():
    from app.scheduler.jobs import build_scheduler
    scheduler = build_scheduler()
    # Scheduler is never started here — inspecting registered jobs is enough
    # and calling shutdown() on an unstarted AsyncIOScheduler raises.
    ids = {j.id for j in scheduler.get_jobs()}
    assert "weekly_verdict" in ids
