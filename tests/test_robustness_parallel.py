"""Parallel fan-out for robustness reruns (parameter jitter et al.).

The reruns are independent, DB-free backtests, so they are executed in chunks of
``workers`` instead of strictly serially. These tests pin the contract the jitter
verdict relies on: input-order results, single execution per thunk, a wall-clock
deadline that stops launching new chunks (verdict-from-completed), a serial
fast-path identical to the legacy loop, and exception propagation.
"""
from __future__ import annotations

import threading
import time

from forven.routers.robustness import (
    _ROBUSTNESS_RERUN_MAX_WORKERS,
    _resolve_robustness_workers,
    _run_backtests_chunked_parallel,
)


def test_results_preserved_in_input_order_despite_completion_order():
    # Later thunks finish FIRST (descending sleeps); results must still be in order.
    n = 6

    def make(i):
        return lambda: (time.sleep((n - i) * 0.01), i)[1]

    out, hit = _run_backtests_chunked_parallel([make(i) for i in range(n)], workers=n)
    assert out == list(range(n))
    assert hit is False


def test_each_thunk_runs_exactly_once_under_concurrency():
    calls: list[int] = []
    lock = threading.Lock()

    def make(i):
        def _fn():
            with lock:
                calls.append(i)
            return i
        return _fn

    out, hit = _run_backtests_chunked_parallel([make(i) for i in range(20)], workers=8)
    assert sorted(calls) == list(range(20))  # every thunk once, none dropped/duplicated
    assert out == list(range(20))
    assert hit is False


def test_deadline_stops_launching_new_chunks_and_returns_partial():
    # workers=2, each thunk ~0.1s, deadline 0.05s: the first chunk (2) runs, then
    # the elapsed check trips and no further chunks launch -> 2 of 6 results.
    def slow(i):
        return lambda: (time.sleep(0.1), i)[1]

    out, hit = _run_backtests_chunked_parallel(
        [slow(i) for i in range(6)], workers=2, deadline_s=0.05
    )
    assert hit is True
    assert out == [0, 1]  # only the first in-flight chunk completed, in order


def test_serial_fast_path_matches_legacy_deadline_semantics():
    # workers=1 -> serial path; deadline trips after the first result.
    def slow(i):
        return lambda: (time.sleep(0.05), i)[1]

    out, hit = _run_backtests_chunked_parallel(
        [slow(i) for i in range(5)], workers=1, deadline_s=0.01
    )
    assert hit is True
    assert out == [0]  # at least one always runs, then the deadline stops it


def test_no_deadline_runs_everything():
    out, hit = _run_backtests_chunked_parallel([lambda i=i: i for i in range(11)], workers=4)
    assert out == list(range(11))
    assert hit is False


def test_empty_input():
    assert _run_backtests_chunked_parallel([], workers=4) == ([], False)


def test_exception_in_a_thunk_propagates():
    def boom():
        raise ValueError("backtest blew up")

    try:
        _run_backtests_chunked_parallel([lambda: 1, boom, lambda: 3], workers=4)
        assert False, "expected the raising thunk to propagate"
    except ValueError as exc:
        assert "blew up" in str(exc)


def test_worker_resolver_defaults_serial_under_pytest_and_hard_caps():
    # Under pytest the unconfigured default is SERIAL so engine/gauntlet tests stay
    # deterministic; production defaults to the process-wide subprocess budget
    # (covered below).
    assert _resolve_robustness_workers(0, n_tasks=30) == 1         # auto -> serial in tests
    assert _resolve_robustness_workers(None, n_tasks=30) == 1      # unset -> serial in tests
    assert _resolve_robustness_workers("nope", n_tasks=10) == 1    # bad input -> serial, never 0
    # Explicit config honoured but hard-capped at the ceiling even when higher.
    assert _resolve_robustness_workers(4, n_tasks=30) == _ROBUSTNESS_RERUN_MAX_WORKERS
    assert _resolve_robustness_workers(999, n_tasks=30) == _ROBUSTNESS_RERUN_MAX_WORKERS
    # Never exceeds the task count.
    assert _resolve_robustness_workers(999, n_tasks=2) == 2
    assert _resolve_robustness_workers(0, n_tasks=1) == 1


def test_worker_resolver_production_default_follows_subprocess_budget(monkeypatch):
    # Outside pytest, the unconfigured default is min(ceiling, process-wide
    # subprocess budget): parallel reruns are ON but can never stack past the
    # global memory ceiling in strategies/concurrency.py.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", "2")
    assert _resolve_robustness_workers(0, n_tasks=30) == 2
    monkeypatch.setenv("FORVEN_BACKTEST_SUBPROCESS_BUDGET", "8")
    assert _resolve_robustness_workers(None, n_tasks=30) == _ROBUSTNESS_RERUN_MAX_WORKERS
    # Explicit 1 still restores the strict serial fast-path.
    assert _resolve_robustness_workers(1, n_tasks=30) == 1
