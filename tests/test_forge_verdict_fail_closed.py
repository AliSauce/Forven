from __future__ import annotations

import json
from datetime import datetime, timezone

import forven.policy as policy
from forven.db import get_db


def test_data_provenance_error_cannot_backfill_cached_passing_verdict(forven_db, monkeypatch):
    from forven import data_provenance

    strategy_id = "forge-provenance"
    now = datetime.now(timezone.utc).isoformat()
    cached_pass = {
        "verdict_tests": {
            "walk_forward": {
                "status": "pass",
                "passed": True,
                "verdict": "PASS",
                "folds": 5,
                "pass_rate": 1.0,
            }
        }
    }
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies (
                id, name, type, symbol, timeframe, params, metrics, status, stage,
                owner, stage_changed_at, created_at, updated_at
            ) VALUES (?, ?, 'rsi_momentum', 'BTC/USDT', '1h', '{}', ?, 'gauntlet',
                      'gauntlet', 'brain', ?, ?, ?)
            """,
            (strategy_id, strategy_id, json.dumps(cached_pass), now, now, now),
        )
        conn.execute(
            """
            INSERT INTO backtest_results (
                result_id, strategy_id, result_type, symbol, timeframe,
                metrics_json, config_json, created_at
            ) VALUES ('WF-PROVENANCE', ?, 'walk_forward', 'BTC/USDT', '1h', ?, ?, ?)
            """,
            (
                strategy_id,
                json.dumps({"verdict": "PASS", "splits": []}),
                json.dumps({"status": "succeeded"}),
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()

    monkeypatch.setattr(
        data_provenance,
        "is_stale_data_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("manifest unavailable")),
    )

    payloads, overall = policy._extract_gauntlet_verdict_payloads(
        strategy_id,
        row,
        cached_pass,
    )

    assert payloads == {}
    assert overall is None
