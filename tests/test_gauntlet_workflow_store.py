from __future__ import annotations

import json

from forven.db import create_strategy_container, get_db, init_db
from forven.gauntlet.definition import WORKFLOW_DEFINITION_VERSION, ordered_step_keys
from forven.gauntlet.store import create_or_get_workflow, get_workflow_detail


def _create_strategy() -> str:
    with get_db() as conn:
        strategy_id, _display_id, _base_id = create_strategy_container(
            conn=conn,
            name="Gauntlet Store Test",
            type_="rsi_momentum",
            symbol="BTC/USDT",
            timeframe="1h",
            params={"rsi_period": 14},
            stage="quick_screen",
            hypothesis_id=None,
        )
    return strategy_id


def test_init_db_creates_gauntlet_tables(forven_db):
    init_db()
    with get_db() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'gauntlet_%'"
            ).fetchall()
        }

    assert {
        "gauntlet_workflows",
        "gauntlet_steps",
        "gauntlet_artifacts",
        "gauntlet_events",
    }.issubset(tables)


def test_create_or_get_workflow_seeds_definition_steps(forven_db):
    strategy_id = _create_strategy()

    workflow = create_or_get_workflow(
        strategy_id=strategy_id,
        created_by="pytest",
        settings_snapshot={"quick_screen": {"enabled": True}},
    )
    same = create_or_get_workflow(
        strategy_id=strategy_id,
        created_by="pytest",
        settings_snapshot={"quick_screen": {"enabled": False}},
    )
    detail = get_workflow_detail(workflow["id"])

    assert workflow["id"] == same["id"]
    assert workflow["strategy_id"] == strategy_id
    assert workflow["definition_version"] == WORKFLOW_DEFINITION_VERSION
    assert [step["step_key"] for step in detail["steps"]] == ordered_step_keys()
    assert detail["steps"][0]["status"] == "queued"
    assert all(step["status"] == "pending" for step in detail["steps"][1:])
    # Engine provenance stamps every workflow snapshot at creation (engine_provenance.py).
    from forven.engine_provenance import BACKTEST_ENGINE_VERSION

    assert json.loads(detail["workflow"]["settings_snapshot_json"]) == {
        "quick_screen": {"enabled": True},
        "engine_version": BACKTEST_ENGINE_VERSION,
    }
