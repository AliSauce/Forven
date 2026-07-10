"""Data-leak / lookahead gates (2026-06-15).

A future-bar leak (e.g. `.shift(-1)`) makes a strategy's metrics impossibly good
on BOTH the IS and OOS slices, so the IS/OOS-gap overfit detector (gap ~0) and
the win-rate trap (needs PF < 1.2) both miss it, and it sails into PAPER.

GATE A (forven.policy._implausible_metrics_reason): reject Sharpe >= 5 / PF >= 8
(or a Sharpe pegged at the +/-10 backtest clamp) on either slice, at quick_screen
(primary, universal) and the gauntlet gate (defense-in-depth).

GATE B (forven.strategies.lookahead_probe.detect_lookahead): a truncation-
invariance probe at registration — a causal signal at bar t must be unchanged
when bars after t are withheld; if it flips, the strategy reads the future.
"""

import json
from datetime import datetime, timezone

import pandas as pd

from forven.db import get_db
from forven.policy import (
    _evaluate_quick_screen_gate,
    _implausible_metrics_reason,
    load_pipeline_config,
)
from forven.strategies.lookahead_probe import detect_lookahead


# ============================ GATE A: helper ===============================

def test_implausible_reason_flags_clamped_oos_sharpe():
    cfg = load_pipeline_config()
    metrics = {
        "in_sample": {"sharpe": 1.4, "profit_factor": 1.3},
        "out_of_sample": {"sharpe": 10.0, "profit_factor": 1.3},
    }
    reason = _implausible_metrics_reason(metrics, cfg)
    assert reason is not None
    low = reason.lower()
    assert "implausible" in low and "leak" in low


def test_implausible_reason_flags_absurd_is_profit_factor():
    cfg = load_pipeline_config()
    metrics = {
        "in_sample": {"sharpe": 2.0, "profit_factor": 15.0},
        "out_of_sample": {"sharpe": 1.8, "profit_factor": 1.4},
    }
    reason = _implausible_metrics_reason(metrics, cfg)
    assert reason is not None
    assert "pf" in reason.lower() or "profit" in reason.lower()


def test_implausible_reason_none_for_sane_metrics():
    cfg = load_pipeline_config()
    metrics = {
        "in_sample": {"sharpe": 1.5, "profit_factor": 1.4},
        "out_of_sample": {"sharpe": 1.1, "profit_factor": 1.35},
    }
    assert _implausible_metrics_reason(metrics, cfg) is None


def test_implausible_reason_none_for_empty_metrics():
    cfg = load_pipeline_config()
    assert _implausible_metrics_reason({}, cfg) is None
    assert _implausible_metrics_reason(None, cfg) is None


def test_implausible_reason_robust_to_none_values():
    cfg = load_pipeline_config()
    # None sharpe/pf must not crash — treated as absent.
    metrics = {
        "in_sample": {"sharpe": None, "profit_factor": None},
        "out_of_sample": {"sharpe": 1.0, "profit_factor": None},
    }
    assert _implausible_metrics_reason(metrics, cfg) is None


# ====================== GATE A: quick_screen gate ==========================

def _insert_strategy(sid, *, metrics, stage="quick_screen"):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, metrics, "
            "status, owner, stage, stage_changed_at, created_at, updated_at) "
            "VALUES (?, ?, 'rsi_momentum', 'ETH', '1h', '{}', ?, 'active', 'brain', ?, ?, ?, ?)",
            (sid, sid, json.dumps(metrics), stage, now, now, now),
        )
        conn.commit()


# Healthy distinct IS/OOS slices that clear every other quick-screen guardrail
# (min_trades, IS Sharpe > 0.1, gap <= 1.5, robustness >= 10, PF floors, win
# trap). Used as the sane baseline; leak variants override sharpe/PF.
def _sane_metrics(**overrides):
    m = {
        "total_trades": 45,
        "total_return_pct": 12.0,
        "max_drawdown_pct": 0.12,
        "robustness_score": 60.0,
        "in_sample": {
            "sharpe": 1.4, "profit_factor": 1.5, "total_trades": 45,
            "total_return_pct": 18.0, "max_drawdown_pct": 0.12, "win_rate": 45.0,
        },
        "out_of_sample": {
            "sharpe": 1.0, "profit_factor": 1.3, "total_trades": 40,
            "total_return_pct": 8.0, "max_drawdown_pct": 0.12, "win_rate": 42.0,
        },
    }
    m.update(overrides)
    return m


def test_quick_screen_rejects_leak_metrics(forven_db):
    # Loosen the win-rate-trap-irrelevant structural floors are not in play here;
    # quick_screen reads metrics directly off the row.
    metrics = _sane_metrics(
        out_of_sample={
            "sharpe": 10.0, "profit_factor": 15.0, "total_trades": 40,
            "total_return_pct": 5000.0, "max_drawdown_pct": 0.02, "win_rate": 79.0,
        },
    )
    _insert_strategy("qs-leak", metrics=metrics)
    passed, reason = _evaluate_quick_screen_gate("qs-leak", load_pipeline_config())
    assert passed is False
    low = reason.lower()
    assert "implausible" in low or "leak" in low, reason


def test_quick_screen_passes_sane_strategy(forven_db):
    _insert_strategy("qs-sane", metrics=_sane_metrics())
    passed, reason = _evaluate_quick_screen_gate("qs-sane", load_pipeline_config())
    assert passed is True, reason
    assert "implausible" not in reason.lower()


# ============================ GATE B: probe ================================

class _LeakStrategy:
    """generate_signals reads the FUTURE bar via .shift(-1) — a lookahead leak."""

    strategy_id = "leak-strat"

    def generate_signals(self, df: pd.DataFrame):
        close = df["close"]
        # Enter when the NEXT bar's close is higher than now (impossible to know
        # at bar t). .shift(-1) pulls bar t+1 back to t => future leak.
        future = close.shift(-1)
        entries = (future > close).fillna(False)
        exits = (future < close).fillna(False)
        return entries, exits


class _CausalStrategy:
    """Clean causal breakout: only uses past/current bars (.shift(+1))."""

    strategy_id = "causal-strat"

    def generate_signals(self, df: pd.DataFrame):
        close = df["close"]
        prior_high = close.rolling(20).max().shift(1)
        entries = (close > prior_high).fillna(False)
        exits = (close < close.rolling(20).min().shift(1)).fillna(False)
        return entries, exits


def test_detect_lookahead_flags_future_shift():
    reason = detect_lookahead(_LeakStrategy())
    assert reason is not None
    low = reason.lower()
    assert "lookahead" in low
    assert "future" in low


def test_detect_lookahead_passes_causal_strategy():
    assert detect_lookahead(_CausalStrategy()) is None


def test_detect_lookahead_none_without_generate_signals():
    class _NoVectorized:
        strategy_id = "no-vec"

    assert detect_lookahead(_NoVectorized()) is None


def test_detect_lookahead_blocks_strategy_originated_probe_errors():
    class _Boom:
        strategy_id = "boom"

        def generate_signals(self, df):
            raise RuntimeError("kaboom")

    reason = detect_lookahead(_Boom())
    assert reason is not None
    assert "causal execution could not be verified" in reason
