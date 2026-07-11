"""Isolated-subprocess offload of the GIL-bound robustness compute hotspots.

Two robustness steps do heavy pandas/numpy work in-thread on an API worker (the
multi-second event-loop stalls attributed to gauntlet compute):

  * Monte Carlo bootstrap — ``n_simulations`` (default 1000) seeded resamples.
  * Regime split classification — per-trade RSI/ADX/EMA over the entry-bar prefix.

Both are now routed through the SAME process-wide subprocess budget/isolation the
isolated backtests use (``strategies/concurrency.py``). These tests pin the contract
the offload relies on: the module-level worker is deterministic and correct, the
dispatcher runs inline under pytest (so the rest of the gauntlet stays deterministic),
and — most importantly — the subprocess path produces a BIT-IDENTICAL result to the
inline path (the seeds/compute live inside the worker, so isolation cannot change the
verdict).
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from forven.routers import robustness as R


def _sample_returns(n: int = 120, seed: int = 7) -> list[float]:
    rng = np.random.default_rng(seed)
    return [max(float(x), -0.999) for x in rng.normal(0.01, 0.05, n)]


_MC_KW = dict(
    original_sharpe=1.2,
    original_return=15.0,
    n_simulations=250,
    initial_capital=10000.0,
    mc_profitable_min=65.0,
    max_dd_p95_limit_pct=40.0,
)


# --------------------------------------------------------------------------- #
# Monte Carlo bootstrap worker
# --------------------------------------------------------------------------- #
def test_monte_carlo_worker_is_deterministic_and_well_formed():
    returns = _sample_returns()
    a = R._monte_carlo_bootstrap_worker(returns, **_MC_KW)
    b = R._monte_carlo_bootstrap_worker(returns, **_MC_KW)
    # Seeded inside the worker -> byte-identical across calls.
    assert json.dumps(a, default=str, sort_keys=True) == json.dumps(b, default=str, sort_keys=True)
    assert a["method"] == "trade_bootstrap"
    assert a["verdict"] in {"PASS", "FAIL"}
    assert a["n_simulations"] == _MC_KW["n_simulations"]
    assert a["n_trades"] == len(returns)
    # equity_paths are capped so the offload payload stays bounded.
    assert len(a["equity_paths"]) <= 50


def test_monte_carlo_verdict_thresholds_are_honored():
    returns = _sample_returns()
    # An unreachable probability floor forces FAIL; a trivially-met one allows PASS.
    fail = R._monte_carlo_bootstrap_worker(returns, **{**_MC_KW, "mc_profitable_min": 200.0})
    assert fail["verdict"] == "FAIL"
    assert any("probability profitable" in r for r in fail["verdict_reasons"])


def test_monte_carlo_dispatch_runs_inline_under_pytest():
    # Under pytest _should_use_process_isolation() is False, so the dispatcher must
    # run inline and match the worker exactly (no subprocess spawned).
    assert "PYTEST_CURRENT_TEST" in os.environ
    returns = _sample_returns()
    dispatched = R._run_monte_carlo_bootstrap(returns, **_MC_KW)
    inline = R._monte_carlo_bootstrap_worker(returns, **_MC_KW)
    assert json.dumps(dispatched, default=str, sort_keys=True) == json.dumps(
        inline, default=str, sort_keys=True
    )


def test_monte_carlo_subprocess_path_is_bit_identical(monkeypatch):
    # Force real process isolation and prove the child-process result is byte-identical
    # to the inline result — the offload must never change a verdict.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("FORVEN_BACKTEST_PROCESS_ISOLATION", "1")
    returns = _sample_returns()
    inline = R._monte_carlo_bootstrap_worker(returns, **_MC_KW)
    isolated = R._run_monte_carlo_bootstrap(returns, **_MC_KW)
    assert json.dumps(isolated, default=str, sort_keys=True) == json.dumps(
        inline, default=str, sort_keys=True
    )


# --------------------------------------------------------------------------- #
# Regime-split classification worker
# --------------------------------------------------------------------------- #
def _regime_fixture(n_trades: int = 40, seed: int = 3):
    idx = pd.date_range("2024-01-01", periods=720, freq="h", tz="UTC")
    close = 100 + np.cumsum(np.random.default_rng(seed).normal(0, 1, 720))
    candles = pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000.0},
        index=idx,
    )
    trades = [
        {
            "entry_time": idx[300 + i].isoformat(),
            "return_pct": float(np.random.default_rng(i).normal(0.5, 2)),
            "pnl": 10.0,
        }
        for i in range(n_trades)
    ]
    return candles, trades


def test_regime_worker_classifies_and_is_deterministic():
    candles, trades = _regime_fixture()
    a = R._regime_classify_trades_worker(candles, trades)
    b = R._regime_classify_trades_worker(candles, trades)
    assert json.dumps(a, default=str, sort_keys=True) == json.dumps(b, default=str, sort_keys=True)
    # Every trade is either classified into a regime or counted as unresolved.
    classified = sum(len(v) for v in a["by_regime_returns"].values())
    assert classified + a["unresolved_trades"] == len(trades)
    assert set(a["by_regime_pnl"].keys()) == set(a["by_regime_returns"].keys())


def test_regime_dispatch_runs_inline_under_pytest():
    assert "PYTEST_CURRENT_TEST" in os.environ
    candles, trades = _regime_fixture()
    dispatched = R._run_regime_classification(candles, trades)
    inline = R._regime_classify_trades_worker(candles, trades)
    assert json.dumps(dispatched, default=str, sort_keys=True) == json.dumps(
        inline, default=str, sort_keys=True
    )


def test_regime_subprocess_path_is_bit_identical(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("FORVEN_BACKTEST_PROCESS_ISOLATION", "1")
    candles, trades = _regime_fixture()
    inline = R._regime_classify_trades_worker(candles, trades)
    isolated = R._run_regime_classification(candles, trades)
    assert json.dumps(isolated, default=str, sort_keys=True) == json.dumps(
        inline, default=str, sort_keys=True
    )


def test_regime_worker_counts_missing_entry_time_as_unresolved():
    candles, _ = _regime_fixture(n_trades=1)
    trades = [{"return_pct": 1.0, "pnl": 5.0}]  # no entry_time
    out = R._regime_classify_trades_worker(candles, trades)
    assert out["unresolved_trades"] == 1
    assert out["unresolved_reasons"].get("missing_entry_time") == 1
    assert out["by_regime_returns"] == {}
