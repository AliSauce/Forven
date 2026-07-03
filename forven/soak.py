from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from forven import api_core
from forven.api_domains import analytics as analytics_domain
from forven.api_domains import paper as paper_domain
from forven.api_domains import trading as trading_domain
from forven.control_plane import status as control_plane_status
from forven.config import get_execution_mode
from forven.db import get_agents, get_db, kv_get
from forven.runtime_health import normalize_daemon_state
from forven.scheduler import _DEFAULT_JOB_IDS
from forven.util import normalize_stage

_REQUIRED_TABLES = {
    "agent_tasks",
    "agents",
    "approvals",
    "scheduler_jobs",
    "strategies",
    "strategy_events",
    "tasks",
    "trades",
}
_CORE_AGENT_IDS = {
    "brain",
    "execution-trader",
    "full-stack-engineer",
    "quant-researcher",
    "risk-manager",
    "simulation-agent",
    "strategy-developer",
}
_STATUS_ORDER = {"ok": 0, "warn": 1, "fail": 2}
_DAEMON_STALE_MINUTES = 10
_SCANNER_STALE_MINUTES = 20
_SCHEDULER_OVERDUE_GRACE_SECONDS = 180
_SCHEDULER_FAILURE_LOOKBACK_MINUTES = 180
_QUEUE_FAILURE_LOOKBACK_MINUTES = 120
_RUNTIME_FAILURE_LOOKBACK_MINUTES = 60
_OPERATOR_ACTION_LOOKBACK_HOURS = 24
_OPENAI_SOAK_WINDOW_HOURS = 4
_OPENAI_CLUSTER_WINDOW_MINUTES = 10
_OPENAI_CLUSTER_LIMIT = 3
_LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
_RUNTIME_PROBLEM_LABELS = {
    "daemon_not_running": "daemon not running",
    "daemon_stale": "daemon heartbeat stale",
    "scanner_stale": "signal scan stale",
    "scanner_execution_stale": "execution scan stale",
    "heartbeat_stale": "discord heartbeat stale",
    "reconciliation_issues": "reconciliation issues",
    "recent_runtime_failures": "recent runtime failures",
    "openai_rate_limit_cluster": "clustered OpenAI rate limits",
}
_LOG_TIMESTAMP_RE = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")
_OPENAI_RATE_LIMIT_KEY_RE = re.compile(r"(openai/[A-Za-z0-9._:-]+(?: [a-z-]+ path)?)")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_epoch_seconds(value: object) -> datetime | None:
    try:
        seconds = float(value)
    except Exception:
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except Exception:
        return None


def _age_seconds(timestamp: datetime | None, *, now: datetime | None = None) -> float | None:
    if timestamp is None:
        return None
    reference = now or _now_utc()
    delta = (reference - timestamp).total_seconds()
    return round(delta, 3) if delta >= 0 else 0.0


def _make_check(name: str, status: str, summary: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_status = status if status in _STATUS_ORDER else "fail"
    return {
        "name": name,
        "status": normalized_status,
        "summary": summary,
        "details": details or {},
    }


def _merge_status(current: str, candidate: str) -> str:
    return current if _STATUS_ORDER[current] >= _STATUS_ORDER[candidate] else candidate


def _describe_runtime_problem(problem: str) -> str:
    return _RUNTIME_PROBLEM_LABELS.get(problem, problem.replace("_", " ").strip())


def _bot_error_log_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".tmp" / "logs" / "forven_bot.err.log"


def _parse_log_timestamp(line: str) -> datetime | None:
    match = _LOG_TIMESTAMP_RE.match(str(line or ""))
    if not match:
        return None
    try:
        parsed = datetime.strptime(match.group("stamp"), "%Y-%m-%d %H:%M:%S,%f")
    except Exception:
        return None
    return parsed.replace(tzinfo=_LOCAL_TIMEZONE).astimezone(timezone.utc)


def _extract_openai_rate_limit_key(line: str) -> str | None:
    raw = str(line or "")
    match = _OPENAI_RATE_LIMIT_KEY_RE.search(raw)
    if match:
        return match.group(1).strip()
    if "api.openai.com" in raw:
        return "openai/http"
    return None


def _collect_openai_rate_limit_observation() -> dict[str, Any]:
    log_path = _bot_error_log_path()
    if not log_path.exists():
        return {
            "available": False,
            "log_path": str(log_path),
            "observed_events": 0,
            "clustered_bursts": 0,
            "observation_window_minutes": 0.0,
            "observation_complete": False,
            "cluster_examples": [],
        }

    try:
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception as exc:
        return {
            "available": False,
            "log_path": str(log_path),
            "error": str(exc),
            "observed_events": 0,
            "clustered_bursts": 0,
            "observation_window_minutes": 0.0,
            "observation_complete": False,
            "cluster_examples": [],
        }

    now = _now_utc()
    cutoff = now - timedelta(hours=_OPENAI_SOAK_WINDOW_HOURS)
    events: list[tuple[datetime, str, str]] = []
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    for line in lines[-4000:]:
        timestamp = _parse_log_timestamp(line)
        if timestamp is not None:
            if first_timestamp is None:
                first_timestamp = timestamp
            last_timestamp = timestamp
        lowered = str(line or "").lower()
        if "429" not in lowered or "openai" not in lowered:
            continue
        key = _extract_openai_rate_limit_key(line)
        if not key or timestamp is None or timestamp < cutoff:
            continue
        events.append((timestamp, key, str(line).strip()))

    window_minutes = 0.0
    if last_timestamp is not None:
        observation_start = first_timestamp if first_timestamp and first_timestamp > cutoff else cutoff
        observation_end = last_timestamp if last_timestamp <= now else now
        window_minutes = max((observation_end - observation_start).total_seconds() / 60.0, 0.0)

    cluster_examples: list[dict[str, Any]] = []
    events_by_key: dict[str, list[tuple[datetime, str]]] = {}
    for timestamp, key, line in events:
        events_by_key.setdefault(key, []).append((timestamp, line))

    for key, key_events in events_by_key.items():
        ordered = sorted(key_events, key=lambda item: item[0])
        start_idx = 0
        for idx, (timestamp, line) in enumerate(ordered):
            while start_idx < idx and (timestamp - ordered[start_idx][0]).total_seconds() > (_OPENAI_CLUSTER_WINDOW_MINUTES * 60):
                start_idx += 1
            count = idx - start_idx + 1
            if count > _OPENAI_CLUSTER_LIMIT:
                cluster_examples.append(
                    {
                        "key": key,
                        "count": count,
                        "window_minutes": _OPENAI_CLUSTER_WINDOW_MINUTES,
                        "first_at": ordered[start_idx][0].isoformat(),
                        "last_at": timestamp.isoformat(),
                        "sample": line,
                    }
                )
                break

    return {
        "available": True,
        "log_path": str(log_path),
        "observed_events": len(events),
        "clustered_bursts": len(cluster_examples),
        "observation_window_minutes": round(window_minutes, 3),
        "observation_complete": window_minutes >= (_OPENAI_SOAK_WINDOW_HOURS * 60),
        "latest_event_at": events[-1][0].isoformat() if events else None,
        "cluster_examples": cluster_examples[:3],
    }


def _table_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    with get_db() as conn:
        for table in ("strategies", "trades", "agent_tasks", "tasks", "approvals", "scheduler_jobs"):
            try:
                row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
            except Exception:
                counts[table] = -1
                continue
            counts[table] = int(row["count"] if row else 0)
    return counts


def _check_db_schema() -> dict[str, Any]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    tables = {str(row["name"]).strip() for row in rows}
    missing = sorted(_REQUIRED_TABLES.difference(tables))
    status = "ok" if not missing else "fail"
    summary = "Required backend tables present" if not missing else f"Missing tables: {', '.join(missing)}"
    return _make_check(
        "db_schema",
        status,
        summary,
        {
            "table_count": len(tables),
            "missing_tables": missing,
        },
    )


def _check_scheduler_health() -> dict[str, Any]:
    with get_db() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT id, enabled, last_status, last_run_at, next_run_at, running_since, last_error FROM scheduler_jobs"
        ).fetchall()]
    if not rows:
        return _make_check(
            "scheduler",
            "fail",
            "No scheduler jobs are registered",
            {"missing_core_jobs": sorted(_DEFAULT_JOB_IDS)},
        )

    existing_ids = {str(row.get("id") or "").strip() for row in rows}
    missing_core = sorted(_DEFAULT_JOB_IDS.difference(existing_ids))
    disabled_core = sorted(
        row["id"]
        for row in rows
        if str(row.get("id") or "").strip() in _DEFAULT_JOB_IDS and not bool(row.get("enabled", 0))
    )
    now = _now_utc()
    failure_cutoff = now - timedelta(minutes=_SCHEDULER_FAILURE_LOOKBACK_MINUTES)
    failed_jobs: list[str] = []
    historical_failed_jobs: list[str] = []
    for row in rows:
        job_id = str(row.get("id") or "").strip()
        if not job_id:
            continue
        last_status = str(row.get("last_status") or "").strip().lower()
        if last_status not in {"failed", "error"}:
            continue
        last_run_at = _parse_timestamp(row.get("last_run_at"))
        if last_run_at is None or last_run_at >= failure_cutoff:
            failed_jobs.append(job_id)
        else:
            historical_failed_jobs.append(job_id)
    failed_jobs = sorted(failed_jobs)
    historical_failed_jobs = sorted(historical_failed_jobs)
    overdue_jobs = sorted(
        str(row.get("id") or "").strip()
        for row in rows
        if bool(row.get("enabled", 0))
        and not str(row.get("running_since") or "").strip()
        and (
            (next_run_at := _parse_timestamp(row.get("next_run_at"))) is not None
            and (now - next_run_at).total_seconds() > _SCHEDULER_OVERDUE_GRACE_SECONDS
        )
    )

    status = "ok"
    summary = f"{len(rows)} scheduler jobs registered"
    if missing_core:
        status = "fail"
        summary = f"Missing scheduler jobs: {', '.join(missing_core)}"
    elif disabled_core or failed_jobs or overdue_jobs:
        status = "warn"
        summary = "Scheduler jobs need attention"

    return _make_check(
        "scheduler",
        status,
        summary,
        {
            "job_count": len(rows),
            "missing_core_jobs": missing_core,
            "disabled_core_jobs": disabled_core,
            "failed_jobs": failed_jobs,
            "historical_failed_jobs": historical_failed_jobs,
            "failure_lookback_minutes": _SCHEDULER_FAILURE_LOOKBACK_MINUTES,
            "overdue_jobs": overdue_jobs,
        },
    )


def _check_control_plane() -> dict[str, Any]:
    health = control_plane_status.health_check()
    stats = analytics_domain.get_stats()
    heartbeat = control_plane_status.get_system_heartbeat()

    expected_heartbeat_keys = {
        "dashboard",
        "open_trades",
        "agent_tasks",
        "strategies",
        "approvals",
        "scanner_state",
    }
    missing_keys = sorted(expected_heartbeat_keys.difference(heartbeat.keys()))
    status = "ok" if not missing_keys else "fail"
    summary = "Core backend control-plane endpoints responded" if not missing_keys else f"Heartbeat missing keys: {', '.join(missing_keys)}"
    return _make_check(
        "control_plane",
        status,
        summary,
        {
            "health_status": health.get("status"),
            "heartbeat_keys": sorted(heartbeat.keys()),
            "missing_heartbeat_keys": missing_keys,
            "table_counts": stats,
        },
    )


def _check_trade_views() -> dict[str, Any]:
    open_trades = trading_domain.read_open_trades(verify_exchange=None)
    recent_trades = trading_domain.read_recent_trades(limit=20)
    paper_sessions = paper_domain._collect_compat_paper_sessions(session_limit=25, trades_limit=200)
    return _make_check(
        "trade_views",
        "ok",
        "Trade and paper-session views loaded",
        {
            "open_trades": len(open_trades),
            "recent_trades": len(recent_trades),
            "paper_sessions": len(paper_sessions),
        },
    )


def _check_queue_health(stale_task_minutes: int) -> dict[str, Any]:
    now = _now_utc()
    stale_cutoff = now - timedelta(minutes=max(int(stale_task_minutes), 1))
    failure_cutoff = now - timedelta(minutes=_QUEUE_FAILURE_LOOKBACK_MINUTES)
    with get_db() as conn:
        agent_rows = [dict(row) for row in conn.execute(
            "SELECT status, started_at, completed_at, created_at FROM agent_tasks"
        ).fetchall()]
        task_rows = [dict(row) for row in conn.execute(
            "SELECT status, claimed_at, completed_at, created_at FROM tasks"
        ).fetchall()]
        approvals_row = conn.execute(
            "SELECT COUNT(*) AS count FROM approvals WHERE status = 'pending_approval'"
        ).fetchone()
        approval_preview_rows = [
            dict(row) for row in conn.execute(
                """
                SELECT id, approval_type, target_type, target_id, owner, created_at
                FROM approvals
                WHERE status = 'pending_approval'
                ORDER BY created_at ASC
                LIMIT 5
                """
            ).fetchall()
        ]
        stale_agent_preview_rows = [
            dict(row) for row in conn.execute(
                """
                SELECT id, display_id, agent_id, type, title, status, created_at, started_at
                FROM agent_tasks
                WHERE LOWER(status) = 'running'
                ORDER BY COALESCE(started_at, created_at) ASC
                LIMIT 25
                """
            ).fetchall()
        ]
        stale_brain_preview_rows = [
            dict(row) for row in conn.execute(
                """
                SELECT id, type, status, created_at, claimed_at, error
                FROM tasks
                WHERE LOWER(status) = 'running'
                ORDER BY COALESCE(claimed_at, created_at) ASC
                LIMIT 25
                """
            ).fetchall()
        ]

    agent_status_counts: dict[str, int] = {}
    stale_agent_tasks = 0
    recent_failed_agent_tasks = 0
    for row in agent_rows:
        status = str(row.get("status") or "unknown").strip().lower() or "unknown"
        agent_status_counts[status] = agent_status_counts.get(status, 0) + 1
        started_at = _parse_timestamp(row.get("started_at") or row.get("created_at"))
        if status == "running" and started_at and started_at < stale_cutoff:
            stale_agent_tasks += 1
        completed_at = _parse_timestamp(row.get("completed_at") or row.get("started_at") or row.get("created_at"))
        if status == "failed" and completed_at and completed_at >= failure_cutoff:
            recent_failed_agent_tasks += 1

    brain_status_counts: dict[str, int] = {}
    stale_brain_tasks = 0
    recent_failed_brain_tasks = 0
    for row in task_rows:
        status = str(row.get("status") or "unknown").strip().lower() or "unknown"
        brain_status_counts[status] = brain_status_counts.get(status, 0) + 1
        claimed_at = _parse_timestamp(row.get("claimed_at") or row.get("created_at"))
        if status == "running" and claimed_at and claimed_at < stale_cutoff:
            stale_brain_tasks += 1
        completed_at = _parse_timestamp(row.get("completed_at") or row.get("claimed_at") or row.get("created_at"))
        if status == "failed" and completed_at and completed_at >= failure_cutoff:
            recent_failed_brain_tasks += 1

    failed_agent_tasks = int(agent_status_counts.get("failed", 0) or 0)
    failed_brain_tasks = int(brain_status_counts.get("failed", 0) or 0)
    approval_preview = []
    for row in approval_preview_rows:
        created_at = _parse_timestamp(row.get("created_at"))
        approval_preview.append(
            {
                "id": int(row["id"]),
                "approval_type": str(row.get("approval_type") or "").strip(),
                "target_type": str(row.get("target_type") or "").strip(),
                "target_id": str(row.get("target_id") or "").strip() or None,
                "owner": str(row.get("owner") or "").strip() or None,
                "created_at": row.get("created_at"),
                "age_seconds": _age_seconds(created_at, now=now),
            }
        )

    stale_agent_preview = []
    for row in stale_agent_preview_rows:
        started_at = _parse_timestamp(row.get("started_at") or row.get("created_at"))
        if not started_at or started_at >= stale_cutoff:
            continue
        stale_agent_preview.append(
            {
                "id": int(row["id"]),
                "display_id": str(row.get("display_id") or "").strip() or None,
                "agent_id": str(row.get("agent_id") or "").strip() or None,
                "type": str(row.get("type") or "").strip() or None,
                "title": str(row.get("title") or "").strip() or None,
                "status": str(row.get("status") or "").strip().lower() or "running",
                "started_at": row.get("started_at") or row.get("created_at"),
                "age_seconds": _age_seconds(started_at, now=now),
            }
        )
        if len(stale_agent_preview) >= 5:
            break

    stale_brain_preview = []
    for row in stale_brain_preview_rows:
        claimed_at = _parse_timestamp(row.get("claimed_at") or row.get("created_at"))
        if not claimed_at or claimed_at >= stale_cutoff:
            continue
        stale_brain_preview.append(
            {
                "id": int(row["id"]),
                "type": str(row.get("type") or "").strip() or None,
                "status": str(row.get("status") or "").strip().lower() or "running",
                "claimed_at": row.get("claimed_at") or row.get("created_at"),
                "age_seconds": _age_seconds(claimed_at, now=now),
                "error": str(row.get("error") or "").strip() or None,
            }
        )
        if len(stale_brain_preview) >= 5:
            break

    pending_approvals = int(approvals_row["count"] if approvals_row else 0)
    status = "ok"
    summary = "Task queues are healthy"
    if stale_agent_tasks or stale_brain_tasks:
        status = "warn"
        summary = "Stale running tasks detected"
    elif recent_failed_agent_tasks or recent_failed_brain_tasks:
        status = "warn"
        summary = "Failed tasks detected"
    elif pending_approvals > 25:
        status = "warn"
        summary = "Approval backlog is elevated"

    return _make_check(
        "queues",
        status,
        summary,
        {
            "agent_task_counts": agent_status_counts,
            "brain_task_counts": brain_status_counts,
            "failed_agent_tasks": failed_agent_tasks,
            "failed_brain_tasks": failed_brain_tasks,
            "recent_failed_agent_tasks": recent_failed_agent_tasks,
            "recent_failed_brain_tasks": recent_failed_brain_tasks,
            "stale_agent_tasks": stale_agent_tasks,
            "stale_brain_tasks": stale_brain_tasks,
            "pending_approvals": pending_approvals,
            "pending_approval_preview": approval_preview,
            "stale_agent_task_preview": stale_agent_preview,
            "stale_brain_task_preview": stale_brain_preview,
            "stale_threshold_minutes": max(int(stale_task_minutes), 1),
            "failure_lookback_minutes": _QUEUE_FAILURE_LOOKBACK_MINUTES,
        },
    )


def _check_runtime_watchdog() -> dict[str, Any]:
    daemon_state = normalize_daemon_state(
        stale_after_seconds=_DAEMON_STALE_MINUTES * 60,
        write_back=True,
    )
    scanner_state = kv_get("scanner_state", {}) or {}
    if not isinstance(daemon_state, dict):
        daemon_state = {}
    if not isinstance(scanner_state, dict):
        scanner_state = {}

    daemon_running = bool(daemon_state.get("running"))
    daemon_last_scan = _parse_timestamp(daemon_state.get("last_scan"))
    daemon_last_tick = _parse_epoch_seconds(daemon_state.get("last_tick_ts"))
    daemon_last_heartbeat = _parse_epoch_seconds(daemon_state.get("last_heartbeat"))
    scanner_last_scan = _parse_timestamp(scanner_state.get("last_scan"))
    scanner_last_signal_scan = _parse_timestamp(scanner_state.get("last_signal_scan"))
    scanner_last_execution_scan = _parse_timestamp(scanner_state.get("last_execution_scan"))

    scheduler_signal_last_run = None
    scheduler_execution_last_run = None
    with get_db() as conn:
        scheduler_rows = conn.execute(
            """
            SELECT id, last_run_at
            FROM scheduler_jobs
            WHERE id IN ('forven-scanner-signal', 'forven-scanner-hourly')
            """
        ).fetchall()
    for row in scheduler_rows:
        job_id = str(row["id"] or "").strip()
        parsed_run_at = _parse_timestamp(row["last_run_at"])
        if job_id == "forven-scanner-signal":
            scheduler_signal_last_run = parsed_run_at
        elif job_id == "forven-scanner-hourly":
            scheduler_execution_last_run = parsed_run_at

    if scanner_last_signal_scan is None:
        scanner_last_signal_scan = scheduler_signal_last_run
    if scanner_last_execution_scan is None:
        scanner_last_execution_scan = scheduler_execution_last_run

    now = _now_utc()
    daemon_reference = daemon_last_tick or daemon_last_scan
    daemon_age_seconds = (now - daemon_reference).total_seconds() if daemon_reference else None
    heartbeat_age_seconds = (now - daemon_last_heartbeat).total_seconds() if daemon_last_heartbeat else None
    scanner_age_seconds = (now - scanner_last_scan).total_seconds() if scanner_last_scan else None
    scanner_signal_age_seconds = (now - scanner_last_signal_scan).total_seconds() if scanner_last_signal_scan else None
    scanner_execution_age_seconds = (now - scanner_last_execution_scan).total_seconds() if scanner_last_execution_scan else None

    lookback_expr = f"-{_RUNTIME_FAILURE_LOOKBACK_MINUTES} minutes"
    with get_db() as conn:
        failure_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT level, source, message, created_at
                FROM activity_log
                WHERE created_at >= datetime('now', ?)
                  AND source IN ('daemon', 'scanner', 'risk', 'trading_smoke')
                  AND level IN ('warning', 'error')
                ORDER BY id DESC
                LIMIT 10
                """,
                (lookback_expr,),
            ).fetchall()
        ]

    recent_runtime_failures = len(failure_rows)
    reconciliation_issues = int(daemon_state.get("reconciliation_issues", 0) or 0)
    openai_rate_limits = _collect_openai_rate_limit_observation()

    status = "ok"
    summary = "Runtime watchdog is healthy"
    problems: list[str] = []

    if not daemon_running:
        status = "fail"
        problems.append("daemon_not_running")
    elif daemon_age_seconds is None or daemon_age_seconds > (_DAEMON_STALE_MINUTES * 60):
        status = "fail"
        problems.append("daemon_stale")

    if scanner_age_seconds is None or scanner_age_seconds > (_SCANNER_STALE_MINUTES * 60):
        status = "fail"
        problems.append("scanner_stale")

    if scanner_execution_age_seconds is None or scanner_execution_age_seconds > (_SCANNER_STALE_MINUTES * 60):
        status = "fail"
        problems.append("scanner_execution_stale")

    if heartbeat_age_seconds is not None and heartbeat_age_seconds > (_DAEMON_STALE_MINUTES * 60):
        status = _merge_status(status, "warn")
        problems.append("heartbeat_stale")

    if reconciliation_issues > 0:
        status = _merge_status(status, "warn")
        problems.append("reconciliation_issues")

    if recent_runtime_failures > 0:
        status = _merge_status(status, "warn")
        problems.append("recent_runtime_failures")

    if int(openai_rate_limits.get("clustered_bursts", 0) or 0) > 0:
        status = _merge_status(status, "warn")
        problems.append("openai_rate_limit_cluster")

    if problems:
        summary = f"Runtime watchdog needs attention: {', '.join(_describe_runtime_problem(problem) for problem in problems)}"

    effective_signal_scan = (
        scanner_last_signal_scan.isoformat() if scanner_last_signal_scan is not None else scanner_state.get("last_signal_scan")
    )
    effective_execution_scan = (
        scanner_last_execution_scan.isoformat()
        if scanner_last_execution_scan is not None
        else scanner_state.get("last_execution_scan")
    )

    return _make_check(
        "runtime",
        status,
        summary,
        {
            "daemon_running": daemon_running,
            "daemon_pid": daemon_state.get("pid"),
            "daemon_process_alive": daemon_state.get("process_alive"),
            "daemon_last_scan": daemon_state.get("last_scan"),
            "daemon_last_tick_ts": daemon_state.get("last_tick_ts"),
            "daemon_last_heartbeat": daemon_state.get("last_heartbeat"),
            "daemon_age_seconds": None if daemon_age_seconds is None else round(daemon_age_seconds, 3),
            "heartbeat_age_seconds": None if heartbeat_age_seconds is None else round(heartbeat_age_seconds, 3),
            "scanner_last_scan": scanner_state.get("last_scan"),
            "scanner_age_seconds": None if scanner_age_seconds is None else round(scanner_age_seconds, 3),
            "scanner_last_signal_scan": effective_signal_scan,
            "scanner_signal_age_seconds": None if scanner_signal_age_seconds is None else round(scanner_signal_age_seconds, 3),
            "scanner_last_execution_scan": effective_execution_scan,
            "scanner_execution_age_seconds": None if scanner_execution_age_seconds is None else round(scanner_execution_age_seconds, 3),
            "scanner_last_scan_execution_enabled": scanner_state.get("execution_enabled"),
            "scanner_last_execution_actions_count": scanner_state.get("last_execution_actions_count"),
            "last_reconcile": daemon_state.get("last_reconcile"),
            "last_reconcile_status": daemon_state.get("last_reconcile_status"),
            "last_reconcile_error": daemon_state.get("last_reconcile_error"),
            "reconciliation_issues": reconciliation_issues,
            "recent_runtime_failures": recent_runtime_failures,
            "recent_failure_examples": failure_rows,
            "openai_rate_limits": openai_rate_limits,
            "daemon_stale_threshold_minutes": _DAEMON_STALE_MINUTES,
            "scanner_stale_threshold_minutes": _SCANNER_STALE_MINUTES,
        },
    )


def _check_agent_roster() -> dict[str, Any]:
    rows = get_agents(enabled_only=False)
    enabled_ids = {
        str(row.get("id") or "").strip()
        for row in rows
        if bool(row.get("enabled", 1))
    }
    present_ids = {str(row.get("id") or "").strip() for row in rows}
    missing = sorted(_CORE_AGENT_IDS.difference(present_ids))
    disabled = sorted(_CORE_AGENT_IDS.intersection(present_ids).difference(enabled_ids))
    status = "ok"
    summary = "Core agent roster present"
    if missing or disabled:
        status = "warn"
        summary = "Core agent roster is incomplete"
    return _make_check(
        "agents",
        status,
        summary,
        {
            "agent_count": len(rows),
            "missing_agents": missing,
            "disabled_agents": disabled,
        },
    )


def _check_pipeline_state() -> dict[str, Any]:
    strategies = api_core.get_strategies()
    funnel = analytics_domain.get_pipeline_funnel()
    stage_counts: dict[str, int] = {}
    unknown_stages: list[str] = []

    for row in strategies:
        normalized = normalize_stage(row.get("stage") or row.get("status"))
        stage_counts[normalized] = stage_counts.get(normalized, 0) + 1
        if normalized == "generated":
            raw = str(row.get("stage") or row.get("status") or "").strip().lower()
            if raw and raw not in {"generated", "quick_screen", "researching"}:
                unknown_stages.append(raw)

    status = "ok" if not unknown_stages else "warn"
    summary = "Pipeline state loaded" if not unknown_stages else "Unknown lifecycle stages found"
    return _make_check(
        "pipeline",
        status,
        summary,
        {
            "strategy_count": len(strategies),
            "stage_counts": stage_counts,
            "unknown_stages": sorted(set(unknown_stages)),
            "funnel_counts": funnel.get("counts", {}),
        },
    )


def _check_operator_actions() -> dict[str, Any]:
    state = kv_get("ops_manual_action_state", {}) or {}
    if not isinstance(state, dict):
        state = {}

    now = _now_utc()
    cutoff = now - timedelta(hours=_OPERATOR_ACTION_LOOKBACK_HOURS)
    actions: list[dict[str, Any]] = []
    failed_recent = 0

    for key, raw_value in state.items():
        if not isinstance(raw_value, dict):
            continue
        updated_at = _parse_timestamp(raw_value.get("updated_at"))
        action = {
            "key": str(key or "").strip(),
            "status": str(raw_value.get("status") or "unknown").strip().lower() or "unknown",
            "summary": str(raw_value.get("summary") or "").strip(),
            "updated_at": updated_at.isoformat() if updated_at else raw_value.get("updated_at"),
            "details": raw_value.get("details") if isinstance(raw_value.get("details"), dict) else {},
        }
        actions.append(action)
        if action["status"] == "fail" and updated_at and updated_at >= cutoff:
            failed_recent += 1

    actions.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    status = "warn" if failed_recent > 0 else "ok"
    summary = (
        f"{failed_recent} recent operator action failure(s)"
        if failed_recent > 0
        else ("Recent operator actions available" if actions else "No operator actions recorded yet")
    )
    return _make_check(
        "operator_actions",
        status,
        summary,
        {
            "action_count": len(actions),
            "failed_recent": failed_recent,
            "actions": actions[:10],
            "lookback_hours": _OPERATOR_ACTION_LOOKBACK_HOURS,
        },
    )


def _probe_hyperliquid_connection(testnet: bool) -> dict[str, Any]:
    from forven.exchange.hyperliquid import get_account_value, get_positions

    account = get_account_value(testnet=testnet, require_connection=True)
    positions = get_positions(testnet=testnet)
    return {
        "account": account,
        "positions": positions,
    }


def _check_hyperliquid(require_exchange_connection: bool) -> dict[str, Any]:
    settings = api_core.get_settings()
    exchange = str(settings.get("exchange") or "hyperliquid").strip().lower()
    mode = get_execution_mode()
    wallet = str(settings.get("hyperliquid_wallet") or "").strip()
    has_key = bool(settings.get("hyperliquid_has_key"))
    from forven.exchange.hyperliquid import resolve_configured_testnet

    testnet = resolve_configured_testnet(default_testnet=mode not in {"live", "mainnet"})

    if exchange != "hyperliquid":
        return _make_check(
            "hyperliquid",
            "fail",
            f"Unexpected execution exchange configured: {exchange}",
            {"exchange": exchange, "execution_mode": mode},
        )

    if not wallet or not has_key:
        return _make_check(
            "hyperliquid",
            "fail",
            "HyperLiquid credentials are incomplete",
            {
                "exchange": exchange,
                "execution_mode": mode,
                "wallet_configured": bool(wallet),
                "has_key": has_key,
                "testnet": testnet,
            },
        )

    details: dict[str, Any] = {
        "exchange": exchange,
        "execution_mode": mode,
        "wallet_configured": True,
        "has_key": True,
        "testnet": testnet,
        "connection_required": bool(require_exchange_connection),
    }

    if not require_exchange_connection:
        return _make_check(
            "hyperliquid",
            "ok",
            "HyperLiquid credentials are configured",
            details,
        )

    try:
        probe = _probe_hyperliquid_connection(testnet=testnet)
    except Exception as exc:
        details["error"] = str(exc)
        return _make_check(
            "hyperliquid",
            "fail",
            "HyperLiquid private connectivity check failed",
            details,
        )

    account = probe.get("account") if isinstance(probe, dict) else {}
    positions = probe.get("positions") if isinstance(probe, dict) else {}
    account_value = None
    total_margin_used = None
    if isinstance(account, dict):
        account_value = account.get("accountValue", account.get("account_value"))
        total_margin_used = account.get("totalMarginUsed", account.get("total_margin_used"))
    details.update(
        {
            "account_value": account_value,
            "total_margin_used": total_margin_used,
            "position_count": len(positions.get("positions", [])) if isinstance(positions, dict) else None,
        }
    )
    return _make_check(
        "hyperliquid",
        "ok",
        "HyperLiquid private connectivity check passed",
        details,
    )


def collect_backend_soak_report(
    *,
    require_exchange_connection: bool = False,
    stale_task_minutes: int = 30,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    overall_status = "ok"

    check_builders: list[tuple[str, Any]] = [
        ("db_schema", _check_db_schema),
        ("scheduler", _check_scheduler_health),
        ("control_plane", _check_control_plane),
        ("trade_views", _check_trade_views),
        ("queues", lambda: _check_queue_health(stale_task_minutes=stale_task_minutes)),
        ("runtime", _check_runtime_watchdog),
        ("agents", _check_agent_roster),
        ("pipeline", _check_pipeline_state),
        ("operator_actions", _check_operator_actions),
        ("hyperliquid", lambda: _check_hyperliquid(require_exchange_connection=require_exchange_connection)),
    ]

    for name, builder in check_builders:
        try:
            check = builder()
        except Exception as exc:
            check = _make_check(
                name,
                "fail",
                f"{name} raised an exception",
                {"error": str(exc)},
            )
        checks.append(check)
        overall_status = _merge_status(overall_status, str(check.get("status") or "fail"))

    queue_details = next((check["details"] for check in checks if check["name"] == "queues"), {})
    runtime_details = next((check["details"] for check in checks if check["name"] == "runtime"), {})
    pipeline_details = next((check["details"] for check in checks if check["name"] == "pipeline"), {})
    trade_details = next((check["details"] for check in checks if check["name"] == "trade_views"), {})
    scheduler_details = next((check["details"] for check in checks if check["name"] == "scheduler"), {})

    return {
        "generated_at": _now_utc().isoformat(),
        "status": overall_status,
        "summary": {
            "execution_mode": get_execution_mode(),
            "table_counts": _table_counts(),
            "strategy_count": int(pipeline_details.get("strategy_count", 0) or 0),
            "stage_counts": pipeline_details.get("stage_counts", {}),
            "open_trades": int(trade_details.get("open_trades", 0) or 0),
            "paper_sessions": int(trade_details.get("paper_sessions", 0) or 0),
            "pending_approvals": int(queue_details.get("pending_approvals", 0) or 0),
            "stale_agent_tasks": int(queue_details.get("stale_agent_tasks", 0) or 0),
            "stale_brain_tasks": int(queue_details.get("stale_brain_tasks", 0) or 0),
            "daemon_running": bool(runtime_details.get("daemon_running")),
            "daemon_age_seconds": runtime_details.get("daemon_age_seconds"),
            "scanner_age_seconds": runtime_details.get("scanner_age_seconds"),
            "recent_runtime_failures": int(runtime_details.get("recent_runtime_failures", 0) or 0),
            "reconciliation_issues": int(runtime_details.get("reconciliation_issues", 0) or 0),
            "scheduler_job_count": int(scheduler_details.get("job_count", 0) or 0),
        },
        "checks": checks,
    }
