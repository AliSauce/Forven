"""Robustness suite audit — Phase 2: two-tier gate (achievable paper / strict live).

The gauntlet->paper gate is LEAN (OOS consistency, param-jitter, drawdown/tail
safety, min-trades). The strict-live criteria (WFA IS->OOS degradation, absolute
OOS Sharpe, OOS trade count, Monte-Carlo percentile, cost-stress survival, regime
consistency) are DEMOTED to advisory at paper and ENFORCED at paper->live via
_strict_robustness_reject. Win-rate is no longer a hard gate.
"""

import json
from datetime import datetime, timezone

from forven.db import get_db, kv_set
from forven.policy import (
    _strict_robustness_reject,
    evaluate_promotion,
    load_pipeline_config,
)


def _row(sid):
    with get_db() as conn:
        return conn.execute("SELECT * FROM strategies WHERE id = ?", (sid,)).fetchone()


def _insert_strategy(sid, *, stage="gauntlet", metrics=None):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, metrics, status, owner, stage, stage_changed_at, created_at, updated_at) "
            "VALUES (?, ?, 'rsi_momentum', 'ETH', '1h', '{}', ?, ?, 'brain', ?, ?, ?, ?)",
            (sid, sid, json.dumps(metrics or {}), stage, stage, now, now, now),
        )
        conn.commit()


def _insert_result(sid, result_type, metrics):
    rid = f"{result_type}-{sid}"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at) "
            "VALUES (?, ?, ?, 'ETH', '1h', ?, '{\"status\":\"succeeded\"}', datetime('now'))",
            (rid, sid, result_type, json.dumps(metrics)),
        )
        conn.commit()


def _wf(passing=True, degradation=0.10):
    return {
        "verdict": "PASS" if passing else "FAIL",
        "degradation": degradation,
        "avg_oos_sharpe": 1.0,
        "total_oos_trades": 40,
        "splits": [
            {"out_of_sample": {"sharpe": 1.2, "total_trades": 20}},
            {"out_of_sample": {"sharpe": 0.9, "total_trades": 18}},
            {"out_of_sample": {"sharpe": 0.7, "total_trades": 15}},
        ],
    }


# --- strict-live battery (the demoted criteria are enforced here) ----------

def test_strict_reject_fires_on_cost_stress_survival(forven_db):
    _insert_strategy("st-cost")
    _insert_result("st-cost", "cost_stress", {"verdict": "PASS", "degradation_pct": 20.0, "stressed": {"sharpe": 0.1}})
    reason = _strict_robustness_reject("st-cost", _row("st-cost"), {}, load_pipeline_config())
    assert reason is not None and "cost-stress" in reason.lower()


def test_strict_reject_fires_on_regime_consistency(forven_db):
    _insert_strategy("st-regime")
    _insert_result("st-regime", "regime_split", {"verdict": "PASS", "n_regimes": 3, "profitable_regime_share": 0.33})
    reason = _strict_robustness_reject("st-regime", _row("st-regime"), {}, load_pipeline_config())
    assert reason is not None and "regime" in reason.lower()


def test_strict_reject_fires_on_wfa_degradation(forven_db):
    _insert_strategy("st-deg")
    _insert_result("st-deg", "walk_forward", _wf(passing=True, degradation=0.55))
    reason = _strict_robustness_reject("st-deg", _row("st-deg"), {}, load_pipeline_config())
    assert reason is not None and "degradation" in reason.lower()


def test_strict_reject_passes_clean_strategy(forven_db):
    _insert_strategy("st-clean")
    _insert_result("st-clean", "walk_forward", _wf(passing=True, degradation=0.10))
    _insert_result("st-clean", "cost_stress", {"verdict": "PASS", "degradation_pct": 20.0, "stressed": {"sharpe": 0.6}})
    _insert_result("st-clean", "regime_split", {"verdict": "PASS", "n_regimes": 3, "profitable_regime_share": 0.75})
    _insert_result("st-clean", "monte_carlo", {"verdict": "PASS", "n_trades": 40, "percentile_score": 0.8, "max_dd_p95_ratio": 0.15})
    assert _strict_robustness_reject("st-clean", _row("st-clean"), {}, load_pipeline_config()) is None


def test_strict_reject_fails_closed_when_evidence_extraction_errors(forven_db, monkeypatch):
    import forven.policy as policy

    _insert_strategy("st-extract-error")
    monkeypatch.setattr(
        policy,
        "_extract_gauntlet_verdict_payloads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("artifact store busy")),
    )

    reason = _strict_robustness_reject(
        "st-extract-error",
        _row("st-extract-error"),
        {},
        load_pipeline_config(),
    )

    assert reason is not None
    assert "robustness evidence unavailable" in reason.lower()
    assert "artifact store busy" in reason


def test_strict_reject_fails_closed_without_gauntlet_artifacts(forven_db):
    _insert_strategy("st-no-artifacts")

    reason = _strict_robustness_reject(
        "st-no-artifacts",
        _row("st-no-artifacts"),
        {},
        load_pipeline_config(),
    )

    assert reason is not None
    assert "no usable gauntlet artifacts" in reason.lower()


# --- lean paper gate: strict failures do NOT block ->paper -----------------

def test_paper_gate_allows_strategy_that_fails_strict_live(forven_db):
    # Disable structural readiness gates so we isolate the robustness logic.
    kv_set("forven:pipeline:settings", {
        "gate_multi_tf_sweep_enabled": False,
        "gate_optimization_required_enabled": False,
        "gate_params_applied_enabled": False,
        "gate_confirmation_backtest_enabled": False,
        "gate_artifact_ordering_enabled": False,
        "gate_validation_freshness_enabled": False,
        "gate_require_artifact_rows_enabled": False,
    })
    sid = "lean-paper"
    _insert_strategy(sid, stage="gauntlet", metrics={
        "out_of_sample": {"total_return_pct": 6.0, "max_drawdown_pct": 0.10, "win_rate": 0.30,
                          "sharpe": 1.1, "profit_factor": 1.6, "total_trades": 45},
        "total_return_pct": 6.0, "sharpe_ratio": 1.1, "profit_factor": 1.6, "total_trades": 45,
    })
    # Optimization artifact must precede the robustness results (ordering gate).
    _insert_result(sid, "optimization", {"status": "succeeded"})
    # All robustness verdicts PASS (so robustness score = 3/3), but the strict
    # dimensions are bad: high WFA degradation, low cost-stressed Sharpe, regimes <50%.
    _insert_result(sid, "walk_forward", _wf(passing=True, degradation=0.55))
    _insert_result(sid, "param_jitter", {"verdict": "PASS", "n_iterations": 30, "pct_positive_sharpe": 82.0, "pass_rate": 0.7})
    _insert_result(sid, "cost_stress", {"verdict": "PASS", "degradation_pct": 20.0, "stressed": {"sharpe": 0.05}})
    _insert_result(sid, "regime_split", {"verdict": "PASS", "n_regimes": 3, "profitable_regime_share": 0.33})
    _insert_result(sid, "monte_carlo", {"verdict": "PASS", "n_simulations": 1000, "n_trades": 40, "percentile_score": 0.4, "drawdown_distribution": {"p95": 15.0}, "max_dd_p95_ratio": 0.15})

    passed, reason = evaluate_promotion(sid, "gauntlet", "paper")
    # Must NOT be blocked for any strict-live reason at the paper stage.
    low = reason.lower()
    assert "degradation" not in low, reason
    assert "cost-stress" not in low, reason
    assert "regime" not in low, reason
    assert "win rate" not in low, reason
    assert "Live gate" not in reason, reason
    assert passed is True, f"lean paper gate should pass; got: {reason}"
