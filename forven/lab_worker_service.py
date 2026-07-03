"""Dedicated Regime Lab worker service backed by lab DB leases/heartbeats."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import logging
import os
import socket
import subprocess
import sys
import threading
import time

import psutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from forven.config import FORVEN_HOME
from forven.lab_dormancy import ensure_regime_lab_enabled
from forven.lab_db import (
    LabJobState,
    claim_next_lab_job,
    get_lab_job,
    heartbeat_lab_job,
    list_lab_jobs,
    recover_stale_lab_jobs,
    set_lab_job_state,
    set_lab_meta,
)
from forven.lab_matrix_engine import MATRIX_JOB_TYPE, run_matrix_job
from forven.lab_orchestrator import (
    ORCHESTRATOR_JOB_TYPE,
    handle_orchestrator_failure,
    handle_orchestrator_success,
    maybe_enqueue_due_continuous_cycle,
    run_orchestrator_cycle_job,
)
from forven.lab_regime_engine import (
    MODEL_REBUILD_JOB_TYPE,
    SEGMENT_BUILD_JOB_TYPE,
    run_model_rebuild_job,
    run_segment_build_job,
)

log = logging.getLogger("forven.lab_worker_service")

WORKER_STATUS_META_KEY = "lab_worker_status"
DEFAULT_POLL_INTERVAL_SECONDS = 3.0

# Circuit breaker — mirrors scheduler pattern (scheduler.py:1119-1121)
_WORKER_CIRCUIT_BREAKER_ALERT = 5
_WORKER_CIRCUIT_BREAKER_REINIT = 20
_WORKER_CIRCUIT_BREAKER_TERMINATE = 50
_WORKER_ERROR_BACKOFF_SECONDS = 10.0
DEFAULT_LEASE_SECONDS = 90
DEFAULT_STALE_TIMEOUT_SECONDS = 180
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0
LAB_WORKER_LOG = "lab_worker.log"
LAB_WORKER_STARTUP_LOG = "forven_lab_worker.log"
LAB_WORKER_STARTUP_ERR_LOG = "forven_lab_worker.err.log"
LAB_WORKER_PID_FILE = "lab_worker.pid"
SUPPORTED_LAB_JOB_TYPES = (
    ORCHESTRATOR_JOB_TYPE,
    MODEL_REBUILD_JOB_TYPE,
    SEGMENT_BUILD_JOB_TYPE,
    MATRIX_JOB_TYPE,
)

# CPU resource gate defaults
DEFAULT_CPU_GATE_SKIP_PCT = 85
DEFAULT_CPU_GATE_REDUCE_PCT = 70
DEFAULT_CPU_GATE_FULL_PCT = 50
CPU_GATE_MAX_SKIP_MINUTES = 15
CPU_GATE_ALERT_MINUTES = 30
CPU_GATE_FORCE_RESTART_MINUTES = 60

PIPELINE_PROGRESS_META_KEY = "pipeline_progress"
MAX_HEARTBEAT_FAILURES_BEFORE_ESCALATION = 5

_resource_gate_stats: dict[str, int] = {"skips": 0, "reductions": 0, "full": 0}
_cpu_gate_skip_since: float | None = None

# Configurable job-level timeout (seconds). A single hung job cannot block the
# worker loop for longer than this.
DEFAULT_JOB_TIMEOUT_SECONDS = 600
# Cooldown (seconds) before restarting the inner loop after circuit breaker.
_WORKER_RESTART_COOLDOWN_SECONDS = 30.0


class JobAbortedError(Exception):
    """Raised when a heartbeat abort signal is detected during job processing."""


def check_abort(abort_event: threading.Event | None) -> None:
    """Raise JobAbortedError if the heartbeat abort signal has been set."""
    if abort_event is not None and abort_event.is_set():
        raise JobAbortedError("Job aborted by heartbeat failure escalation")


def _record_pipeline_progress(event: str, job_id: str | None = None) -> None:
    """Write pipeline progress KV for watchdog staleness detection."""
    try:
        from forven.lab_db import get_lab_meta
        now_iso = datetime.now(timezone.utc).isoformat()
        progress = get_lab_meta(PIPELINE_PROGRESS_META_KEY, {}) or {}
        if not isinstance(progress, dict):
            progress = {}
        if event == "claimed":
            progress["last_job_claimed_at"] = now_iso
            progress["last_claimed_job_id"] = job_id
        elif event == "completed":
            progress["last_job_completed_at"] = now_iso
            progress["last_completed_job_id"] = job_id
            progress["jobs_completed_last_hour"] = int(progress.get("jobs_completed_last_hour", 0)) + 1
            progress["last_hour_reset_at"] = progress.get("last_hour_reset_at", now_iso)
        progress["updated_at"] = now_iso
        set_lab_meta(PIPELINE_PROGRESS_META_KEY, progress)
    except Exception as exc:
        log.warning("Pipeline progress write failed (event=%s, job_id=%s): %s", event, job_id, exc)


def get_canonical_lab_worker_log_path() -> Path:
    log_dir = FORVEN_HOME / "lab"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / LAB_WORKER_LOG


def build_worker_id(prefix: str = "lab-worker") -> str:
    host = socket.gethostname().split(".", 1)[0] or "localhost"
    return f"{prefix}:{host}:{os.getpid()}:{uuid4().hex[:8]}"


def _pid_file_path() -> Path:
    return FORVEN_HOME / "lab" / LAB_WORKER_PID_FILE


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False

    try:
        process = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False

    try:
        if not process.is_running():
            return False
        if process.status() == psutil.STATUS_ZOMBIE:
            return False
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        return True

    if os.name == "nt":
        return True

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_pid_lock() -> bool:
    """Write PID file if no other live worker holds it. Returns True if acquired."""
    pid_path = _pid_file_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    if pid_path.exists():
        try:
            existing_pid = int(pid_path.read_text().strip())
            if _is_pid_alive(existing_pid):
                log.warning(
                    "Another lab worker is already running (PID %d)",
                    existing_pid,
                    extra={"existing_pid": existing_pid},
                )
                return False
        except (ValueError, OSError):
            pass
    pid_path.write_text(str(os.getpid()))
    return True


def release_pid_lock() -> None:
    """Remove the PID file on clean shutdown."""
    pid_path = _pid_file_path()
    try:
        if pid_path.exists():
            stored_pid = int(pid_path.read_text().strip())
            if stored_pid == os.getpid():
                pid_path.unlink(missing_ok=True)
    except (ValueError, OSError):
        pid_path.unlink(missing_ok=True)


def _write_worker_status(
    *,
    worker_id: str,
    state: str,
    current_job_id: str | None = None,
    last_job_id: str | None = None,
    last_result: dict[str, Any] | None = None,
    last_error: str | None = None,
) -> None:
    payload = {
        "worker_id": worker_id,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "state": state,
        "current_job_id": current_job_id,
        "last_job_id": last_job_id,
        "last_result": last_result or {},
        "last_error": last_error,
        "heartbeat_at": time.time(),
    }
    set_lab_meta(WORKER_STATUS_META_KEY, payload)


def _reconcile_orchestrator_state() -> bool:
    """Check for stuck orchestrator state and auto-recover on worker startup.

    Returns True if recovery was performed.
    """
    from forven.lab_orchestrator import (
        get_orchestrator_config,
        get_orchestrator_status,
        ORCHESTRATOR_STATUS_META_KEY,
    )
    from forven.lab_db import set_lab_job_state, LabJobState, set_lab_meta
    from datetime import datetime, timezone, timedelta

    try:
        config = get_orchestrator_config()
        status = get_orchestrator_status()
    except Exception as exc:
        log.error("Orchestrator state reconciliation skipped: %s", exc)
        return False

    state = str(status.get("state") or "idle").strip().lower()

    # Auto-recover from "failed" state after cooldown (default 10 min)
    if state == "failed":
        failed_cooldown_minutes = int(config.get("failed_recovery_cooldown_minutes", 10))
        last_completed_raw = status.get("last_cycle_completed_at")
        should_recover = True
        if last_completed_raw:
            try:
                last_completed = datetime.fromisoformat(str(last_completed_raw).replace("Z", "+00:00"))
                if last_completed.tzinfo is None:
                    last_completed = last_completed.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - last_completed
                if age < timedelta(minutes=failed_cooldown_minutes):
                    should_recover = False
            except Exception:
                pass  # Can't parse — recover anyway
        if should_recover:
            log.warning(
                "Orchestrator auto-recovering from 'failed' state (cooldown %dm elapsed)",
                failed_cooldown_minutes,
            )
            status["state"] = "idle" if config.get("enabled") else "paused"
            status["pending_model_job_id"] = None
            status["pending_segments_job_id"] = None
            status["pending_matrix_job_id"] = None
            status["last_error"] = "auto-recovered from failed state"
            if config.get("enabled"):
                from forven.lab_orchestrator import _now_iso
                status["next_run_at"] = _now_iso()
            set_lab_meta(ORCHESTRATOR_STATUS_META_KEY, status)
            try:
                from forven.notifications import emit_notification
                emit_notification(
                    "orchestrator_failed_recovery",
                    severity="warning",
                    source="lab_worker",
                    title="Orchestrator recovered from failed state",
                    summary=f"Auto-recovered from 'failed' to '{status['state']}' after {failed_cooldown_minutes}m cooldown",
                    channel_name="alerts",
                    dedupe_key="orchestrator_failed_recovery",
                )
            except Exception:
                pass
            return True
        return False

    stuck_states = {"queued_model", "queued_segments", "queued_matrix", "running"}
    if state not in stuck_states:
        return False

    cadence_hours = max(1, int(config.get("cadence_hours", 12)))
    stale_threshold = timedelta(hours=cadence_hours * 2)

    last_started_raw = status.get("last_cycle_started_at")
    if last_started_raw:
        try:
            last_started = datetime.fromisoformat(str(last_started_raw).replace("Z", "+00:00"))
            if last_started.tzinfo is None:
                last_started = last_started.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - last_started
            if age < stale_threshold:
                return False  # Not stale yet — let it run
        except Exception:
            pass  # Can't parse — treat as stale

    # Check for "running" state where all referenced jobs are actually finished
    if state == "running":
        pending_keys = ["pending_model_job_id", "pending_segments_job_id", "pending_matrix_job_id"]
        all_done = True
        for key in pending_keys:
            job_id = status.get(key)
            if job_id:
                try:
                    referenced_job = get_lab_job(str(job_id))
                    if referenced_job is not None and referenced_job.state == LabJobState.RUNNING:
                        all_done = False
                        break
                except Exception:
                    pass  # Can't check — treat as done
        if all_done:
            log.warning(
                "Orchestrator in 'running' state but all referenced jobs are finished — recovering to idle",
            )
            status["state"] = "idle" if config.get("enabled") else "paused"
            status["pending_model_job_id"] = None
            status["pending_segments_job_id"] = None
            status["pending_matrix_job_id"] = None
            status["last_error"] = "auto-recovered: running with no active jobs"
            if config.get("enabled"):
                from forven.lab_orchestrator import _now_iso
                status["next_run_at"] = _now_iso()
            set_lab_meta(ORCHESTRATOR_STATUS_META_KEY, status)
            return True

    # State is stuck — recover
    log.warning(
        "Orchestrator stuck in state '%s' (last_cycle_started_at=%s) — auto-recovering",
        state,
        last_started_raw,
    )

    # Fail any pending jobs
    pending_keys = ["pending_model_job_id", "pending_segments_job_id", "pending_matrix_job_id"]
    for key in pending_keys:
        job_id = status.get(key)
        if job_id:
            try:
                set_lab_job_state(
                    str(job_id),
                    state=LabJobState.FAILED,
                    error_json={"error": "stale_recovery", "reason": "orchestrator stuck on worker startup"},
                    progress_json={"phase": "failed", "error": "stale_recovery"},
                )
                log.info("Marked stale pending job %s as FAILED", job_id)
            except Exception as exc:
                log.warning("Failed to mark stale job %s: %s", job_id, exc)

    # Reset orchestrator status
    status["state"] = "idle" if config.get("enabled") else "paused"
    status["pending_model_job_id"] = None
    status["pending_segments_job_id"] = None
    status["pending_matrix_job_id"] = None
    status["last_error"] = "auto-recovered from stuck state on worker startup"
    if config.get("enabled"):
        from forven.lab_orchestrator import _now_iso
        status["next_run_at"] = _now_iso()
    set_lab_meta(ORCHESTRATOR_STATUS_META_KEY, status)

    log.warning("Orchestrator auto-recovered to '%s' state", status["state"])
    return True


def _load_cpu_gate_config() -> dict[str, int]:
    """Load CPU gate thresholds from orchestrator config, falling back to defaults."""
    from forven.lab_orchestrator import get_orchestrator_config
    config = get_orchestrator_config()
    def _clamp(key: str, default: int) -> int:
        try:
            return max(1, min(100, int(config.get(key, default))))
        except (TypeError, ValueError):
            return default
    return {
        "skip_pct": _clamp("cpu_gate_skip_pct", DEFAULT_CPU_GATE_SKIP_PCT),
        "reduce_pct": _clamp("cpu_gate_reduce_pct", DEFAULT_CPU_GATE_REDUCE_PCT),
        "full_pct": _clamp("cpu_gate_full_pct", DEFAULT_CPU_GATE_FULL_PCT),
    }


def _check_resource_gate(*, configured_matrix_workers: int = 4) -> dict:
    """Check CPU usage and return gating decision.

    Returns dict with keys: action ("skip"|"reduce"|"full"), cpu_pct, effective_matrix_workers.
    """
    gate_config = _load_cpu_gate_config()
    try:
        cpu_pct = psutil.cpu_percent(interval=1.0)
    except Exception as exc:
        log.warning("psutil.cpu_percent failed, defaulting to reduced capacity: %s", exc)
        # Fail safe: assume moderate load, not zero
        return {"action": "reduce", "cpu_pct": -1.0, "effective_matrix_workers": 1}

    if cpu_pct >= gate_config["skip_pct"]:
        _resource_gate_stats["skips"] += 1
        return {"action": "skip", "cpu_pct": cpu_pct, "effective_matrix_workers": 0}
    elif cpu_pct >= gate_config["reduce_pct"]:
        _resource_gate_stats["reductions"] += 1
        return {"action": "reduce", "cpu_pct": cpu_pct, "effective_matrix_workers": 1}
    else:
        _resource_gate_stats["full"] += 1
        return {"action": "full", "cpu_pct": cpu_pct, "effective_matrix_workers": max(1, configured_matrix_workers)}


def get_lab_worker_status() -> dict[str, Any]:
    running_jobs = [
        job.model_dump()
        for job in list_lab_jobs(states=[LabJobState.RUNNING], limit=20)
    ]
    from forven.lab_db import get_lab_meta

    meta = get_lab_meta(WORKER_STATUS_META_KEY, {})
    if not isinstance(meta, dict):
        meta = {}
    heartbeat_at = meta.get("heartbeat_at")
    heartbeat_age_seconds: float | None = None
    if heartbeat_at is not None:
        try:
            heartbeat_age_seconds = max(0.0, time.time() - float(heartbeat_at))
        except Exception:
            heartbeat_age_seconds = None
    active = bool(
        meta.get("state") in {"starting", "idle", "running"}
        and heartbeat_age_seconds is not None
        and heartbeat_age_seconds <= max(DEFAULT_STALE_TIMEOUT_SECONDS / 2.0, 30.0)
    )
    if heartbeat_age_seconds is not None:
        meta["heartbeat_age_seconds"] = heartbeat_age_seconds
    meta["is_stale"] = not active
    return {
        "active": active,
        "worker": meta,
        "running_jobs": running_jobs,
    }


def _start_non_matrix_job_heartbeat(
    *,
    worker_id: str,
    job_id: str,
    job_type: str,
    lease_seconds: int,
    interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    abort_event = threading.Event()

    def _loop() -> None:
        consecutive_failures = 0
        while not stop_event.wait(max(0.1, float(interval_seconds))):
            try:
                heartbeat_lab_job(
                    job_id,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                    progress_json={"phase": "working", "job_type": job_type},
                )
                _write_worker_status(worker_id=worker_id, state="running", current_job_id=job_id)
                consecutive_failures = 0
            except Exception as exc:
                consecutive_failures += 1
                if consecutive_failures >= MAX_HEARTBEAT_FAILURES_BEFORE_ESCALATION:
                    log.error(
                        "Heartbeat failed %d times — signalling job abort for %s",
                        consecutive_failures, job_id,
                        extra={"worker_id": worker_id, "job_id": job_id},
                    )
                    abort_event.set()
                elif consecutive_failures >= 3:
                    log.error(
                        "Heartbeat failed %d consecutive times: %s",
                        consecutive_failures, exc,
                        extra={"worker_id": worker_id, "job_id": job_id},
                    )
                else:
                    log.warning(
                        "Lab worker heartbeat failed (%d): %s",
                        consecutive_failures, exc,
                        extra={"worker_id": worker_id, "job_id": job_id},
                    )

    thread = threading.Thread(
        target=_loop,
        name=f"lab-job-heartbeat-{job_id}",
        daemon=True,
    )
    # Preserve the abort signal for internal callers without changing the
    # long-standing `(stop_event, thread)` return contract used by tests.
    setattr(thread, "abort_event", abort_event)
    thread.start()
    return stop_event, thread


def get_lab_worker_log_candidates() -> list[Path]:
    repo_root = Path(__file__).resolve().parents[1]
    return [
        repo_root / ".forven_home" / "lab" / LAB_WORKER_LOG,
        get_canonical_lab_worker_log_path(),
        repo_root / ".tmp" / "logs" / LAB_WORKER_STARTUP_LOG,
        repo_root / ".tmp" / "logs" / LAB_WORKER_STARTUP_ERR_LOG,
    ]


def resolve_lab_worker_log_path() -> Path:
    candidates: list[Path] = []
    seen: set[str] = set()
    for candidate in get_lab_worker_log_candidates():
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    existing = [path for path in candidates if path.exists()]
    if existing:
        existing.sort(
            key=lambda path: (
                1 if path.stat().st_size > 0 else 0,
                path.stat().st_mtime,
            ),
            reverse=True,
        )
        return existing[0]
    return candidates[0]


def read_lab_worker_feed(*, limit_lines: int = 200) -> dict[str, Any]:
    resolved_limit = max(10, int(limit_lines))
    log_path = resolve_lab_worker_log_path()
    exists = log_path.exists()
    updated_at = None
    lines: list[str] = []
    total_lines = 0
    truncated = False

    if exists:
        updated_at = log_path.stat().st_mtime
        tail: deque[str] = deque(maxlen=resolved_limit)
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                total_lines += 1
                tail.append(raw_line.rstrip("\r\n"))
        lines = list(tail)
        truncated = total_lines > len(lines)

    return {
        "path": str(log_path),
        "exists": exists,
        "lines": lines,
        "line_count": total_lines,
        "truncated": truncated,
        "updated_at": updated_at,
    }


def start_lab_worker_process() -> dict[str, Any]:
    ensure_regime_lab_enabled(action="start the Regime Lab worker")
    status = get_lab_worker_status()
    if bool(status.get("active")):
        worker = dict(status.get("worker") or {})
        return {
            "status": "already_running",
            "worker": worker,
            "pid": worker.get("pid"),
        }

    repo_root = Path(__file__).resolve().parents[1]
    log_path = get_canonical_lab_worker_log_path()

    creationflags = 0
    popen_kwargs: dict[str, Any] = {
        "cwd": str(repo_root),
        "stdin": subprocess.DEVNULL,
        "stdout": log_path.open("a", encoding="utf-8"),
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name == "nt":
        # CREATE_NO_WINDOW, not DETACHED_PROCESS: a detached child has no
        # console, so the venv launcher's re-spawned base interpreter allocates
        # a fresh VISIBLE console window. A hidden console is inherited instead.
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        popen_kwargs["creationflags"] = creationflags
    else:
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(
        [sys.executable, "-m", "forven", "lab", "worker"],
        **popen_kwargs,
    )
    return {
        "status": "started",
        "pid": int(process.pid),
        "log_path": str(log_path),
    }


def process_claimed_lab_job(
    *,
    worker_id: str,
    job_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    effective_matrix_workers: int | None = None,
    abort_event: threading.Event | None = None,
) -> dict[str, Any]:
    job = get_lab_job(job_id)
    if job is None:
        raise ValueError(f"Unknown lab job: {job_id}")
    if job.state != LabJobState.RUNNING:
        raise RuntimeError(f"Lab job {job_id} is not in RUNNING state")

    heartbeat_lab_job(
        job_id,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        progress_json={"phase": "starting", "job_type": job.job_type},
    )
    _write_worker_status(worker_id=worker_id, state="running", current_job_id=job_id)
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    if job.job_type != MATRIX_JOB_TYPE:
        heartbeat_stop, heartbeat_thread = _start_non_matrix_job_heartbeat(
            worker_id=worker_id,
            job_id=job_id,
            job_type=job.job_type,
            lease_seconds=lease_seconds,
        )

    # Combine two abort sources into one event the job logic checks:
    #  - the wall-clock timeout from the run loop (abort_event arg), and
    #  - the heartbeat-failure escalation thread's own event.
    # A small relay thread mirrors the heartbeat event into the timeout event so
    # check_abort() / run_matrix_job() only need to watch one signal.
    heartbeat_abort: threading.Event | None = (
        getattr(heartbeat_thread, "abort_event", None) if heartbeat_thread else None
    )
    combined_abort: threading.Event | None
    relay_stop: threading.Event | None = None
    relay_thread: threading.Thread | None = None
    if abort_event is not None and heartbeat_abort is not None:
        combined_abort = abort_event
        relay_stop = threading.Event()

        def _relay() -> None:
            while not relay_stop.wait(0.5):
                if heartbeat_abort.is_set():
                    abort_event.set()
                    return

        relay_thread = threading.Thread(target=_relay, name=f"lab-abort-relay-{job_id}", daemon=True)
        relay_thread.start()
    else:
        combined_abort = abort_event if abort_event is not None else heartbeat_abort

    try:
        payload = dict(job.payload_json or {})
        if job.job_type == MATRIX_JOB_TYPE and effective_matrix_workers is not None:
            payload["matrix_workers"] = effective_matrix_workers

        check_abort(combined_abort)

        if job.job_type == ORCHESTRATOR_JOB_TYPE:
            summary = run_orchestrator_cycle_job(payload, job_id=job.id)
        elif job.job_type == MODEL_REBUILD_JOB_TYPE:
            summary = run_model_rebuild_job(payload)
        elif job.job_type == SEGMENT_BUILD_JOB_TYPE:
            summary = run_segment_build_job(payload)
        elif job.job_type == MATRIX_JOB_TYPE:
            summary = run_matrix_job(
                job_id, worker_id=worker_id, lease_seconds=lease_seconds, abort_event=combined_abort
            )
        else:
            raise ValueError(f"Unsupported lab job type for worker service: {job.job_type}")

        check_abort(combined_abort)

        handle_orchestrator_success(job_type=job.job_type, payload=payload, summary=summary)
        set_lab_job_state(
            job_id,
            state=LabJobState.SUCCEEDED,
            progress_json={"phase": "completed", **summary},
        )
        _write_worker_status(
            worker_id=worker_id,
            state="idle",
            current_job_id=None,
            last_job_id=job_id,
            last_result=summary,
        )
        return summary
    except JobAbortedError as abort_exc:
        log.error("Job %s aborted by heartbeat escalation: %s", job_id, abort_exc)
        set_lab_job_state(
            job_id,
            state=LabJobState.FAILED,
            error_json={"error": str(abort_exc), "worker_id": worker_id, "reason": "heartbeat_abort"},
            progress_json={"phase": "failed", "error": "heartbeat_abort"},
        )
        handle_orchestrator_failure(job_type=job.job_type, payload=dict(job.payload_json or {}), error=str(abort_exc))
        _write_worker_status(
            worker_id=worker_id,
            state="idle",
            current_job_id=None,
            last_job_id=job_id,
            last_error=str(abort_exc),
        )
        return {"error": str(abort_exc), "job_id": job_id, "state": str(LabJobState.FAILED)}
    except Exception as exc:
        refreshed = get_lab_job(job_id)
        attempts = int(refreshed.attempts if refreshed else job.attempts)
        max_attempts = int(refreshed.max_attempts if refreshed else job.max_attempts)
        terminal_state = LabJobState.DEADLETTER if attempts >= max_attempts else LabJobState.FAILED
        set_lab_job_state(
            job_id,
            state=terminal_state,
            error_json={"error": str(exc), "worker_id": worker_id},
            deadletter_reason=("max_attempts_exceeded" if terminal_state == LabJobState.DEADLETTER else None),
            progress_json={"phase": "failed", "error": str(exc)},
        )
        handle_orchestrator_failure(job_type=job.job_type, payload=dict(job.payload_json or {}), error=str(exc))
        _write_worker_status(
            worker_id=worker_id,
            state="idle",
            current_job_id=None,
            last_job_id=job_id,
            last_error=str(exc),
        )
        return {"error": str(exc), "job_id": job_id, "state": str(terminal_state)}
    finally:
        if relay_stop is not None:
            relay_stop.set()
        if relay_thread is not None:
            relay_thread.join(timeout=2.0)
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2.0)


def _get_job_timeout_seconds() -> float:
    """Load configurable job timeout from orchestrator config."""
    try:
        from forven.lab_orchestrator import get_orchestrator_config
        return float(get_orchestrator_config().get("job_timeout_seconds", DEFAULT_JOB_TIMEOUT_SECONDS))
    except Exception:
        return float(DEFAULT_JOB_TIMEOUT_SECONDS)


def run_lab_worker_loop(
    *,
    worker_id: str | None = None,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    stale_timeout_seconds: int = DEFAULT_STALE_TIMEOUT_SECONDS,
    once: bool = False,
) -> dict[str, Any]:
    ensure_regime_lab_enabled(action="start the Regime Lab worker")
    resolved_worker_id = worker_id or build_worker_id()
    processed = 0
    last_idle_log_at = 0.0

    if not acquire_pid_lock():
        log.error("Failed to acquire PID lock — another worker may be running")
        return {"status": "locked", "worker_id": resolved_worker_id, "processed_jobs": 0}

    _write_worker_status(worker_id=resolved_worker_id, state="starting")
    log.info("Lab worker starting", extra={"worker_id": resolved_worker_id, "once": once})

    try:
        _reconcile_orchestrator_state()
    except Exception as exc:
        log.error("Orchestrator reconciliation failed on startup: %s", exc, exc_info=True)

    # Outer loop: survives circuit breaker resets. Only fatal errors exit.
    try:
      while True:
        consecutive_errors = 0
        try:
          while True:
            try:
              _write_worker_status(worker_id=resolved_worker_id, state="idle")
              recovered = recover_stale_lab_jobs(worker_timeout_seconds=stale_timeout_seconds)
              if recovered:
                  log.warning("Recovered stale lab jobs", extra={"worker_id": resolved_worker_id, "count": recovered})
              scheduled = maybe_enqueue_due_continuous_cycle()
              if scheduled:
                  log.info(
                      "Queued continuous Regime Lab cycle",
                      extra={"worker_id": resolved_worker_id, "cycle_id": scheduled.get("cycle_id"), "job_id": scheduled.get("job_id")},
                  )
              # CPU resource gate — skip or throttle based on system load
              global _cpu_gate_skip_since
              try:
                  from forven.lab_orchestrator import get_orchestrator_config as _get_orch_config
                  _configured_workers = int(_get_orch_config().get("matrix_workers", 4))
              except Exception:
                  _configured_workers = 4
              gate = _check_resource_gate(configured_matrix_workers=_configured_workers)
              if gate["action"] == "skip":
                  now = time.time()
                  if _cpu_gate_skip_since is None:
                      _cpu_gate_skip_since = now
                  skip_duration_min = (now - _cpu_gate_skip_since) / 60.0

                  # Force a claim attempt after CPU_GATE_MAX_SKIP_MINUTES to prevent indefinite stall
                  if skip_duration_min >= CPU_GATE_MAX_SKIP_MINUTES:
                      log.warning(
                          "CPU gate: forced claim after %.0f min skip (CPU %.1f%%)",
                          skip_duration_min, gate["cpu_pct"],
                      )
                      gate = {"action": "reduce", "cpu_pct": gate["cpu_pct"], "effective_matrix_workers": 1}
                      # Fall through to claim attempt
                  else:
                      if now - last_idle_log_at >= 15.0:
                          log.warning("CPU gate: skipping claim (CPU %.1f%%, skipping %.0fmin)", gate["cpu_pct"], skip_duration_min)
                          last_idle_log_at = now
                      # Alert if skipping too long
                      if skip_duration_min >= CPU_GATE_ALERT_MINUTES and int(skip_duration_min) % 10 == 0:
                          try:
                              from forven.notifications import emit_notification
                              emit_notification(
                                  "cpu_gate_stall",
                                  severity="critical",
                                  source="lab_worker",
                                  title="CPU gate blocking pipeline",
                                  summary=f"Lab worker skipping all jobs for {skip_duration_min:.0f} min (CPU {gate['cpu_pct']:.0f}%)",
                                  channel_name="alerts",
                                  dedupe_key="cpu_gate_stall",
                              )
                          except Exception as _alert_exc:
                              log.warning("Failed to emit CPU gate stall alert: %s", _alert_exc)
                      if once:
                          break
                      time.sleep(max(0.1, float(poll_interval_seconds)))
                      continue
              else:
                  _cpu_gate_skip_since = None  # Reset skip timer on non-skip
              effective_matrix_workers = gate["effective_matrix_workers"]
              if gate["action"] == "reduce":
                  log.info("CPU gate: reducing matrix_workers to %d (CPU %.1f%%)", effective_matrix_workers, gate["cpu_pct"])

              # Persist gate stats for overnight summary
              try:
                  set_lab_meta("resource_gate_stats", dict(_resource_gate_stats))
              except Exception as _stats_exc:
                  log.warning("Failed to persist resource gate stats: %s", _stats_exc)

              job = None
              for supported_job_type in SUPPORTED_LAB_JOB_TYPES:
                  job = claim_next_lab_job(
                      worker_id=resolved_worker_id,
                      job_type=supported_job_type,
                      lease_seconds=lease_seconds,
                  )
                  if job is not None:
                      break
              if job is None:
                  now = time.time()
                  if now - last_idle_log_at >= 15.0:
                      log.info("Lab worker idle", extra={"worker_id": resolved_worker_id, "processed_jobs": processed})
                      last_idle_log_at = now
                  if once:
                      break
                  time.sleep(max(0.1, float(poll_interval_seconds)))
                  continue

              log.info(
                  "Claimed lab job",
                  extra={"worker_id": resolved_worker_id, "job_id": job.id, "job_type": job.job_type},
              )
              _record_pipeline_progress("claimed", job_id=job.id)

              # Job-level timeout: prevent a single hung job from blocking the loop.
              # The timer now SETS a real abort_event (was a no-op `lambda: None`
              # sentinel) which is passed into process_claimed_lab_job. Matrix jobs
              # check it between strategy backtests (run_matrix_job(abort_event=...))
              # and non-matrix jobs check it via check_abort(), so a wall-clock
              # overrun actually interrupts the work instead of only being logged.
              job_timeout = _get_job_timeout_seconds()
              timeout_abort = threading.Event()
              job_timer = threading.Timer(job_timeout, timeout_abort.set)
              job_start = time.monotonic()
              try:
                  job_timer.start()
                  process_claimed_lab_job(
                      worker_id=resolved_worker_id,
                      job_id=job.id,
                      lease_seconds=lease_seconds,
                      effective_matrix_workers=effective_matrix_workers,
                      abort_event=timeout_abort,
                  )
              finally:
                  job_timer.cancel()
                  elapsed = time.monotonic() - job_start
                  if elapsed >= job_timeout:
                      log.error(
                          "Job %s exceeded timeout (%.0fs >= %.0fs) — abort signalled",
                          job.id, elapsed, job_timeout,
                      )

              processed += 1
              _record_pipeline_progress("completed", job_id=job.id)
              log.info(
                  "Completed lab job",
                  extra={"worker_id": resolved_worker_id, "job_id": job.id, "processed_jobs": processed},
              )
              if once:
                  break

              consecutive_errors = 0

            except Exception as exc:
              consecutive_errors += 1
              log.exception(
                  "Lab worker iteration failed (#%d): %s",
                  consecutive_errors, exc,
                  extra={"worker_id": resolved_worker_id},
              )

              if consecutive_errors >= _WORKER_CIRCUIT_BREAKER_TERMINATE:
                  log.critical(
                      "Lab worker circuit breaker: %d consecutive failures — restarting inner loop after cooldown",
                      consecutive_errors,
                  )
                  try:
                      from forven.notifications import emit_notification
                      emit_notification(
                          "lab_worker_circuit_breaker",
                          severity="critical",
                          source="lab_worker",
                          title="Lab worker circuit breaker — restarting",
                          summary=f"{consecutive_errors} consecutive iteration failures. Last error: {exc}. Restarting after {_WORKER_RESTART_COOLDOWN_SECONDS}s cooldown.",
                          channel_name="alerts",
                          dedupe_key="lab_worker_circuit_breaker",
                      )
                  except Exception as _notify_exc:
                      log.error("Failed to emit circuit breaker alert: %s", _notify_exc)
                  break  # Break inner loop; outer loop will restart after cooldown

              elif consecutive_errors >= _WORKER_CIRCUIT_BREAKER_REINIT:
                  log.error("Lab worker circuit breaker: %d failures — reinitializing lab DB", consecutive_errors)
                  try:
                      from forven.lab_db import init_lab_db
                      init_lab_db()
                  except Exception as reinit_err:
                      log.error("Lab DB reinit failed: %s", reinit_err)

              elif consecutive_errors >= _WORKER_CIRCUIT_BREAKER_ALERT:
                  try:
                      from forven.notifications import emit_notification
                      emit_notification(
                          "lab_worker_degraded",
                          severity="critical",
                          source="lab_worker",
                          title="Lab worker degraded",
                          summary=f"{consecutive_errors} consecutive iteration failures. Error: {exc}",
                          channel_name="alerts",
                          dedupe_key="lab_worker_degraded",
                      )
                  except Exception as _notify_exc:
                      log.error("Failed to emit degraded alert: %s", _notify_exc)

              if once:
                  break
              # Exponential backoff: 10s -> 20s -> 40s -> ... -> max 120s
              backoff = min(_WORKER_ERROR_BACKOFF_SECONDS * (2 ** (consecutive_errors - 1)), 120.0)
              time.sleep(backoff)

          # Inner loop exited — check if this was a clean exit (once mode) or circuit breaker
          if once:
              break

          # Circuit breaker restart: cooldown then restart inner loop
          log.warning(
              "Lab worker restarting inner loop after %.0fs cooldown",
              _WORKER_RESTART_COOLDOWN_SECONDS,
          )
          time.sleep(_WORKER_RESTART_COOLDOWN_SECONDS)
          # Re-reconcile orchestrator state before restarting
          try:
              _reconcile_orchestrator_state()
          except Exception as exc:
              log.error("Orchestrator reconciliation failed on restart: %s", exc)

        except (KeyboardInterrupt, SystemExit):
            log.info("Lab worker received shutdown signal")
            break

    finally:
        _write_worker_status(worker_id=resolved_worker_id, state="stopped")
        release_pid_lock()
        log.info("Lab worker stopping", extra={"worker_id": resolved_worker_id, "processed_jobs": processed})

    return {"status": "ok", "worker_id": resolved_worker_id, "processed_jobs": processed}
