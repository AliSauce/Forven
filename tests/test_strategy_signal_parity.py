from __future__ import annotations

import importlib
import inspect

import pandas as pd
from fastapi import APIRouter

from forven.strategies.builtin import bollinger_s00120 as bollinger_module
from forven.routers import verdict as verdict_router
from forven.strategies.backtest import _run_signal_walk, _vectorized_signals
from forven.strategies.builtin.donchian import DonchianStrategy
from forven.strategies.builtin.ema_cross import EMACrossStrategy
from forven.strategies.builtin.orb import ORBStrategy
from forven.strategies.builtin.parabolic_sar import (
    ParabolicSARStrategy,
    parabolic_sar_series,
)
from forven.strategies.builtin.stress_test import StressTestStrategy
from forven.strategies.builtin.vwap_pullback_eth_15m import (
    Eth15mVWAPPullbackStrategy,
)


def _price_frame(closes: list[float], *, freq: str = "h") -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=len(closes), freq=freq, tz="UTC")
    close = pd.Series(closes, index=index, dtype=float)
    return pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + 0.4,
            "low": close - 0.4,
            "close": close,
            "volume": 1_000.0,
        },
        index=index,
    )


def test_donchian_generate_signal_matches_vectorized_path():
    df = _price_frame([10.0, 10.2, 10.4, 10.6, 10.8, 10.7, 11.5, 12.4])
    params = {"donchian_period": 5}
    strategy = DonchianStrategy("donchian-test", params)

    signal = strategy.generate_signal(df)
    entry_signals, exit_signals = _vectorized_signals(df, "donchian", params)

    assert signal.entry_signal is True
    assert signal.exit_signal is False
    assert bool(entry_signals.iloc[-1]) is signal.entry_signal
    assert bool(exit_signals.iloc[-1]) is signal.exit_signal


def test_orb_generate_signal_matches_vectorized_path():
    df = _price_frame([10.0, 10.1, 10.2, 10.3, 10.4, 10.2, 11.2])
    params = {"orb_bars": 4}
    strategy = ORBStrategy("orb-test", {"range_bars": 4})

    signal = strategy.generate_signal(df)
    entry_signals, exit_signals = _vectorized_signals(df, "orb", params)

    assert signal.entry_signal is True
    assert signal.exit_signal is False
    assert bool(entry_signals.iloc[-1]) is signal.entry_signal
    assert bool(exit_signals.iloc[-1]) is signal.exit_signal


def test_parabolic_sar_step_affects_series_and_matches_vectorized_path():
    df = _price_frame([10.0, 10.4, 10.9, 11.5, 11.2, 10.7, 10.1, 10.6, 11.1, 11.6, 12.0, 11.4])

    slow = parabolic_sar_series(df, step=0.01, max_step=0.2)
    fast = parabolic_sar_series(df, step=0.05, max_step=0.2)
    assert not slow.equals(fast)

    params = {"step": 0.02, "max_step": 0.2}
    strategy = ParabolicSARStrategy("psar-test", params)
    signal = strategy.generate_signal(df)
    entry_signals, exit_signals = _vectorized_signals(df, "parabolic_sar", params)

    assert bool(entry_signals.iloc[-1]) is signal.entry_signal
    assert bool(exit_signals.iloc[-1]) is signal.exit_signal


def test_ema_cross_generate_signal_matches_vectorized_path_with_regime_filter():
    df = _price_frame([100.0, 95.0, 90.0, 85.0, 80.0, 82.0, 84.0, 86.0])
    params = {"ema_fast": 2, "ema_slow": 4, "ema_regime": 8, "adx_period": 2, "adx_min": 0}
    strategy = EMACrossStrategy("ema-cross-test", params)

    signal = strategy.generate_signal(df)
    entry_signals, exit_signals = _vectorized_signals(
        df.assign(
            ema_fast=df["close"].ewm(span=params["ema_fast"], adjust=False).mean(),
            ema_slow=df["close"].ewm(span=params["ema_slow"], adjust=False).mean(),
            ema_regime=df["close"].ewm(span=params["ema_regime"], adjust=False).mean(),
            adx_val=100.0,
        ),
        "ema_cross",
        params,
    )

    assert signal.entry_signal is False
    assert signal.exit_signal is False
    assert bool(entry_signals.iloc[-1]) is signal.entry_signal
    assert bool(exit_signals.iloc[-1]) is signal.exit_signal


def test_stress_test_generate_signal_matches_vectorized_schedule():
    df = _price_frame([100.0 + (i * 0.25) for i in range(24)], freq="15min")
    strategy = StressTestStrategy("stress-test", {"hold_bars": 1, "flat_bars": 1})

    signal = strategy.generate_signal(df)
    entry_signals, exit_signals = strategy.generate_signals(df)

    assert bool(entry_signals.iloc[-1]) is signal.entry_signal
    assert bool(exit_signals.iloc[-1]) is signal.exit_signal
    assert signal.indicators["cycle_bars"] == 2
    assert signal.indicators["bar_interval_seconds"] == 900


def test_eth_15m_vwap_pullback_generate_signal_matches_vectorized_path():
    closes = [100.0 + (i * 0.3) for i in range(60)] + [118.0, 116.5, 115.0, 114.0, 113.0, 114.0]
    df = _price_frame(closes, freq="15min")
    strategy = Eth15mVWAPPullbackStrategy(
        "vwap-pullback-test",
        {
            "vwap_period": 8,
            "distance_pct": 0.01,
            "ema_regime": 20,
            "slope_bars": 4,
            "rsi_period": 6,
            "rsi_entry": 45,
            "rsi_exit": 60,
        },
    )

    signal = strategy.generate_signal(df)
    entry_signals, exit_signals = strategy.generate_signals(df)

    assert bool(entry_signals.iloc[-1]) is signal.entry_signal
    assert bool(exit_signals.iloc[-1]) is signal.exit_signal


def test_stress_test_signal_is_stable_across_rolling_windows():
    df = _price_frame([100.0 + (i * 0.1) for i in range(40)], freq="5min")
    strategy = StressTestStrategy("stress-test", {"hold_bars": 1, "flat_bars": 2, "phase_offset": 1})

    full_signal = strategy.generate_signal(df)
    rolling_signal = strategy.generate_signal(df.tail(12))

    assert rolling_signal.entry_signal is full_signal.entry_signal
    assert rolling_signal.exit_signal is full_signal.exit_signal
    assert rolling_signal.indicators["phase"] == full_signal.indicators["phase"]


def test_stress_test_backtest_walk_generates_many_single_bar_trades():
    df = _price_frame([100.0 + (i * 0.05) for i in range(480)], freq="1min")
    strategy = StressTestStrategy("stress-test", {"hold_bars": 1, "flat_bars": 1})

    trades = _run_signal_walk(
        checker=None,
        df=df,
        params=strategy.params,
        warmup=210,
        leverage=1.0,
        strategy_obj=strategy,
        fee_bps=0.0,
        slippage_bps=0.0,
    )

    # The final bar can leave a position open-at-end (force-closed same bar,
    # bars_held=0); exclude it so we assess the steady-state cadence only.
    closed = [trade for trade in trades if not trade.get("open_at_end")]
    assert len(closed) >= 100
    assert {trade["bars_held"] for trade in closed} == {1}
    assert {trade["direction"] for trade in closed} == {"long"}


def test_verdict_router_import_smoke():
    module = importlib.reload(verdict_router)

    assert isinstance(module.router, APIRouter)

    route_paths = [route.path for route in module.router.routes]
    assert route_paths.count("/verdict/run") == 1
    # GET /verdict/{result_id} was intentionally removed — verdicts are not persisted, so
    # there is nothing to fetch by id (see forven/routers/strategies.py). Guard the removal.
    assert route_paths.count("/verdict/{result_id}") == 0
    assert route_paths.count("/verdict/guide") == 1


def test_verdict_router_source_is_not_duplicated():
    source = inspect.getsource(verdict_router)
    assert source.count("router = APIRouter(") == 1
    assert source.count('@router.post("/verdict/run"') == 1


def test_bollinger_module_import_smoke():
    module = importlib.reload(bollinger_module)

    assert module.STRATEGY_CLASS.__name__ == "BollingerS00120Strategy"
    assert module.TYPE_NAME == "bollinger"
    assert len(module.STRATEGIES) == 1


def test_bollinger_module_source_is_not_duplicated():
    source = inspect.getsource(bollinger_module)
    assert source.count("TYPE_NAME = ") == 1
    assert source.count("STRATEGY_CLASS = ") == 1
