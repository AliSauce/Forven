from __future__ import annotations

import math

import pytest

import pandas as pd

import numpy as np

from forven.strategies.backtest import (
    _BARS_PER_YEAR,
    _build_equity_curve_from_trades,
    _compute_basic_metrics,
    compute_metrics,
)


def _curve(equities):
    """Minimal MTM ledger from a list of per-bar equity values."""
    return [
        {"equity": float(e), "drawdown_equity": float(e), "initial_equity": float(equities[0])}
        for e in equities
    ]


def test_compute_basic_metrics_uses_compounded_equity_curve():
    trades = [
        {"pnl_pct": 0.10, "bars_held": 5},
        {"pnl_pct": -0.50, "bars_held": 8},
        {"pnl_pct": 0.20, "bars_held": 6},
    ]

    metrics = _compute_basic_metrics(trades, total_bars=720)

    # Equity path: 1.0 -> 1.1 -> 0.55 -> 0.66
    assert float(metrics["total_return_pct"]) == pytest.approx(-0.34, abs=1e-5)
    assert float(metrics["max_drawdown_pct"]) == pytest.approx(0.50, abs=1e-5)


def test_compute_basic_metrics_caps_liquidation_drawdown():
    trades = [
        {"pnl_pct": -1.50, "bars_held": 2},
        {"pnl_pct": 0.30, "bars_held": 3},
    ]

    metrics = _compute_basic_metrics(trades, total_bars=720)

    assert float(metrics["total_return_pct"]) == pytest.approx(-1.0, abs=1e-9)
    assert float(metrics["max_drawdown_pct"]) == pytest.approx(1.0, abs=1e-9)


def test_compute_metrics_monthly_and_annualized_are_ratio_units():
    metrics = compute_metrics(
        trades=[{"pnl_pct": 0.20, "bars_held": 10}],
        total_bars=731,  # ~1 month
    )

    assert float(metrics["total_return_pct"]) == pytest.approx(0.20, abs=1e-5)
    assert float(metrics["monthly_return_pct"]) == pytest.approx(0.20, abs=1e-3)
    # 20% per month compounded for a year => ~7.9x in ratio units (not 790% points).
    assert 7.0 < float(metrics["annualized_return_pct"]) < 9.0


def test_compute_basic_metrics_single_trade_does_not_require_mean_return():
    metrics = _compute_basic_metrics(
        trades=[{"pnl_pct": 0.12, "bars_held": 4}],
        total_bars=96,
    )

    assert float(metrics["sharpe"]) == 0.0
    assert float(metrics["sortino"]) == 0.0


def test_sortino_uses_target_semideviation_about_zero():
    """Downside deviation must be the RMS of the NEGATIVE returns about MAR=0, not
    np.std (which mean-centers the downside list and overstates Sortino)."""
    trades = [
        {"pnl_pct": 0.10, "bars_held": 3},
        {"pnl_pct": -0.20, "bars_held": 4},
        {"pnl_pct": 0.30, "bars_held": 5},
    ]
    # mean = 0.0666667; downside RMS = sqrt(0.20**2 / 3) = 0.1154701; with total_bars ==
    # bars_per_year, trades_per_year = 3 → sortino = (0.0666667/0.1154701)*sqrt(3) = 1.0
    # exactly. The old mean-centered np.std denominator gave ~1.2247 (overstated).
    m = _compute_basic_metrics(trades, total_bars=8760, timeframe="1h")
    assert float(m["sortino"]) == pytest.approx(1.0, abs=1e-3)


def test_sharpe_annualization_uses_timeframe():
    """With the same bar count, daily timeframe should yield lower Sharpe than hourly.

    Given identical bar counts, hourly bars span a shorter calendar period than
    daily bars, so trades_per_year is higher for hourly, producing a higher
    annualized Sharpe.  The ratio should be sqrt(8760/365) = sqrt(24) ~ 4.9.
    """
    trades = [
        {"pnl_pct": 0.05, "bars_held": 3},
        {"pnl_pct": -0.02, "bars_held": 2},
        {"pnl_pct": 0.03, "bars_held": 4},
        {"pnl_pct": 0.01, "bars_held": 3},
    ]

    # Same bar count — only the timeframe interpretation differs.
    hourly = _compute_basic_metrics(trades, total_bars=720, timeframe="1h")
    daily = _compute_basic_metrics(trades, total_bars=720, timeframe="1d")

    sharpe_1h = float(hourly["sharpe"])
    sharpe_1d = float(daily["sharpe"])

    assert sharpe_1h > 0
    assert sharpe_1d > 0

    # hourly / daily ratio should be sqrt(8760/365) = sqrt(24) ~ 4.9
    ratio = sharpe_1h / sharpe_1d
    assert ratio == pytest.approx(math.sqrt(24), rel=0.01)


def test_mark_to_market_curve_captures_intratrade_drawdown():
    idx = pd.date_range("2025-01-01", periods=4, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100.0, 100.0, 50.0, 110.0],
            "high": [100.0, 100.0, 55.0, 110.0],
            "low": [100.0, 100.0, 50.0, 110.0],
            "close": [100.0, 100.0, 50.0, 110.0],
            "volume": [1.0] * 4,
        },
        index=idx,
    )
    trades = [
        {
            "entry_time": str(idx[1]),
            "exit_time": str(idx[3]),
            "entry_price": 100.0,
            "exit_price": 110.0,
            "direction": "long",
            "leverage": 1.0,
            "size_fraction_raw": 1.0,
            "pnl_pct": 0.10,
            "bars_held": 2,
        }
    ]

    curve = _build_equity_curve_from_trades(trades, df, 10_000.0)
    metrics = compute_metrics(trades, total_bars=len(df), equity_curve=curve)

    assert curve[2]["equity"] == pytest.approx(5_000.0)
    assert float(metrics["total_return_pct"]) == pytest.approx(0.10)
    assert float(metrics["max_drawdown_pct"]) == pytest.approx(0.50)


def _bar_sharpe_expected(returns, timeframe):
    """Mirror the production bar-level Sharpe (mean/std * sqrt(bpy), clamped to ±10)."""
    r = np.asarray(returns, dtype=float)
    bpy = _BARS_PER_YEAR[timeframe]
    raw = (r.mean() / r.std()) * np.sqrt(bpy) if r.std() > 1e-6 else 0.0
    return float(np.clip(raw, -10.0, 10.0))


def test_bar_level_sharpe_is_primary_when_curve_present_and_includes_flat_bars():
    """v5: with an equity curve, `sharpe`/`sortino` are the bar-level CALENDAR values —
    computed from consecutive-point returns (flat bars INCLUDED), annualized by
    sqrt(bars_per_year). Trade-based (event) values are preserved as trade_*."""
    # Equity path chosen so bar returns have a deliberate FLAT bar and enough dispersion
    # that the annualized Sharpe stays UNCLAMPED (well within ±10) on the weekly frame.
    # bar returns: [+0.02, 0.0(flat), -0.01, +0.03, -0.005]
    e0 = 10_000.0
    r = [0.02, 0.0, -0.01, 0.03, -0.005]
    equities = [e0]
    for x in r:
        equities.append(equities[-1] * (1.0 + x))
    curve = _curve(equities)
    trades = [
        {"pnl_pct": 0.02, "bars_held": 1},
        {"pnl_pct": -0.01, "bars_held": 1},
        {"pnl_pct": 0.03, "bars_held": 1},
        {"pnl_pct": -0.005, "bars_held": 1},
    ]
    m = _compute_basic_metrics(trades, total_bars=len(curve), timeframe="1w", equity_curve=curve)

    # Reconstruct the exact per-bar simple returns from the curve (what the engine uses).
    eq = np.array(equities)
    bar_returns = (eq[1:] - eq[:-1]) / eq[:-1]
    expected = _bar_sharpe_expected(bar_returns, "1w")
    assert -10.0 < expected < 10.0, "test frame must keep the bar-Sharpe unclamped"
    assert float(m["sharpe"]) == pytest.approx(round(expected, 3), abs=2e-3)

    # Trade-based (event) value is preserved and computed by the OLD formula (differs).
    event = _compute_basic_metrics(trades, total_bars=len(curve), timeframe="1w")  # no curve
    assert float(m["trade_sharpe"]) == pytest.approx(float(event["sharpe"]), abs=1e-6)


def test_bar_level_sharpe_lower_than_event_for_sparse_trading():
    """The scale-shift note: a sparse-trading strategy's bar-Sharpe is LOWER than its
    event Sharpe because the flat bars dilute the mean and enter the volatility. Compared
    at the RAW (pre-clamp) mean/std ratios so the demonstration doesn't hide under the
    ±10 clamp that heavy annualization would otherwise impose on both."""
    # A mostly-flat curve with a handful of moves of MIXED sign (real dispersion).
    r = [0.0, 0.0, 0.02, 0.0, -0.01, 0.0, 0.0, 0.03, 0.0, 0.0]
    equities = [10_000.0]
    for x in r:
        equities.append(equities[-1] * (1.0 + x))
    curve = _curve(equities)
    # The "event" series = the non-flat moves the strategy actually took.
    event_pnls = np.array([x for x in r if x != 0.0])
    bar_returns = np.array(r)  # calendar series, flat bars included

    # Raw (un-annualized, un-clamped) Sharpe ratios: bar dilutes the mean toward zero.
    event_ratio = event_pnls.mean() / event_pnls.std()
    bar_ratio = bar_returns.mean() / bar_returns.std()
    assert 0.0 < bar_ratio < event_ratio, "flat bars must lower the calendar Sharpe ratio"

    # And the engine's `sharpe` is the bar-level one (primary) while trade_sharpe is event.
    m = _compute_basic_metrics(
        [{"pnl_pct": float(x), "bars_held": 1} for x in event_pnls],
        total_bars=len(curve), timeframe="1w", equity_curve=curve,
    )
    assert float(m["sharpe"]) != float(m["trade_sharpe"])


def test_all_flat_curve_sharpe_is_zero_not_nan():
    """Division-by-zero guard: a curve that never moves (all-flat) yields sharpe 0."""
    curve = _curve([10_000.0, 10_000.0, 10_000.0, 10_000.0])
    m = _compute_basic_metrics(
        [{"pnl_pct": 0.0, "bars_held": 1}], total_bars=len(curve), timeframe="1h", equity_curve=curve
    )
    assert float(m["sharpe"]) == 0.0
    assert float(m["sortino"]) == 0.0


def test_no_curve_keeps_event_sharpe_as_primary():
    """Legacy fallback: with NO equity curve, `sharpe` stays the trade-based value and
    equals trade_sharpe (nothing breaks for metric-only callers)."""
    trades = [
        {"pnl_pct": 0.05, "bars_held": 3},
        {"pnl_pct": -0.02, "bars_held": 2},
        {"pnl_pct": 0.03, "bars_held": 4},
    ]
    m = _compute_basic_metrics(trades, total_bars=8760, timeframe="1h")  # no equity_curve
    assert float(m["sharpe"]) == float(m["trade_sharpe"])
    assert float(m["sortino"]) == float(m["trade_sortino"])
    assert m["sharpe"] != 0.0  # dispersion present → non-trivial


def test_simultaneous_hedged_exits_are_additive_not_sequentially_compounded():
    idx = pd.date_range("2025-01-01", periods=2, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100.0, 100.0],
            "high": [100.0, 100.0],
            "low": [100.0, 100.0],
            "close": [100.0, 100.0],
            "volume": [1.0, 1.0],
        },
        index=idx,
    )
    trades = [
        {
            "entry_time": str(idx[0]),
            "exit_time": str(idx[1]),
            "entry_price": 100.0,
            "exit_price": 110.0,
            "direction": "long",
            "leverage": 1.0,
            "size_fraction_raw": 1.0,
            "pnl_pct": 0.10,
            "bars_held": 1,
        },
        {
            "entry_time": str(idx[0]),
            "exit_time": str(idx[1]),
            "entry_price": 100.0,
            "exit_price": 90.0,
            "direction": "short",
            "leverage": 1.0,
            "size_fraction_raw": 1.0,
            "pnl_pct": 0.10,
            "bars_held": 1,
        },
    ]

    curve = _build_equity_curve_from_trades(trades, df, 10_000.0)
    metrics = compute_metrics(trades, total_bars=len(df), equity_curve=curve)

    assert curve[-1]["equity"] == pytest.approx(12_000.0)
    assert float(metrics["total_return_pct"]) == pytest.approx(0.20)
