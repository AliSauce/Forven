"""A genuine restore (terminal -> quick_screen) must re-align the strategy row's
TIMEFRAME to the author's declaration: prior runs can leave a hijacked column,
and the fresh quick-screen then runs on the wrong context (S06895 run eight:
restored with a residual 1h column, screened at 1h, Gate1 rejected on the fresh
1h numbers while the declared-4h evidence was positive). Metrics are already
NULLed by the existing reset_terminal_metrics; forward transitions untouched."""

import json
from datetime import datetime, timezone

from forven.brain import transition_stage
from forven.db import get_db


def _insert(sid: str, *, stage: str, tf: str, metrics: dict, params: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, metrics, "
            "status, owner, stage, stage_changed_at, created_at, updated_at) "
            "VALUES (?, ?, 'rsi_momentum', 'BTC', ?, ?, ?, ?, 'brain', ?, ?, ?, ?)",
            (sid, sid, tf, json.dumps(params), json.dumps(metrics), stage, stage, now, now, now),
        )
        conn.commit()


def _row(sid: str):
    with get_db() as conn:
        return conn.execute(
            "SELECT stage, timeframe, metrics FROM strategies WHERE id = ?", (sid,)
        ).fetchone()


def test_restore_realigns_timeframe_and_strips_stale_metrics(forven_db):
    sid = "S-HYG1"
    stale = {
        "sharpe": -1.22,
        "total_trades": 31,
        "total_return_pct": -9.0,
        "composite_robustness_score": 95.85,
        "robustness_tests_passed": 5,
    }
    _insert(sid, stage="archived", tf="1h", metrics=stale,
            params={"_timeframe": "4h", "kc_period": 10})

    transition_stage(sid, "quick_screen", reason="operator restore", actor="ui")

    row = _row(sid)
    assert row["stage"] == "quick_screen"
    assert str(row["timeframe"]).lower() == "4h", "timeframe must realign to params._timeframe"
    blob = json.loads(row["metrics"] or "{}")
    assert blob.get("sharpe") is None, (
        "stale performance metrics must be gone (reset_terminal_metrics NULLs the blob)"
    )


def test_non_restore_transitions_keep_metrics(forven_db):
    sid = "S-HYG2"
    good = {"sharpe": 1.2, "total_trades": 40, "fitness": 50.0, "total_return_pct": 8.0}
    _insert(sid, stage="quick_screen", tf="4h", metrics=good,
            params={"_timeframe": "4h"})
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, "
            "timeframe, metrics_json, config_json, created_at) "
            "VALUES ('bt-hyg2', ?, 'backtest', 'BTC', '4h', ?, '{}', ?)",
            (sid, json.dumps(good), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    transition_stage(sid, "gauntlet", reason="gate pass", actor="gauntlet_workflow")

    row = _row(sid)
    blob = json.loads(row["metrics"] or "{}")
    assert blob.get("sharpe") == 1.2, "forward transitions must not strip metrics"
