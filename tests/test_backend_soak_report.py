from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from forven.api_domains import paper as paper_domain
from forven.control_plane import ops as control_plane_ops
from forven import soak
from forven.db import create_approval, get_db, kv_set
from forven.routers.ops import router as ops_router
from forven.routers.status import router as status_router
from forven.scheduler import _DEFAULT_JOB_IDS


def _seed_scheduler_jobs() -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        for job_id in _DEFAULT_JOB_IDS:
            conn.execute(
                """
                INSERT OR IGNORE INTO scheduler_jobs
                    (id, name, enabled, schedule_type, schedule_expr, command, last_status, next_run_at)
                VALUES (?, ?, 1, 'interval', '60000', ?, 'ok', ?)
                """,
                (job_id, job_id, job_id, now),
            )


def _seed_agents() -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        for agent_id in sorted(soak._CORE_AGENT_IDS):
            conn.execute(
                """
                INSERT OR IGNORE INTO agents
                    (id, name, role, enabled, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (agent_id, agent_id, agent_id, now, now),
            )


def _seed_runtime_state(*, stale: bool = False, runtime_failures: int = 0) -> None:
    now = datetime.now(timezone.utc)
    daemon_scan = now - timedelta(minutes=25 if stale else 1)
    scanner_scan = now - timedelta(minutes=45 if stale else 2)
    heartbeat = now - timedelta(minutes=30 if stale else 1)

    kv_set(
        "daemon_state",
        {
            "running": True,
            "last_scan": daemon_scan.isoformat(),
            "last_tick_ts": daemon_scan.timestamp(),
            "last_heartbeat": heartbeat.timestamp(),
            "last_reconcile": now.isoformat(),
            "last_reconcile_status": "ok",
            "reconciliation_issues": 0,
        },
    )
    kv_set(
        "scanner_state",
        {
            "last_scan": scanner_scan.isoformat(),
            "execution_enabled": True,
            "last_signal_scan": scanner_scan.isoformat(),
            "last_execution_scan": scanner_scan.isoformat(),
            "last_execution_actions_count": 1,
        },
    )

    if runtime_failures:
        created_at = now.strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            for index in range(runtime_failures):
                conn.execute(
                    """
                    INSERT INTO activity_log (level, source, message, created_at)
                    VALUES ('warning', 'scanner', ?, ?)
                    """,
                    (
                        f"runtime failure {index}",
                        created_at,
                    ),
                )


def _patch_core_views(monkeypatch) -> None:
    monkeypatch.setattr(soak.control_plane_status, "health_check", lambda: {"status": "ok"})
    monkeypatch.setattr(soak.analytics_domain, "get_stats", lambda: {"strategies": 0, "trades": 0})
    monkeypatch.setattr(
        soak.control_plane_status,
        "get_system_heartbeat",
        lambda: {
            "dashboard": {},
            "open_trades": [],
            "agent_tasks": [],
            "strategies": [],
            "approvals": [],
            "scanner_state": {},
        },
    )
    monkeypatch.setattr(soak.trading_domain, "read_open_trades", lambda verify_exchange=False: [])
    monkeypatch.setattr(soak.trading_domain, "read_recent_trades", lambda limit=20: [])
    monkeypatch.setattr(paper_domain, "_collect_compat_paper_sessions", lambda session_limit=25, trades_limit=200: [])
    monkeypatch.setattr(
        soak.api_core,
        "get_settings",
        lambda: {
            "exchange": "hyperliquid",
            "hyperliquid_wallet": "0xabc123",
            "hyperliquid_has_key": True,
            "hyperliquid_testnet": True,
        },
    )
    monkeypatch.setattr(soak, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(
        soak,
        "_bot_error_log_path",
        lambda: Path.cwd() / ".tmp" / "tests" / "missing-openai-rate-limit.log",
    )


def test_collect_backend_soak_report_ok(forven_db, monkeypatch):
    _seed_scheduler_jobs()
    _seed_agents()
    _seed_runtime_state()
    _patch_core_views(monkeypatch)

    report = soak.collect_backend_soak_report()

    assert report["status"] == "ok"
    assert report["summary"]["scheduler_job_count"] == len(_DEFAULT_JOB_IDS)
    check_map = {check["name"]: check for check in report["checks"]}
    assert check_map["db_schema"]["status"] == "ok"
    assert check_map["scheduler"]["status"] == "ok"
    assert check_map["runtime"]["status"] == "ok"
    assert check_map["runtime"]["details"]["scanner_last_execution_scan"] is not None
    assert check_map["hyperliquid"]["status"] == "ok"


def test_collect_backend_soak_report_warns_on_stale_agent_tasks(forven_db, monkeypatch):
    _seed_scheduler_jobs()
    _seed_agents()
    _seed_runtime_state()
    _patch_core_views(monkeypatch)

    stale_started_at = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, status, created_at, started_at)
            VALUES (?, 'analysis', 'Stale task', 'desc', 'running', ?, ?)
            """,
            ("brain", stale_started_at, stale_started_at),
        )

    report = soak.collect_backend_soak_report(stale_task_minutes=30)

    assert report["status"] == "warn"
    queues = next(check for check in report["checks"] if check["name"] == "queues")
    assert queues["status"] == "warn"
    assert queues["details"]["stale_agent_tasks"] == 1


def test_collect_backend_soak_report_warns_on_failed_tasks(forven_db, monkeypatch):
    _seed_scheduler_jobs()
    _seed_agents()
    _seed_runtime_state()
    _patch_core_views(monkeypatch)

    created_at = datetime.now(timezone.utc).isoformat()
    for index in range(26):
        create_approval(
            "code_change",
            target_type="strategy",
            target_id=f"S{index:05d}",
            owner="operator",
        )
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, status, created_at, completed_at, error)
            VALUES (?, 'analysis', 'Failed task', 'desc', 'failed', ?, ?, ?)
            """,
            ("brain", created_at, created_at, "agent failure"),
        )
        conn.execute(
            """
            INSERT INTO tasks
                (type, payload, status, created_at, completed_at, error)
            VALUES ('brain_invoke', '{}', 'failed', ?, ?, ?)
            """,
            (created_at, created_at, "brain failure"),
        )

    report = soak.collect_backend_soak_report()

    assert report["status"] == "warn"
    queues = next(check for check in report["checks"] if check["name"] == "queues")
    assert queues["status"] == "warn"
    assert queues["summary"] == "Failed tasks detected"
    assert queues["details"]["failed_agent_tasks"] == 1
    assert queues["details"]["failed_brain_tasks"] == 1
    assert queues["details"]["recent_failed_agent_tasks"] == 1
    assert queues["details"]["recent_failed_brain_tasks"] == 1
    assert queues["details"]["agent_task_counts"]["failed"] == 1
    assert queues["details"]["brain_task_counts"]["failed"] == 1
    assert queues["details"]["pending_approvals"] == 26


def test_collect_backend_soak_report_ignores_historical_failed_tasks(forven_db, monkeypatch):
    _seed_scheduler_jobs()
    _seed_agents()
    _seed_runtime_state()
    _patch_core_views(monkeypatch)

    stale_completed_at = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, status, created_at, completed_at, error)
            VALUES (?, 'analysis', 'Old failed task', 'desc', 'failed', ?, ?, ?)
            """,
            ("brain", stale_completed_at, stale_completed_at, "agent failure"),
        )
        conn.execute(
            """
            INSERT INTO tasks
                (type, payload, status, created_at, completed_at, error)
            VALUES ('brain_invoke', '{}', 'failed', ?, ?, ?)
            """,
            (stale_completed_at, stale_completed_at, "brain failure"),
        )

    report = soak.collect_backend_soak_report()

    queues = next(check for check in report["checks"] if check["name"] == "queues")
    assert queues["status"] == "ok"
    assert queues["details"]["failed_agent_tasks"] == 1
    assert queues["details"]["failed_brain_tasks"] == 1
    assert queues["details"]["recent_failed_agent_tasks"] == 0
    assert queues["details"]["recent_failed_brain_tasks"] == 0


def test_collect_backend_soak_report_prioritizes_stale_queue_summary(forven_db, monkeypatch):
    _seed_scheduler_jobs()
    _seed_agents()
    _seed_runtime_state()
    _patch_core_views(monkeypatch)

    stale_started_at = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    created_at = datetime.now(timezone.utc).isoformat()
    for index in range(26):
        create_approval(
            "code_change",
            target_type="strategy",
            target_id=f"P{index:05d}",
            owner="operator",
        )
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, status, created_at, started_at)
            VALUES (?, 'analysis', 'Stale task', 'desc', 'running', ?, ?)
            """,
            ("brain", stale_started_at, stale_started_at),
        )
        conn.execute(
            """
            INSERT INTO tasks
                (type, payload, status, created_at, completed_at, error)
            VALUES ('brain_invoke', '{}', 'failed', ?, ?, ?)
            """,
            (created_at, created_at, "brain failure"),
        )

    report = soak.collect_backend_soak_report(stale_task_minutes=30)

    queues = next(check for check in report["checks"] if check["name"] == "queues")
    assert queues["status"] == "warn"
    assert queues["summary"] == "Stale running tasks detected"
    assert queues["details"]["stale_agent_tasks"] == 1
    assert queues["details"]["failed_brain_tasks"] == 1
    assert queues["details"]["pending_approvals"] == 26


def test_collect_backend_soak_report_humanizes_runtime_summary(forven_db, monkeypatch):
    _seed_scheduler_jobs()
    _seed_agents()
    _seed_runtime_state()
    _patch_core_views(monkeypatch)

    stale_scanner_scan = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    kv_set(
        "scanner_state",
        {
            "last_scan": stale_scanner_scan,
            "execution_enabled": True,
            "last_signal_scan": stale_scanner_scan,
            "last_execution_scan": stale_scanner_scan,
            "last_execution_actions_count": 0,
        },
    )

    report = soak.collect_backend_soak_report()

    runtime = next(check for check in report["checks"] if check["name"] == "runtime")
    assert runtime["status"] == "fail"
    assert "signal scan stale" in runtime["summary"]
    assert "execution scan stale" in runtime["summary"]
    assert "scanner_stale" not in runtime["summary"]


def test_collect_backend_soak_report_warns_on_scheduler_errors(forven_db, monkeypatch):
    _seed_scheduler_jobs()
    _seed_agents()
    _seed_runtime_state()
    _patch_core_views(monkeypatch)

    stale_due = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE scheduler_jobs
            SET last_status = 'error',
                last_error = 'monitor insert mismatch',
                next_run_at = ?
            WHERE id = 'forven-slippage-monitor'
            """,
            (stale_due,),
        )

    report = soak.collect_backend_soak_report()

    scheduler = next(check for check in report["checks"] if check["name"] == "scheduler")
    assert scheduler["status"] == "warn"
    assert "forven-slippage-monitor" in scheduler["details"]["failed_jobs"]
    assert "forven-slippage-monitor" in scheduler["details"]["overdue_jobs"]


def test_collect_backend_soak_report_ignores_historical_scheduler_failures(forven_db, monkeypatch):
    _seed_scheduler_jobs()
    _seed_agents()
    _seed_runtime_state()
    _patch_core_views(monkeypatch)

    stale_run = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
    future_due = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE scheduler_jobs
            SET last_status = 'error',
                last_error = 'old transient failure',
                last_run_at = ?,
                next_run_at = ?
            WHERE id = 'forven-slippage-monitor'
            """,
            (stale_run, future_due),
        )

    report = soak.collect_backend_soak_report()

    scheduler = next(check for check in report["checks"] if check["name"] == "scheduler")
    assert scheduler["status"] == "ok"
    assert "forven-slippage-monitor" not in scheduler["details"]["failed_jobs"]
    assert "forven-slippage-monitor" in scheduler["details"]["historical_failed_jobs"]


def test_collect_backend_soak_report_warns_on_clustered_openai_rate_limits(forven_db, monkeypatch, tmp_path):
    _seed_scheduler_jobs()
    _seed_agents()
    _seed_runtime_state()
    _patch_core_views(monkeypatch)
    monkeypatch.setattr(soak, "_now_utc", lambda: datetime(2026, 3, 10, 12, 8, tzinfo=timezone.utc))

    bot_log = tmp_path / "forven_bot.err.log"
    bot_log.write_text(
        "\n".join(
            [
                "2026-03-10 12:00:00,000 [forven.agents.runner] WARNING openai/gpt-5.2 tool-call path: 429 rate limited (round 1), cooldown 35.0s",
                "2026-03-10 12:02:00,000 [forven.agents.runner] WARNING openai/gpt-5.2 tool-call path: 429 rate limited (round 1), cooldown 35.0s",
                "2026-03-10 12:04:00,000 [forven.agents.runner] WARNING openai/gpt-5.2 tool-call path: 429 rate limited (round 1), cooldown 35.0s",
                "2026-03-10 12:06:00,000 [forven.agents.runner] WARNING openai/gpt-5.2 tool-call path: 429 rate limited (round 1), cooldown 35.0s",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(soak, "_bot_error_log_path", lambda: bot_log)

    report = soak.collect_backend_soak_report()

    runtime = next(check for check in report["checks"] if check["name"] == "runtime")
    assert runtime["status"] == "warn"
    assert "clustered OpenAI rate limits" in runtime["summary"]
    assert runtime["details"]["openai_rate_limits"]["clustered_bursts"] == 1
    assert runtime["details"]["openai_rate_limits"]["observed_events"] == 4


def test_collect_backend_soak_report_includes_queue_previews(forven_db, monkeypatch):
    _seed_scheduler_jobs()
    _seed_agents()
    _seed_runtime_state()
    _patch_core_views(monkeypatch)

    stale_started_at = (datetime.now(timezone.utc) - timedelta(minutes=95)).isoformat()
    stale_claimed_at = (datetime.now(timezone.utc) - timedelta(minutes=80)).isoformat()
    approval_id = create_approval(
        "code_change",
        target_type="strategy",
        target_id="S00042",
        owner="operator",
    )
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, display_id, description, status, created_at, started_at)
            VALUES (?, 'analysis', 'Review stale execution task', 'AT0007', 'desc', 'running', ?, ?)
            """,
            ("full-stack-engineer", stale_started_at, stale_started_at),
        )
        conn.execute(
            """
            INSERT INTO tasks
                (type, payload, status, created_at, claimed_at, error)
            VALUES ('brain_invoke', '{}', 'running', ?, ?, ?)
            """,
            (stale_claimed_at, stale_claimed_at, "waiting on remote model"),
        )

    report = soak.collect_backend_soak_report(stale_task_minutes=30)

    queues = next(check for check in report["checks"] if check["name"] == "queues")
    approval_preview = queues["details"]["pending_approval_preview"]
    stale_agent_preview = queues["details"]["stale_agent_task_preview"]
    stale_brain_preview = queues["details"]["stale_brain_task_preview"]

    assert approval_preview[0]["id"] == approval_id
    assert approval_preview[0]["target_id"] == "S00042"
    assert stale_agent_preview[0]["display_id"] == "AT0007"
    assert stale_agent_preview[0]["agent_id"] == "full-stack-engineer"
    assert stale_brain_preview[0]["type"] == "brain_invoke"
    assert stale_brain_preview[0]["error"] == "waiting on remote model"


def test_collect_backend_soak_report_includes_operator_actions(forven_db, monkeypatch):
    _seed_scheduler_jobs()
    _seed_agents()
    _seed_runtime_state()
    _patch_core_views(monkeypatch)

    kv_set(
        "ops_manual_action_state",
        {
            "signal_scan": {
                "status": "ok",
                "summary": "Signal scan completed",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "details": {"signals_count": 2},
            },
            "execution_scan": {
                "status": "fail",
                "summary": "Execution scan blocked",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "details": {"reason": "System paused"},
            },
        },
    )

    report = soak.collect_backend_soak_report()

    operator_actions = next(check for check in report["checks"] if check["name"] == "operator_actions")
    assert operator_actions["status"] == "warn"
    assert operator_actions["details"]["action_count"] == 2
    assert operator_actions["details"]["failed_recent"] == 1


def test_collect_backend_soak_report_fails_when_execution_scanner_is_stale(forven_db, monkeypatch):
    _seed_scheduler_jobs()
    _seed_agents()
    _patch_core_views(monkeypatch)

    now = datetime.now(timezone.utc)
    daemon_scan = now - timedelta(minutes=1)
    scanner_signal = now - timedelta(minutes=1)
    scanner_execution = now - timedelta(minutes=45)

    kv_set(
        "daemon_state",
        {
            "running": True,
            "last_scan": daemon_scan.isoformat(),
            "last_tick_ts": daemon_scan.timestamp(),
            "last_heartbeat": daemon_scan.timestamp(),
            "last_reconcile": now.isoformat(),
            "last_reconcile_status": "ok",
            "reconciliation_issues": 0,
        },
    )
    kv_set(
        "scanner_state",
        {
            "last_scan": scanner_signal.isoformat(),
            "last_signal_scan": scanner_signal.isoformat(),
            "last_execution_scan": scanner_execution.isoformat(),
            "execution_enabled": False,
        },
    )

    report = soak.collect_backend_soak_report()

    runtime = next(check for check in report["checks"] if check["name"] == "runtime")
    assert report["status"] == "fail"
    assert runtime["status"] == "fail"
    assert runtime["details"]["scanner_execution_age_seconds"] is not None


def test_collect_backend_soak_report_fails_strict_hyperliquid_probe(forven_db, monkeypatch):
    _seed_scheduler_jobs()
    _seed_agents()
    _seed_runtime_state()
    _patch_core_views(monkeypatch)
    monkeypatch.setattr(soak, "_probe_hyperliquid_connection", lambda testnet: (_ for _ in ()).throw(RuntimeError("connection refused")))

    report = soak.collect_backend_soak_report(require_exchange_connection=True)

    assert report["status"] == "fail"
    hyperliquid = next(check for check in report["checks"] if check["name"] == "hyperliquid")
    assert hyperliquid["status"] == "fail"
    assert "connection refused" in hyperliquid["details"]["error"]


def test_collect_backend_soak_report_flags_stale_runtime(forven_db, monkeypatch):
    _seed_scheduler_jobs()
    _seed_agents()
    _seed_runtime_state(stale=True, runtime_failures=1)
    _patch_core_views(monkeypatch)

    report = soak.collect_backend_soak_report()

    assert report["status"] == "fail"
    runtime = next(check for check in report["checks"] if check["name"] == "runtime")
    assert runtime["status"] == "fail"
    assert runtime["details"]["recent_runtime_failures"] == 1
    assert runtime["details"]["daemon_age_seconds"] > (soak._DAEMON_STALE_MINUTES * 60)
    assert runtime["details"]["scanner_age_seconds"] > (soak._SCANNER_STALE_MINUTES * 60)


def test_system_soak_report_route_delegates(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_collect_backend_soak_report(*, require_exchange_connection: bool, stale_task_minutes: int):
        captured["require_exchange_connection"] = require_exchange_connection
        captured["stale_task_minutes"] = stale_task_minutes
        return {"status": "ok", "checks": []}

    monkeypatch.setattr("forven.routers.status.collect_backend_soak_report", _fake_collect_backend_soak_report)

    app = FastAPI()
    app.include_router(status_router)
    client = TestClient(app)

    response = client.get(
        "/api/system/soak-report",
        params={"require_exchange_connection": "true", "stale_task_minutes": 45},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert captured == {"require_exchange_connection": True, "stale_task_minutes": 45}


def test_post_signal_scan_now_runs_signal_only(monkeypatch):
    monkeypatch.setattr("forven.db.init_db", lambda: None)
    monkeypatch.setattr(control_plane_ops, "log_activity", lambda *args, **kwargs: None)
    operator_state: dict[str, object] = {}
    monkeypatch.setattr(
        control_plane_ops,
        "kv_get",
        lambda key, default=None: {
            "scanner_state": {
                "strategies": ["S00001", "S00002"],
                "actions_count": 0,
                "last_scan": "2026-03-06T00:00:00+00:00",
                "last_signal_scan": "2026-03-06T00:00:00+00:00",
                "last_execution_scan": "2026-03-05T23:55:00+00:00",
                "last_execution_actions_count": 2,
            },
            "ops_manual_action_state": operator_state,
        }.get(key, default),
    )
    monkeypatch.setattr(control_plane_ops, "kv_set", lambda key, value: operator_state.update(value) if key == "ops_manual_action_state" and isinstance(value, dict) else None)

    captured: dict[str, object] = {}

    def _fake_run_scan(*, execute_positions: bool):
        captured["execute_positions"] = execute_positions
        return {"S00001": {"signal": "buy"}}

    monkeypatch.setattr("forven.scanner.run_scan", _fake_run_scan)

    result = asyncio.run(control_plane_ops.post_signal_scan_now())

    assert captured == {"execute_positions": False}
    assert result["ok"] is True
    assert result["mode"] == "signal_only"
    assert result["requested_execution"] is False
    assert result["execution_allowed"] is False
    assert result["execution_enabled"] is False
    assert result["signals_count"] == 1
    assert result["strategy_count"] == 2


def test_post_execution_scan_now_respects_trading_gate(forven_db, monkeypatch):
    from forven.system_pause import set_system_paused

    monkeypatch.setattr("forven.db.init_db", lambda: None)
    monkeypatch.setattr(control_plane_ops, "log_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "forven.scanner.run_scan",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("run_scan should not execute while paused")),
    )
    set_system_paused(True, paused_at="2026-03-06T00:00:00+00:00")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(control_plane_ops.post_execution_scan_now())

    assert exc_info.value.status_code == 409
    assert "System paused by operator" in str(exc_info.value.detail)


def test_post_execution_scan_now_degrades_to_signal_only_when_policy_disabled(monkeypatch):
    monkeypatch.setattr("forven.db.init_db", lambda: None)
    monkeypatch.setattr(control_plane_ops, "log_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(control_plane_ops, "_ops_bool_setting", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        control_plane_ops,
        "is_trading_allowed",
        lambda: (_ for _ in ()).throw(AssertionError("trading gate should be skipped when scanner execution is disabled")),
    )
    operator_state: dict[str, object] = {}
    monkeypatch.setattr(
        control_plane_ops,
        "kv_get",
        lambda key, default=None: {
            "scanner_state": {
                "strategies": ["S00001"],
                "actions_count": 0,
                "last_scan": "2026-03-06T00:00:00+00:00",
                "last_signal_scan": "2026-03-05T23:55:00+00:00",
                "last_execution_scan": "2026-03-06T00:00:00+00:00",
                "last_execution_actions_count": 0,
                "requested_execution": True,
                "execution_allowed": False,
                "mode": "signal_only_by_policy",
            },
            "ops_manual_action_state": operator_state,
        }.get(key, default),
    )
    monkeypatch.setattr(
        control_plane_ops,
        "kv_set",
        lambda key, value: operator_state.update(value) if key == "ops_manual_action_state" and isinstance(value, dict) else None,
    )

    captured: dict[str, object] = {}

    def _fake_run_scan(*, execute_positions: bool):
        captured["execute_positions"] = execute_positions
        return {"S00001": {"signal": "hold"}}

    monkeypatch.setattr("forven.scanner.run_scan", _fake_run_scan)

    result = asyncio.run(control_plane_ops.post_execution_scan_now())

    assert captured == {"execute_positions": True}
    assert result["mode"] == "signal_only_by_policy"
    assert result["requested_execution"] is True
    assert result["execution_allowed"] is False
    assert result["execution_enabled"] is False
    assert result["signals_count"] == 1


def test_post_exchange_reconcile_now_wraps_result(monkeypatch):
    monkeypatch.setattr("forven.db.init_db", lambda: None)
    monkeypatch.setattr(control_plane_ops, "log_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        control_plane_ops,
        "_update_daemon_recovery_from_reconcile",
        lambda *args, **kwargs: {
            "recovery_active": False,
            "recovery_status": "idle",
            "recovery_requires_operator": False,
            "recovery_summary": "",
            "recovery_batch_id": None,
        },
    )
    operator_state: dict[str, object] = {}
    monkeypatch.setattr(control_plane_ops, "kv_get", lambda key, default=None: operator_state if key == "ops_manual_action_state" else default)
    monkeypatch.setattr(control_plane_ops, "kv_set", lambda key, value: operator_state.update(value) if key == "ops_manual_action_state" and isinstance(value, dict) else None)
    monkeypatch.setattr("forven.exchange.risk.sync_from_trades", lambda: 0)
    monkeypatch.setattr(
        "forven.exchange.risk.reconcile_all_books",
        lambda *args, **kwargs: {
            "sqlite_open": 2,
            "exchange_open": 1,
            "synced": False,
            "discrepancies": [{"type": "missing_on_exchange", "details": "ghost"}],
        },
    )

    result = asyncio.run(control_plane_ops.post_exchange_reconcile_now())

    assert result["ok"] is True
    assert result["sqlite_open"] == 2
    assert result["exchange_open"] == 1
    assert result["discrepancy_count"] == 1


def test_system_manual_action_routes_delegate(monkeypatch):
    captured: list[str] = []

    async def _fake_signal():
        captured.append("signal")
        return {"ok": True, "mode": "signal_only"}

    async def _fake_execution():
        captured.append("execution")
        return {"ok": True, "mode": "signal_execution"}

    async def _fake_reconcile():
        captured.append("reconcile")
        return {"ok": True, "synced": True}

    monkeypatch.setattr("forven.routers.ops.control_plane_ops.post_signal_scan_now", _fake_signal)
    monkeypatch.setattr("forven.routers.ops.control_plane_ops.post_execution_scan_now", _fake_execution)
    monkeypatch.setattr("forven.routers.ops.control_plane_ops.post_exchange_reconcile_now", _fake_reconcile)

    app = FastAPI()
    app.include_router(ops_router)
    client = TestClient(app)

    signal_response = client.post("/api/system/scanner/signal-run")
    legacy_signal_response = client.post("/api/signals/check-now")
    execution_response = client.post("/api/system/scanner/execution-run")
    reconcile_response = client.post("/api/system/exchange/reconcile")

    assert signal_response.status_code == 200
    assert legacy_signal_response.status_code == 200
    assert execution_response.status_code == 200
    assert reconcile_response.status_code == 200
    assert captured == ["signal", "signal", "execution", "reconcile"]
