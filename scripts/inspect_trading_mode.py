"""Read-only execution configuration inspection; never imports storage or loops."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.config import Settings, _ENV_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=_ENV_PATH)
    arguments = parser.parse_args()
    settings = Settings(_env_file=arguments.env_file)
    directory = settings.data_cache_dir
    ledger = directory / "trading.db" if settings.ledger_mode == "live" else directory / "simulation" / "trading.db"
    controls = (
        "profitstream_score_threshold", "risk_per_trade_pct", "max_position_pct",
        "max_open_positions", "aggressive_max_open_positions", "rollback_max_open_positions",
        "max_long_exposure_pct", "kelly_fraction_cap", "stop_loss_pct", "atr_stop_min_pct",
        "atr_stop_max_pct", "take_profit_1_pct", "take_profit_1_fraction", "take_profit_2_pct",
        "take_profit_2_fraction", "trailing_stop_pct", "daily_loss_limit_pct",
        "drawdown_circuit_breaker_pct", "market_regime_gate_enabled", "breadth_gate_enabled",
        "ml_gate_enabled", "mean_reversion_exit_enabled", "trend_filter_enabled",
    )
    print(json.dumps({
        "env_file": str(arguments.env_file),
        "trading_mode": settings.trading_mode.value,
        "legacy_flags": {name: getattr(settings, name) for name in ("live_mode", "paper_trading", "dry_run")},
        "live_buys_enabled": settings.live_buys_enabled,
        "ledger": str(ledger.resolve()),
        "mode_environment_overrides": [name for name in ("TRADING_MODE", "LIVE_MODE", "PAPER_TRADING", "DRY_RUN") if name in os.environ],
        "risk_controls": {name: getattr(settings, name) for name in controls},
        "mutations": False,
    }, indent=2))


if __name__ == "__main__":
    main()