from collections import Counter

from app.config import Timeframe
from app.signals import Signal, SignalAction, SignalAggregator


def _sig(agent: str, sym: str, tf: Timeframe, action: SignalAction, conf: float) -> Signal:
    return Signal(agent=agent, symbol=sym, timeframe=tf, action=action, confidence=conf)


def test_aggregator_picks_majority_weighted():
    sigs = [
        _sig("a", "BTCUSDT", Timeframe.H1, SignalAction.BUY, 0.9),
        _sig("b", "BTCUSDT", Timeframe.W1, SignalAction.SELL, 0.6),  # weekly weight 4x
    ]
    out = SignalAggregator().aggregate(sigs)
    assert out["BTCUSDT"].action is SignalAction.SELL


def test_aggregator_groups_by_symbol():
    sigs = [
        _sig("a", "BTCUSDT", Timeframe.D1, SignalAction.BUY, 0.5),
        _sig("a", "ETHUSDT", Timeframe.D1, SignalAction.SELL, 0.5),
    ]
    out = SignalAggregator().aggregate(sigs)
    assert Counter(s.action for s in out.values()) == Counter(
        {SignalAction.BUY: 1, SignalAction.SELL: 1}
    )
