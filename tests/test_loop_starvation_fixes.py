"""Single-process starvation fixes: the event-loop lag watchdog makes GIL/loop
stalls visible (log + /api/health), and the live WS tolerates transient send
timeouts instead of dropping on the first one."""

from __future__ import annotations

import asyncio

import pytest

import forven.loop_watchdog as lw


# ── Loop-lag watchdog ────────────────────────────────────────────────────────


def test_monitor_records_and_snapshots():
    monitor = lw.LoopLagMonitor()
    for lag in (0.0, 0.01, 0.02):
        monitor.record(lag)
    snap = monitor.snapshot()
    assert snap["last_lag_ms"] == 20.0
    assert snap["max_lag_ms"] == 20.0
    assert snap["stalls_over_warn"] == 0
    assert snap["stalls_over_ws_risk"] == 0
    assert snap["window_ticks"] == 3


def test_monitor_counts_stalls_and_ws_risk():
    monitor = lw.LoopLagMonitor()
    monitor.record(1.2)   # over warn, under ws-risk
    monitor.record(3.0)   # over ws-risk (would drop a WS send)
    snap = monitor.snapshot()
    assert snap["stalls_over_warn"] == 2
    assert snap["stalls_over_ws_risk"] == 1
    assert snap["max_lag_ms"] == 3000.0
    assert snap["max_lag_at"] is not None


def test_loop_lag_issues_flag_ws_risk_only(monkeypatch):
    monitor = lw.LoopLagMonitor()
    monkeypatch.setattr(lw, "_MONITOR", monitor)
    assert lw.loop_lag_issues() == []
    monitor.record(1.5)  # warn-level stall: visible in snapshot, not an issue
    assert lw.loop_lag_issues() == []
    monitor.record(2.8)  # WS-drop-risk stall: a named health issue
    issues = lw.loop_lag_issues()
    assert len(issues) == 1
    assert "WS drop risk" in issues[0]


def test_health_summary_includes_event_loop(forven_db, monkeypatch):
    monitor = lw.LoopLagMonitor()
    monitor.record(0.05)
    monkeypatch.setattr(lw, "_MONITOR", monitor)
    from forven.control_plane.status import health_check

    payload = health_check()
    assert "event_loop" in payload["details"]
    assert payload["details"]["event_loop"]["window_ticks"] == 1


@pytest.mark.asyncio
async def test_watchdog_measures_injected_stall(monkeypatch):
    monitor = lw.LoopLagMonitor()
    monkeypatch.setattr(lw, "_MONITOR", monitor)
    monkeypatch.setattr(lw, "TICK_SECONDS", 0.01)

    task = asyncio.create_task(lw.run_loop_watchdog())
    try:
        await asyncio.sleep(0.05)  # a few clean ticks
        # Block the loop synchronously — exactly what a GIL-holding handler does.
        import time as _time

        _time.sleep(0.15)
        await asyncio.sleep(0.05)  # let the watchdog tick observe the stall
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert monitor.max_lag >= 0.1, f"watchdog missed the injected stall: {monitor.snapshot()}"


# ── WS send grace ────────────────────────────────────────────────────────────


class _TimeoutThenDeadWs:
    """send_json hangs (→ wait_for timeout) `hang_count` times, then raises."""

    def __init__(self, hang_count: int):
        self.hang_count = hang_count
        self.calls = 0

    async def send_json(self, payload):
        self.calls += 1
        if self.calls <= self.hang_count:
            await asyncio.sleep(60)  # forces wait_for timeout
        raise RuntimeError("socket closed")


class _HealthyWs:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _make_send_json(ws, timeout_seconds: float):
    """Reproduce live_ws's _send_json closure semantics against a fake socket."""
    from forven.api_domains import live_ws

    consecutive = [0]

    async def _send_json(payload: dict) -> bool:
        try:
            await asyncio.wait_for(ws.send_json(payload), timeout=timeout_seconds)
            consecutive[0] = 0
            return True
        except asyncio.TimeoutError:
            consecutive[0] += 1
            return consecutive[0] < live_ws.WS_SEND_TIMEOUT_GRACE
        except Exception:
            return False

    return _send_json, consecutive


@pytest.mark.asyncio
async def test_ws_send_survives_one_timeout_drops_after_grace():
    from forven.api_domains import live_ws

    assert live_ws.WS_SEND_TIMEOUT_GRACE == 2
    ws = _TimeoutThenDeadWs(hang_count=10)  # every send hangs
    send, consecutive = _make_send_json(ws, timeout_seconds=0.02)
    assert await send({"type": "prices"}) is True   # 1st timeout: tolerated
    assert await send({"type": "prices"}) is False  # 2nd consecutive: drop
    assert consecutive[0] == 2


@pytest.mark.asyncio
async def test_ws_send_success_resets_grace_counter():
    healthy = _HealthyWs()
    send, consecutive = _make_send_json(healthy, timeout_seconds=0.5)
    assert await send({"type": "ping"}) is True
    assert consecutive[0] == 0


@pytest.mark.asyncio
async def test_ws_send_hard_error_drops_immediately():
    ws = _TimeoutThenDeadWs(hang_count=0)  # raises on first call
    send, _ = _make_send_json(ws, timeout_seconds=0.5)
    assert await send({"type": "prices"}) is False  # no grace for a dead socket
