"""Tests for S00152 overfitting guardrails in _evaluate_paper_gate."""

import json
from datetime import datetime, timezone, timedelta

from forven.db import get_db
from forven.policy import _evaluate_paper_gate, _check_paper_trades, _check_paper_return, DEFAULT_PIPELINE_CONFIG


def _insert_strategy(conn, sid, *, metrics=None, stage_changed_at=None):
    """Insert a strategy row for testing."""
    if stage_changed_at is None:
        stage_changed_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    conn.execute(
        "INSERT INTO strategies (id, name, type, stage, stage_changed_at, metrics) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sid, sid, "rsi_momentum", "paper_trading", stage_changed_at, json.dumps(metrics or {})),
    )
    conn.commit()


def _insert_paper_trades(conn, sid, pnls, *, stage_changed_at=None):
    """Insert closed paper trades for a strategy.

    Rows carry pnl_is_equity_fraction=true (the kernel-managed parity marker the
    promotion gate now requires) so these represent valid equity-fraction paper
    trades — see PROMOTION-GATE-PARITY-2/3 / policy._PARITY_PNL_FILTER.
    """
    base = datetime.now(timezone.utc) - timedelta(days=20)
    for i, pnl in enumerate(pnls):
        closed_at = (base + timedelta(hours=i)).isoformat()
        conn.execute(
            "INSERT INTO trades (id, strategy_id, strategy, asset, direction, status, pnl_pct, "
            "execution_type, closed_at, signal_data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"t-{sid}-{i}", sid, sid, "BTC/USDT", "long", "CLOSED", pnl, "paper", closed_at,
             '{"pnl_is_equity_fraction": true}'),
        )
    conn.commit()


def _insert_legacy_margin_trades(conn, sid, pnls, *, flag=None):
    """Insert closed paper rows WITHOUT the equity-fraction parity flag (legacy /
    margin-scale / converge artifacts the gate must exclude)."""
    base = datetime.now(timezone.utc) - timedelta(days=19)
    sd = json.dumps(flag) if flag else None
    for i, pnl in enumerate(pnls):
        conn.execute(
            "INSERT INTO trades (id, strategy_id, strategy, asset, direction, status, pnl_pct, "
            "execution_type, closed_at, signal_data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"legacy-{sid}-{i}", sid, sid, "BTC/USDT", "long", "CLOSED", pnl, "paper",
             (base + timedelta(hours=i)).isoformat(), sd),
        )
    conn.commit()


def test_gate_excludes_non_equity_fraction_rows(forven_db):
    """PROMOTION-GATE-PARITY-2/3: only kernel equity-fraction paper rows (flagged
    pnl_is_equity_fraction) count toward the promotion gate. Legacy/margin-scale rows
    must NOT pad the sample or compound as (much larger) equity returns."""
    with get_db() as conn:
        _insert_strategy(conn, "s-mixed", metrics={"profit_factor": 3.0})
        _insert_paper_trades(conn, "s-mixed", [0.01] * 12)  # 12 valid equity-fraction rows
        # 5 legacy margin-scale rows + a converge artifact — none flagged.
        _insert_legacy_margin_trades(conn, "s-mixed", [0.30] * 5, flag={"non_vectorizable_legacy": True})

    # Sample counts ONLY the 12 flagged rows (not 17).
    _, msg = _check_paper_trades("s-mixed")
    assert "12/" in msg, msg

    # And the excluded +30% margin rows can't inflate the compounded paper return.
    passed, ret_msg = _check_paper_return("s-mixed")
    assert passed  # the 12 small positive equity-fraction trades are net positive
    # 12 * +1% compounded ≈ +12.7%, NOT the ~3700% five +30% margin rows would add.
    pct = float(ret_msg.split(":")[1].strip().rstrip("%"))
    assert pct < 50.0, ret_msg


# ---------------------------------------------------------------------------
# S00152: OOS >> IS Sharpe overfitting flag
# ---------------------------------------------------------------------------


def test_oos_sharpe_much_higher_than_is_sharpe_rejects(forven_db):
    """OOS Sharpe > 1.5x IS Sharpe should be rejected as overfitting risk."""
    with get_db() as conn:
        _insert_strategy(conn, "s-overfit", metrics={
            "is_sharpe": 1.0,
            "oos_sharpe": 2.0,  # 2.0x IS → exceeds 1.5x limit
        })
        _insert_paper_trades(conn, "s-overfit", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("s-overfit", DEFAULT_PIPELINE_CONFIG)
    assert not passed
    assert "S00152 REJECT" in msg
    assert "OVERFITTING" in msg


def test_oos_sharpe_within_ratio_passes(forven_db):
    """OOS Sharpe <= 1.5x IS Sharpe should not trigger overfitting flag."""
    with get_db() as conn:
        _insert_strategy(conn, "s-ok-sharpe", metrics={
            "is_sharpe": 1.5,
            "oos_sharpe": 2.0,  # 1.33x IS → under 1.5x limit
            "profit_factor": 3.0,
        })
        _insert_paper_trades(conn, "s-ok-sharpe", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("s-ok-sharpe", DEFAULT_PIPELINE_CONFIG)
    assert passed


def test_oos_sharpe_check_skipped_when_is_zero(forven_db):
    """If IS Sharpe is zero, the OOS/IS ratio check should be skipped."""
    with get_db() as conn:
        _insert_strategy(conn, "s-zero-is", metrics={
            "is_sharpe": 0.0,
            "oos_sharpe": 3.0,
            "profit_factor": 3.0,
        })
        _insert_paper_trades(conn, "s-zero-is", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("s-zero-is", DEFAULT_PIPELINE_CONFIG)
    assert passed


# ---------------------------------------------------------------------------
# S00152: Profit Factor thresholds
# ---------------------------------------------------------------------------


def test_profit_factor_below_1_5_rejects(forven_db):
    """PF < 1.5 should be hard-rejected."""
    with get_db() as conn:
        _insert_strategy(conn, "s-low-pf", metrics={
            "profit_factor": 1.2,
        })
        _insert_paper_trades(conn, "s-low-pf", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("s-low-pf", DEFAULT_PIPELINE_CONFIG)
    assert not passed
    assert "S00152 REJECT" in msg
    assert "Profit Factor" in msg


def test_profit_factor_between_1_5_and_2_0_passes_with_size_reduction(forven_db):
    """PF between 1.5 and 2.0 should pass but with 50% position sizing reduction."""
    with get_db() as conn:
        _insert_strategy(conn, "s-mid-pf", metrics={
            "profit_factor": 1.7,
        })
        _insert_paper_trades(conn, "s-mid-pf", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("s-mid-pf", DEFAULT_PIPELINE_CONFIG)
    assert passed
    assert "50% size reduction" in msg
    assert "S00152 PF warning" in msg


def test_profit_factor_above_2_0_passes_normally(forven_db):
    """PF >= 2.0 should pass without size reduction."""
    with get_db() as conn:
        _insert_strategy(conn, "s-good-pf", metrics={
            "profit_factor": 2.5,
        })
        _insert_paper_trades(conn, "s-good-pf", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("s-good-pf", DEFAULT_PIPELINE_CONFIG)
    assert passed
    assert "50% size reduction" not in msg


# ---------------------------------------------------------------------------
# S00152: Extended paper trading (min closed-trades floor; Default preset = 10)
# ---------------------------------------------------------------------------


def test_insufficient_paper_trades_rejects(forven_db):
    """Fewer than the Default min_closed_trades (10) should be rejected."""
    with get_db() as conn:
        _insert_strategy(conn, "s-few-trades", metrics={"profit_factor": 3.0})
        _insert_paper_trades(conn, "s-few-trades", [1.0] * 5)

    passed, msg = _evaluate_paper_gate("s-few-trades", DEFAULT_PIPELINE_CONFIG)
    assert not passed
    assert "5/10" in msg


def test_insufficient_paper_sample_precedes_static_pf_reject(forven_db):
    """Forward paper evidence should block before static PF hard-fails."""
    with get_db() as conn:
        _insert_strategy(conn, "s-few-trades-low-pf", metrics={"profit_factor": 1.2})
        _insert_paper_trades(conn, "s-few-trades-low-pf", [1.0] * 5)

    passed, msg = _evaluate_paper_gate("s-few-trades-low-pf", DEFAULT_PIPELINE_CONFIG)
    assert not passed
    assert msg == "Insufficient paper sample: 5/10 closed trades"


def test_exactly_50_trades_passes(forven_db):
    """Exactly 50 trades should meet the minimum."""
    with get_db() as conn:
        _insert_strategy(conn, "s-fifty", metrics={"profit_factor": 3.0})
        _insert_paper_trades(conn, "s-fifty", [1.0] * 50)

    passed, msg = _evaluate_paper_gate("s-fifty", DEFAULT_PIPELINE_CONFIG)
    assert passed


# ---------------------------------------------------------------------------
# S00152: Must have positive paper return
# ---------------------------------------------------------------------------


def test_negative_paper_return_rejects(forven_db):
    """Paper return <= 0 should be rejected even with enough trades."""
    with get_db() as conn:
        _insert_strategy(conn, "s-neg-return", metrics={"profit_factor": 3.0})
        # 30 wins + 30 losses that net to negative
        pnls = [2.0] * 25 + [-2.5] * 25 + [-1.0] * 10
        _insert_paper_trades(conn, "s-neg-return", pnls)

    passed, msg = _evaluate_paper_gate("s-neg-return", DEFAULT_PIPELINE_CONFIG)
    assert not passed
    # Should hit either the S00152 positive return check or the general return check
    assert "return" in msg.lower()


def test_zero_paper_return_rejects(forven_db):
    """Paper return of exactly 0 should be rejected (must be > 0)."""
    with get_db() as conn:
        _insert_strategy(conn, "s-zero-return", metrics={"profit_factor": 3.0})
        # 50 trades that net to exactly zero
        pnls = [1.0] * 25 + [-1.0] * 25
        _insert_paper_trades(conn, "s-zero-return", pnls)

    passed, msg = _evaluate_paper_gate("s-zero-return", DEFAULT_PIPELINE_CONFIG)
    assert not passed


# ---------------------------------------------------------------------------
# S00152: Paper drawdown limit
# ---------------------------------------------------------------------------


def test_paper_drawdown_exceeding_limit_rejects(forven_db):
    """Paper max drawdown >= 15% should be rejected."""
    with get_db() as conn:
        _insert_strategy(conn, "s-high-dd", metrics={"profit_factor": 3.0})
        # Compounding return: gains then a 20% drop, then recovery to net positive
        # PnL values are fractional (e.g., 0.02 = +2%, -0.20 = -20%)
        pnls = [0.02] * 20 + [-0.20] + [0.02] * 39
        _insert_paper_trades(conn, "s-high-dd", pnls)

    passed, msg = _evaluate_paper_gate("s-high-dd", DEFAULT_PIPELINE_CONFIG)
    assert not passed
    assert "drawdown" in msg.lower()


# ---------------------------------------------------------------------------
# Strategy not found
# ---------------------------------------------------------------------------


def test_missing_strategy_rejects(forven_db):
    """Non-existent strategy should be rejected gracefully."""
    passed, msg = _evaluate_paper_gate("nonexistent-id", DEFAULT_PIPELINE_CONFIG)
    assert not passed
    assert "not found" in msg.lower()


# ---------------------------------------------------------------------------
# S00152: OOS profit factor preferred over general PF
# ---------------------------------------------------------------------------


def test_oos_profit_factor_used_when_available(forven_db):
    """OOS profit factor should be used for evaluation when available."""
    with get_db() as conn:
        _insert_strategy(conn, "s-oos-pf", metrics={
            "profit_factor": 3.0,  # general PF is fine
            "oos_profit_factor": 1.2,  # but OOS PF is below 1.5
        })
        _insert_paper_trades(conn, "s-oos-pf", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("s-oos-pf", DEFAULT_PIPELINE_CONFIG)
    assert not passed
    assert "Profit Factor" in msg


def test_general_pf_fallback_when_no_oos(forven_db):
    """When OOS PF not available, general PF should be used."""
    with get_db() as conn:
        _insert_strategy(conn, "s-gen-pf", metrics={
            "profit_factor": 1.7,  # between 1.5-2.0 → 50% reduction
        })
        _insert_paper_trades(conn, "s-gen-pf", [1.0] * 60)

    passed, msg = _evaluate_paper_gate("s-gen-pf", DEFAULT_PIPELINE_CONFIG)
    assert passed
    assert "50% size reduction" in msg
