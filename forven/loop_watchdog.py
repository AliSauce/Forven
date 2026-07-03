"""Event-loop lag watchdog for the API process.

The backend is ONE process: uvicorn's request loop + the live WebSocket share
the GIL with the scheduler, agent/brain loops, the in-process daemon, and every
threadpool request handler. Any code path that holds the GIL (or blocks the
request loop directly) for more than ~2.5s starves the WS send window and drops
live clients — historically these storms were invisible until a user reported a
disconnect (see the /api/data/coverage parquet column-load incident).

This watchdog makes them visible and attributable: a background task on the
request loop sleeps a fixed tick and measures scheduling drift. Drift IS the
starvation — it can only come from the loop being blocked or the GIL being
held. Stalls are throttle-logged with their magnitude (timestamp correlates
them with the offending request in api.log) and surfaced in /api/health.

Deliberately lean: a deque of recent samples, a max, a counter. No dashboards.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("forven.loop_watchdog")

TICK_SECONDS = 0.5
# The WS send timeout is 2.5s: a stall at/above it WILL time out an in-flight
# send. Warn well before that so near-misses are visible too.
WARN_LAG_SECONDS = 1.0
WS_RISK_LAG_SECONDS = 2.5
_WARN_THROTTLE_SECONDS = 30.0
_SAMPLE_WINDOW = 600  # ~5 minutes of ticks


class LoopLagMonitor:
    def __init__(self) -> None:
        self.samples: deque[float] = deque(maxlen=_SAMPLE_WINDOW)
        self.last_lag = 0.0
        self.max_lag = 0.0
        self.max_lag_at: str | None = None
        self.stall_count = 0  # samples >= WARN_LAG_SECONDS
        self.ws_risk_count = 0  # samples >= WS_RISK_LAG_SECONDS (would drop a WS send)
        self._last_warn_monotonic = 0.0

    def record(self, lag: float) -> None:
        self.samples.append(lag)
        self.last_lag = lag
        if lag > self.max_lag:
            self.max_lag = lag
            self.max_lag_at = datetime.now(timezone.utc).isoformat()
        if lag < WARN_LAG_SECONDS:
            return
        self.stall_count += 1
        if lag >= WS_RISK_LAG_SECONDS:
            self.ws_risk_count += 1
        now = time.monotonic()
        if now - self._last_warn_monotonic >= _WARN_THROTTLE_SECONDS:
            self._last_warn_monotonic = now
            log.warning(
                "Event loop stalled %.2fs (warn>=%.1fs, ws-drop-risk>=%.1fs) — a handler or "
                "thread held the GIL/loop; correlate with the request logged around this "
                "timestamp. stalls=%d ws_risk=%d max=%.2fs",
                lag, WARN_LAG_SECONDS, WS_RISK_LAG_SECONDS,
                self.stall_count, self.ws_risk_count, self.max_lag,
            )

    def snapshot(self) -> dict:
        recent = sorted(self.samples)
        p95 = recent[int(len(recent) * 0.95)] if recent else 0.0
        return {
            "last_lag_ms": round(self.last_lag * 1000, 1),
            "p95_lag_ms": round(p95 * 1000, 1),
            "max_lag_ms": round(self.max_lag * 1000, 1),
            "max_lag_at": self.max_lag_at,
            "stalls_over_warn": self.stall_count,
            "stalls_over_ws_risk": self.ws_risk_count,
            "window_ticks": len(self.samples),
            "tick_seconds": TICK_SECONDS,
        }


_MONITOR = LoopLagMonitor()


def loop_lag_snapshot() -> dict:
    """Current lag stats — safe to call from any thread (GIL-atomic reads)."""
    return _MONITOR.snapshot()


def loop_lag_issues() -> list[str]:
    """Health-check issues derived from recent lag (empty when healthy)."""
    snap = _MONITOR.snapshot()
    issues: list[str] = []
    if snap["stalls_over_ws_risk"] > 0:
        issues.append(
            f"event loop stalled >= {WS_RISK_LAG_SECONDS:.1f}s "
            f"{snap['stalls_over_ws_risk']}x (WS drop risk; max {snap['max_lag_ms']:.0f}ms "
            f"at {snap['max_lag_at']})"
        )
    return issues


async def run_loop_watchdog() -> None:
    """Run on the uvicorn request loop (spawned from the lifespan)."""
    log.info("Event-loop lag watchdog started (tick=%.1fs warn=%.1fs)", TICK_SECONDS, WARN_LAG_SECONDS)
    while True:
        before = time.monotonic()
        await asyncio.sleep(TICK_SECONDS)
        lag = max(0.0, (time.monotonic() - before) - TICK_SECONDS)
        _MONITOR.record(lag)
