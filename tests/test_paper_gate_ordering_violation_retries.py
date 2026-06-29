"""A gauntlet -> paper promotion gate that fails on an ORDERING / stale-validation
verdict must RETRY (blocked_runtime), never drain to a terminal ``failed_gate``.

Root cause it guards (S03523): optimization re-ran, producing fresh params, but the
paper-promotion gate was evaluated in the ~20-min window before walk_forward re-ran on
those params. The gate's artifact-ordering check ("walk_forward was run before
optimization — re-run after optimization") tripped, the gate-check step returned
``failed_gate``, the workflow drained to ``status='failed_gate'`` after burning its
retries, and demote_failed_gate_strategies ARCHIVED a genuinely-passing strategy.

The ordering violation is PENDING RE-VALIDATION, not a merit failure — it self-resolves
the instant walk_forward re-runs. So the gate must return a retryable block whose
reason_code is in engine._NO_DRAIN_REASON_CODES (never burns down to failed_gate); the
2-day un-promotable hygiene backstop catches any genuine deadlock. Generic: ANY strategy
whose validation re-runs slightly after its optimization was being mis-archived.
"""

from __future__ import annotations

import forven.gauntlet.status as gstatus
import forven.gauntlet.tasks as tasks
from forven.gauntlet.engine import _NO_DRAIN_REASON_CODES


def _patch_status_ok(monkeypatch):
    monkeypatch.setattr(
        gstatus,
        "get_strategy_gauntlet_status",
        lambda sid: {"ok": True, "missing_required": []},
    )


def _patch_transition(monkeypatch, transition: dict):
    # Promotion gate also picks an execution profile (best-effort) before the
    # transition — stub it so the test doesn't touch the backtest store.
    monkeypatch.setattr(tasks, "_select_and_persist_execution_profile", lambda *a, **k: {})
    monkeypatch.setattr(tasks, "_transition_to_paper", lambda **k: transition)


def test_no_drain_set_includes_stale_validation():
    # The reason_codes the gate emits for pending-re-validation MUST be exempt from
    # the failed_gate drain, or the retry would still eventually archive the strategy.
    assert "stale_validation" in _NO_DRAIN_REASON_CODES
    assert "artifacts_pending" in _NO_DRAIN_REASON_CODES


def test_ordering_violation_reason_code_retries_not_failed(monkeypatch):
    _patch_status_ok(monkeypatch)
    _patch_transition(
        monkeypatch,
        {
            "to": "gauntlet",  # blocked — did NOT reach paper
            "reason_code": "stale_validation",
            "reason": "Ordering violation: walk_forward was run before optimization — re-run after optimization",
        },
    )
    out = tasks.run_paper_promotion_gate({"strategy_id": "S-ORD"}, {})
    assert out["status"] == "blocked_runtime", "stale_validation must RETRY, not failed_gate"
    assert out["status"] != "failed_gate"
    assert out["retryable"] is True
    assert out["reason_code"] in _NO_DRAIN_REASON_CODES


def test_ordering_violation_text_only_retries(monkeypatch):
    # Defence in depth: even if reason_code isn't propagated, the gate message text
    # alone ("ordering violation" / "re-run after optimization") must trigger the retry.
    _patch_status_ok(monkeypatch)
    _patch_transition(
        monkeypatch,
        {
            "to": "gauntlet",
            "reason": "Ordering violation: walk_forward was run before optimization — re-run after optimization",
        },
    )
    out = tasks.run_paper_promotion_gate({"strategy_id": "S-ORD2"}, {})
    assert out["status"] == "blocked_runtime"
    assert out["retryable"] is True
    assert out["reason_code"] in _NO_DRAIN_REASON_CODES


def test_genuine_merit_failure_still_terminal(monkeypatch):
    # A real quality verdict (NOT pending re-validation) must still drain to
    # failed_gate so a truly-failing strategy is archived, not retried forever.
    _patch_status_ok(monkeypatch)
    _patch_transition(
        monkeypatch,
        {
            "to": "gauntlet",
            "reason_code": "overfitting_guardrails",
            "reason": "IS->OOS Sharpe gap 2.10 > 1.5 (reject)",
        },
    )
    out = tasks.run_paper_promotion_gate({"strategy_id": "S-FAIL"}, {})
    assert out["status"] == "failed_gate", "a genuine merit failure must stay terminal"


def test_successful_promotion_passes(monkeypatch):
    _patch_status_ok(monkeypatch)
    _patch_transition(monkeypatch, {"to": "paper"})
    out = tasks.run_paper_promotion_gate({"strategy_id": "S-OK"}, {})
    assert out["status"] == "passed"
