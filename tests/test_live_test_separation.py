"""Test runs must never touch LIVE storage, credentials, or diagnostics."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.exchange.client import BinanceUSClient
from app.storage.db import Storage

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_storage_refuses_non_temporary_database_under_pytest():
    production_like = _REPO_ROOT / "data" / "cache" / "guard-probe.db"

    with pytest.raises(RuntimeError, match="non-temporary database"):
        Storage(production_like)
    assert not production_like.exists()


def test_exchange_client_never_holds_credentials_under_pytest():
    settings = Settings(_env_file=None).model_copy(update={
        "binance_api_key": SecretStr("k" * 64),
        "binance_api_secret": SecretStr("s" * 64),
    })

    client = BinanceUSClient(settings=settings)

    assert client._spot.api_key is None
    assert client._spot.api_secret is None


def test_pytest_without_conftest_does_not_load_repository_env_file():
    env = {k: v for k, v in os.environ.items() if k != "APP_ENV_FILE"}
    env["PYTEST_VERSION"] = "8.2.0"
    probe = "import app.config as c; print(c._ENV_PATH)"

    out = subprocess.run(
        [sys.executable, "-c", probe], cwd=_REPO_ROOT, env=env,
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    assert out == os.devnull


def test_live_diagnostics_exclude_test_tagged_audit_rows(tmp_path):
    store = Storage(tmp_path / "audit.db")
    common = dict(
        mode="live", symbol="BTCUSDT", signal="SELL", confidence=0.9,
        risk_passed=True, position_exists=True, available_balance=1.0,
        min_notional_passed=True, exception=None,
    )
    store.record_trade_audit(
        **common, execution_attempted=True, binance_response="SUCCESS",
        final_outcome="executed_sell", detail={"agents": ["test"]},
    )
    store.record_trade_audit(
        **common, execution_attempted=False, binance_response="NOT_SUBMITTED",
        final_outcome="market_gate", detail={"agents": ["profitstream_strategy"]},
    )

    live = store.recent_trade_audit(mode="live")
    assert [row["final_outcome"] for row in live] == ["market_gate"]
    assert len(store.recent_trade_audit(mode="live", include_test=True)) == 2
