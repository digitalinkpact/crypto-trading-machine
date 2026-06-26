from __future__ import annotations

import pytest

from app import credentials


def test_save_trade_fees_rejects_out_of_range():
    # Validation happens before any .env write, so these never touch disk.
    with pytest.raises(ValueError):
        credentials.save_trade_fees(maker=0.001, taker=0.05)  # taker > 1%
    with pytest.raises(ValueError):
        credentials.save_trade_fees(maker=-0.001, taker=0.001)  # negative maker
