"""Phase 2 — the shared signals→regime→kernel pipeline (`run_strategy_execution`)
must reproduce the backtest's own walk EXACTLY.

This is the proof that the live/paper scanner can drive the SAME engine as the
backtest: ``run_strategy_execution(...) + force_close`` must equal what the backtest's
``_run_signal_walk`` produces for the same strategy/profile/history. Once green, the
walk is refactored to call ``run_strategy_execution`` (so they literally share code),
and the scanner calls it too — paper trades become the kernel's trades by construction.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forven.strategies import backtest as bt
from forven.strategies import execution_kernel as ek
from forven.strategies.builtin.ema_cross import EMACrossStrategy
from forven.strategies.builtin.rsi_momentum import RSIMomentumStrategy
from forven.strategies.builtin.supertrend import SuperTrendStrategy


def _frame(n: int = 400, seed: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0005, 0.02, size=n).cumsum()
    close = 100.0 * np.exp(steps)
    spread = np.abs(rng.normal(0.0, 0.012, size=n)) + 0.004
    high = close * (1.0 + spread)
    low = close * (1.0 - spread)
    openp = np.empty(n)
    openp[0] = close[0]
    openp[1:] = close[:-1] * (1.0 + rng.normal(0.0, 0.004, size=n - 1))
    high = np.maximum.reduce([high, openp, close])
    low = np.minimum.reduce([low, openp, close])
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": openp, "high": high, "low": low, "close": close, "volume": 1000.0},
        index=idx,
    )


WARMUP = 50
LEVERAGE = 2.0
FEE_BPS = 4.5
SLIP_BPS = 2.0

CASES = [
    ("rsi_momentum", RSIMomentumStrategy,
     {"rsi_period": 14, "rsi_entry": 45, "rsi_exit": 55, "ema_fast": 10, "ema_slow": 30, "adx_period": 14, "adx_min": 0},
     {"sizing_mode": "fraction", "risk_per_trade": 0.01, "stop_loss_pct": 3.0, "take_profit_pct": 5.0}),
    ("ema_cross", EMACrossStrategy,
     {"ema_fast": 8, "ema_slow": 21, "ema_regime": 50, "adx_period": 14, "adx_min": 0},
     None),  # no profile -> default 1% fraction sizing (must match on BOTH sides)
    ("supertrend", SuperTrendStrategy,
     {"multiplier": 1.0, "atr_period": 10},
     {"sizing_mode": "atr", "atr_stop_multiplier": 2.0, "risk_per_trade": 0.01, "trailing_stop_pct": 4.0}),
]


@pytest.mark.parametrize("label,cls,params,ec", CASES, ids=[c[0] for c in CASES])
@pytest.mark.parametrize("regime_gate", [True, False])
def test_shared_pipeline_matches_backtest_walk(label, cls, params, ec, regime_gate):
    df = _frame()
    strat = cls(f"{label}-test", params)

    reference = bt._run_signal_walk(
        None, df, strat.params, WARMUP, LEVERAGE,
        strategy_obj=strat, strategy_type=label,
        fee_bps=FEE_BPS, slippage_bps=SLIP_BPS, regime_gate=regime_gate,
        trade_mode="long_only", execution_controls=ec, initial_capital=10000.0,
    )

    res = bt.run_strategy_execution(
        df, strat, params=strat.params, warmup=WARMUP, leverage=LEVERAGE,
        fee_bps=FEE_BPS, slippage_bps=SLIP_BPS, regime_gate=regime_gate,
        trade_mode="long_only", execution_controls=ec, initial_capital=10000.0,
        strategy_type=label,
    )
    assert res is not None, f"[{label}] run_strategy_execution returned None (no generate_signals?)"
    drag = ek.round_trip_drag(FEE_BPS, SLIP_BPS, LEVERAGE)
    candidate = ek.force_close(res, df, leverage=LEVERAGE, round_trip_drag=drag, trade_mode="long_only")

    assert candidate == reference, (
        f"[{label}/regime_gate={regime_gate}] shared pipeline diverged from the backtest walk:\n"
        f"  walk={len(reference)} trades, shared={len(candidate)} trades"
    )


def test_isolated_worker_threads_symbol_into_the_kernel_path():
    """Regression (8a68eb7): symbol/timeframe were wired into the kernel call
    inside _run_signal_walk using names that did not exist in that scope, so
    EVERY signal-walk backtest died with `NameError: name 'asset' is not
    defined` before the kernel even ran. Drive the isolated worker end-to-end
    with the same positional argument order backtest_strategy submits."""
    df = _frame()
    result = bt._isolated_backtest_worker(
        "S-regress-asset",
        "stochastic",
        "stochastic",
        {"k_period": 14, "d_period": 3, "k_oversold": 25, "k_overbought": 75},
        df,
        LEVERAGE,
        FEE_BPS,
        SLIP_BPS,
        True,       # regime_gate
        WARMUP,
        "1h",       # resolved_timeframe
        "both",     # trade_mode
        False,      # include_funding (skip data-file lookups)
        None,       # execution_controls
        10000.0,    # initial_capital
        "ETH/BTC",  # asset — must reach run_strategy_execution as symbol=
    )

    assert "error" not in result, result.get("error")
    assert isinstance(result["is_trades"], list)
    assert isinstance(result["oos_trades"], list)


def test_profile_is_threaded_from_params_when_no_explicit_controls():
    """Phase 3: a strategy carrying an execution_profile must be sized/stopped by the
    gauntlet path (which passes execution_controls=None) IDENTICALLY to passing the
    profile explicitly — and DIFFERENTLY from the default 1% sizing. This is what makes
    the promotion metrics reflect what paper actually trades."""
    df = _frame()
    # Chosen to size DIFFERENTLY from the default risk engine (atr / mult=2.0 / risk=1%):
    # 2% risk over a 3.5x-ATR stop, so size_fraction is unambiguously distinct.
    profile = {"sizing_mode": "atr", "atr_stop_multiplier": 3.5, "risk_per_trade": 0.02, "take_profit_pct": 6.0}
    base = {"rsi_period": 14, "rsi_entry": 45, "rsi_exit": 55, "ema_fast": 10, "ema_slow": 30, "adx_period": 14, "adx_min": 0}
    profiled = dict(base, execution_profile=profile)

    def _walk(params, ec):
        strat = RSIMomentumStrategy("rsi-thread", dict(params, _asset="BTC"))
        return bt._run_signal_walk(
            None, df, params, WARMUP, LEVERAGE, strategy_obj=strat, strategy_type="rsi_momentum",
            fee_bps=FEE_BPS, slippage_bps=SLIP_BPS, regime_gate=False, trade_mode="long_only",
            execution_controls=ec, initial_capital=10000.0,
        )

    explicit = _walk(profiled, profile)          # profile passed explicitly
    threaded = _walk(profiled, None)             # gauntlet style: None → derive from params
    default_sized = _walk(base, None)            # no profile → default 1%

    assert threaded == explicit, "execution_profile was not threaded from params"
    assert [t.get("size_fraction") for t in threaded] != [t.get("size_fraction") for t in default_sized], (
        "threaded profile produced the same sizing as the 1% default — profile had no effect"
    )
