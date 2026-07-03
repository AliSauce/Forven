"""Process-wide budget for concurrent backtest subprocesses.

Every isolated backtest (quick screen, sweep, confirmation, optimizer combo,
robustness rerun, walk-forward) spawns its own child process that re-imports
forven and holds its own candle frame — so PEAK MEMORY scales with the number
of subprocesses alive at once, not with thread counts. Historically that made
every parallel lever in the pipeline (gauntlet drain workers, param-jitter
rerun workers) default OFF: each was individually bounded, but the bounds
STACKED (drain x jitter x grid x robustness jobs) with no global ceiling, and
this host has a memory-pressure-restart history.

This module is that global ceiling. `backtest_subprocess_slot()` must be held
around every backtest subprocess spawn; at most `backtest_subprocess_budget()`
slots exist per Python process, so the parallel levers can default ON while
total subprocess memory stays bounded no matter how the levers combine.
Excess spawns QUEUE (they don't fail), so contention degrades to the old
serial pacing rather than to errors — a queued backtest is never mistaken for
a failed one (transient waits must never become merit failures).

Budget resolution: FORVEN_BACKTEST_SUBPROCESS_BUDGET env override, else the
`backtest_subprocess_budget` runtime setting (Settings > System > resource
tuning), else 4. Re-read on every acquire, so edits apply live without a
restart. Setting 1 restores strict one-subprocess-at-a-time behaviour.

The budget is per-process: if the API backend and the daemon both spawn
backtests, each gets its own ceiling.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager

log = logging.getLogger(__name__)

DEFAULT_BACKTEST_SUBPROCESS_BUDGET = 4
_BUDGET_MIN = 1
_BUDGET_MAX = 8

# A slot is held for one subprocess lifetime, which is itself hard-bounded by the
# backtest/walk-forward timeouts — so waiters always drain. This ceiling exists
# only as a backstop against a pathological leak: rather than wedge the whole
# pipeline forever, proceed over budget with a loud warning (the budget is a
# memory-pressure guard, not a correctness gate).
_MAX_SLOT_WAIT_SECONDS = 900.0

# Waiters re-check the (possibly edited) budget at this cadence even without a
# release notify, so a raised budget frees queued work promptly.
_SLOT_POLL_SECONDS = 5.0

_cond = threading.Condition()
_active = 0


def _runtime_int_setting(key: str, default: int, lo: int, hi: int) -> int:
    """Read a bounded int from the runtime settings KV; `default` on any miss."""
    try:
        from forven.db import kv_get

        raw = kv_get("forven:settings", {})
        value = int((raw or {}).get(key))
    except Exception:
        return default
    return max(lo, min(value, hi))


def backtest_subprocess_budget() -> int:
    """Max concurrent backtest subprocesses for THIS process (>=1, <=8)."""
    env = str(os.getenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", "") or "").strip()
    if env:
        try:
            return max(_BUDGET_MIN, min(int(env), _BUDGET_MAX))
        except ValueError:
            pass
    return _runtime_int_setting(
        "backtest_subprocess_budget",
        DEFAULT_BACKTEST_SUBPROCESS_BUDGET,
        _BUDGET_MIN,
        _BUDGET_MAX,
    )


def active_backtest_subprocess_slots() -> int:
    """Currently-held slots (observability/tests)."""
    with _cond:
        return _active


@contextmanager
def backtest_subprocess_slot(purpose: str = "backtest"):
    """Hold one unit of the process-wide subprocess budget.

    Blocks while the budget is exhausted, re-reading the budget on each wake so
    a settings edit applies to already-queued waiters. After
    `_MAX_SLOT_WAIT_SECONDS` it proceeds over budget with a warning instead of
    wedging the pipeline (see module docstring).
    """
    global _active
    start = time.monotonic()
    logged_wait = False
    with _cond:
        while _active >= backtest_subprocess_budget():
            waited = time.monotonic() - start
            if waited >= _MAX_SLOT_WAIT_SECONDS:
                log.warning(
                    "Backtest subprocess budget: proceeding OVER budget after waiting %.0fs "
                    "(purpose=%s active=%d budget=%d) — possible leaked slot",
                    waited, purpose, _active, backtest_subprocess_budget(),
                )
                break
            if waited >= 30.0 and not logged_wait:
                log.info(
                    "Backtest subprocess budget: %s queued behind %d active (budget=%d)",
                    purpose, _active, backtest_subprocess_budget(),
                )
                logged_wait = True
            _cond.wait(timeout=_SLOT_POLL_SECONDS)
        _active += 1
    try:
        yield
    finally:
        with _cond:
            _active = max(0, _active - 1)
            _cond.notify_all()
