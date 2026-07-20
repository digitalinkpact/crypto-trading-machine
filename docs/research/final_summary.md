# ProfitStream Research Results

- Tested combinations: 10000
- Passing combinations (win>65%, pf>1.75, dd<10%): 270
- Trades used in autopsy window: 68
- Losses in autopsy window: 30

## Recommended Production Configuration

- score_threshold: 71
- rsi_lo/rsi_hi: 40/60
- vol_mult: 1.8
- atr_threshold: 0.04
- adx_threshold: 35.0
- trailing_stop: 0.012
- stop_loss: 0.0125
- take_profit: 0.08
- ema_short/ema_long: 9/21

## Why the Prior Setup Drew Down

- Too many entries fired outside trend-bull states.
- Weak volume/ADX regimes created whipsaw losses.
- Stop-loss clusters without cooldown protection amplified drawdown.
- Sideways and low-liquidity periods generated low-quality trades.
