# Data Schema

This documents the columns available on the backtest/scanner DataFrame so strategies reference real fields. It is injected into every worker agent's context.

## Core Columns (always present)

Every backtest DataFrame contains these OHLCV columns:

| Column      | Type    | Description                          |
|-------------|---------|--------------------------------------|
| timestamp   | int64   | Unix milliseconds (UTC)              |
| open        | float64 | Candle open price                    |
| high        | float64 | Candle high price                    |
| low         | float64 | Candle low price                     |
| close       | float64 | Candle close price                   |
| volume      | float64 | Volume in base currency              |

Candles are Binance **USD-M perpetual** klines (the venue semantics we execute on via
Hyperliquid perps), with spot fallback only for bases without a listed perp.

## Enrichment Columns (present when historical data collected)

These columns are joined onto OHLCV at read time via `DataManager.enrich()`. They are
populated via Binance Vision bulk backfill and continuous collection. Every
forward-window aggregate stream is re-stamped to its bucket CLOSE before the join, so a
sub-hour candle never reads an in-progress bucket (no lookahead).

| Column                | Type    | Granularity | Available From | Description                                               |
|-----------------------|---------|-------------|----------------|-----------------------------------------------------------|
| funding_rate          | float64 | 8h          | ~2019-09       | Binance Futures funding rate. Positive = longs pay shorts. Sentiment/positioning signal. |
| open_interest         | float64 | 1h, 4h      | ~2021-12       | Open interest in base currency. Rising OI + rising price = trend confirmation; rising OI + falling price = bearish pressure. |
| ls_ratio              | float64 | 1h          | ~2021-12       | Global long/short ACCOUNT ratio (>1 = more long accounts). Contrarian crowding signal. |
| long_pct / short_pct  | float64 | 1h          | ~2021-12       | Long/short account shares derived from ls_ratio (sum to 1). |
| taker_buy_sell_ratio  | float64 | 1h          | ~2021-12       | Taker buy volume / taker sell volume (>1 = aggressive buying). Order-flow signal. |
| long_liq_usd / short_liq_usd / liq_imbalance | float64 | 1h | forward-only | Liquidation flow (collector env-gated; columns absent unless enabled). |
| basis                 | float64 | 1h          | ~2019-09       | Perp premium index vs underlying index (dimensionless fraction, e.g. 0.0004). Funding predictor + crowding/carry regime. Positive = perp trades rich. |
| iv_btc / iv_eth       | float64 | 1h          | ~2021-03       | Deribit DVOL implied-volatility index (crypto's VIX; annualized %, e.g. 55.0). Market-wide vol regime — joined onto EVERY symbol's frame. |

## Research-only Columns (opt-in, never on the strategy path)

Daily macro carries same-day-close lookahead and weekend gaps, so it is only joined
when a research caller passes `include_macro=True`: `fear_greed`, `vix_close`,
`dxy_close`, `spy_close`, `treasury_10y`, `btc_dominance`.

### Notes

- Joins are `merge_asof` backward — each candle gets the most recent value KNOWN at
  its open (aggregates: known at the source bucket's close).
- Columns are `NaN` before their stream's coverage starts, and the whole column is
  ABSENT when a stream was never collected for the symbol. Always guard:
  `df['basis'].notna()` or `.fillna(0)` where zero is a sane neutral. For
  `open_interest`/`ls_ratio`/`basis`, prefer `.notna()` gating — zero is a real
  market state, not "missing".
- `basis` and `iv_*` are NOT filled with defaults — a fake 0 basis or 0 IV would be
  a signal, so missing stays NaN.
- Coverage varies per symbol: the research universe (top ~50 perps by liquidity)
  carries 1h/4h/1d everywhere, 15m/5m on the top ~20, 1m on the top ~10. Deep
  derivatives history (OI/LSR/taker) is bounded by the `metrics_days` setting.
- Symbol listing windows matter: use only bars within a symbol's listed window
  (the symbol registry records inception/delisting; delisted history remains
  backtestable on purpose — survivorship-bias control).
