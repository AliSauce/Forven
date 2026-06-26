"""The once-per-process scheduler-bootstrap guard: init_db + all migrations +
job reconciliation must run ONCE per process, not on every get_scheduler() poll
(a profiled hot path). force=True re-runs intentionally; conftest resets the flag
per test for isolation."""
from __future__ import annotations


def test_bootstrap_scheduler_jobs_runs_once_per_process(forven_db, monkeypatch):
    import forven.api_core as core

    core._SCHEDULER_BOOTSTRAP_DONE = False
    calls = {"init_db": 0}
    monkeypatch.setattr("forven.db.init_db", lambda: calls.__setitem__("init_db", calls["init_db"] + 1))
    monkeypatch.setattr("forven.brain.run_gauntlet_backtest_migration", lambda: None)
    monkeypatch.setattr(core, "get_jobs", lambda: [{"id": "x"}])
    monkeypatch.setattr(core, "reconcile_forven_jobs", lambda: {"removed": 0, "added": 0})
    monkeypatch.setattr(core, "ensure_monitoring_jobs", lambda: 0)
    monkeypatch.setattr(core, "migrate_legacy_scanner_cadence", lambda: False)
    monkeypatch.setattr(core, "migrate_data_manager_jobs", lambda: 0)

    for _ in range(3):
        core._bootstrap_scheduler_jobs()
    assert calls["init_db"] == 1  # guarded: heavy init/migrations run once, not per poll

    core._bootstrap_scheduler_jobs(force=True)
    assert calls["init_db"] == 2  # force=True re-runs intentionally
