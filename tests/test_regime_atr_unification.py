"""v5 regime ATR-ratio unification.

The ATR ratio that feeds ``regime._classify`` used to be computed inline at three sites
with drifting conventions: the backtest signal-walk (``_precompute_regimes``) used a
44-bar baseline while robustness (``_detect_entry_regime``) and the live detector used a
30-bar baseline (``tr[-44:-14]``). So a bar could classify to a different regime in the
backtest vs. live/robustness. v5 routes ALL three through
``forven.regime.regime_atr_ratio_*`` so the baseline is identical everywhere.

These tests lock the equivalence: the vectorized signal-walk ratio at bar i equals the
window-based scalar at the same bar, and the two backtest regime paths agree bar-for-bar.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forven import regime
from forven.strategies import backtest as bt


def _synth_frame(n: int = 400, seed: int = 11) -> pd.DataFrame:
    """A volatile random walk so ATR ratios span calm and spiky regimes."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0004, 0.02, size=n).cumsum()
    close = 100.0 * np.exp(steps)
    spread = np.abs(rng.normal(0.0, 0.02, size=n)) + 0.003
    high = close * (1.0 + spread)
    low = close * (1.0 - spread)
    openp = np.empty(n)
    openp[0] = close[0]
    openp[1:] = close[:-1]
    high = np.maximum.reduce([high, openp, close])
    low = np.minimum.reduce([low, openp, close])
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": openp, "high": high, "low": low, "close": close, "volume": 1_000.0},
        index=idx,
    )


def test_vectorized_ratio_matches_window_scalar_at_each_bar():
    """regime_atr_ratio_series(...).iloc[i] == regime_atr_ratio_at(window ending at i)."""
    df = _synth_frame()
    series = regime.regime_atr_ratio_series(df["high"], df["low"], df["close"])
    # Check several bars deep enough to have a full baseline window.
    for i in (60, 120, 250, 399):
        window = df.iloc[: i + 1]
        scalar = regime.regime_atr_ratio_at(window["high"], window["low"], window["close"])
        vec = series.iloc[i]
        if np.isnan(vec):
            # warmup NaN → scalar substitutes the default; skip the pointwise compare
            continue
        assert scalar == pytest.approx(float(vec), rel=1e-9, abs=1e-9)


def test_scalar_helper_reproduces_legacy_30bar_window_math():
    """The unified scalar equals the LEGACY robustness inline math (tr[-14:] mean /
    tr[-44:-14] mean) — proving we unified onto the 30-bar baseline, not something new."""
    df = _synth_frame().iloc[:120]
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1).to_numpy()
    atr_current = float(np.nanmean(tr[-14:]))
    atr_avg = float(np.nanmean(tr[-44:-14]))
    legacy_ratio = atr_current / atr_avg
    unified = regime.regime_atr_ratio_at(high, low, close)
    assert unified == pytest.approx(legacy_ratio, rel=1e-9)


def test_both_backtest_regime_paths_agree_bar_for_bar():
    """The signal-walk stamping (_precompute_regimes) and the robustness entry-regime
    detector (_detect_entry_regime) must classify each bar to the SAME regime now that
    they share the ATR baseline (ADX/EMA/RSI were already identical inputs)."""
    df = _synth_frame(n=420)
    walk_regimes = bt._precompute_regimes(df)

    mismatches = []
    # _detect_entry_regime needs >=210 bars of context; compare where both are defined.
    for i in range(230, len(df)):
        window = df.iloc[: i + 1]
        entry_regime = bt._detect_entry_regime(window)
        walk_regime = walk_regimes.iloc[i]
        if entry_regime != walk_regime:
            mismatches.append((i, walk_regime, entry_regime))

    # Allow zero mismatches — same inputs, same classifier, same ATR baseline.
    assert not mismatches, f"{len(mismatches)} bars classified differently, e.g. {mismatches[:5]}"
