"""Regression tests for the /api/strategies/batch-transition endpoint.

`transition_stage` does NOT raise when a move is *blocked* (WIP cap full,
operator approval required, gate failure, …). It returns a dict whose
"blocked_reason" key is set. The batch endpoint must classify those as
failures rather than silently reporting them as successfully transitioned.
A success or a no-op (already in the target stage / already archived) has no
"blocked_reason" and must report success.
"""

from __future__ import annotations

import forven.brain as brain
from forven.routers.strategies import (
    BatchTransitionBody,
    batch_transition_strategies,
)


def test_blocked_ids_land_in_failed_and_noop_reports_success(monkeypatch):
    """A WIP-capped id, an approval-required id, a real success, and an
    already-archived re-archive (no-op) are each classified correctly."""

    def fake_transition_stage(*, strategy_id, target_stage, reason, actor, force):
        if strategy_id == "wip-capped":
            # Mirrors _record_blocked_transition() for a full WIP stage.
            return {
                "strategy_id": strategy_id,
                "from": "quick_screen",
                "to": "quick_screen",
                "requested_to": target_stage,
                "blocked_reason": "Gauntlet WIP cap reached (50/50)",
                "reason_code": "wip_cap_exceeded",
            }
        if strategy_id == "needs-approval":
            # Mirrors the operator-approval block path (adds approval_id).
            blocked = {
                "strategy_id": strategy_id,
                "from": "gauntlet",
                "to": "gauntlet",
                "requested_to": target_stage,
                "blocked_reason": "Promotion approval queued (approval #42)",
                "reason_code": "operator_promotion_approval_required",
            }
            blocked["approval_id"] = "42"
            return blocked
        if strategy_id == "already-archived":
            # No-op: normalized_target == current_stage. No blocked_reason.
            return {
                "strategy_id": strategy_id,
                "from": "archived",
                "to": "archived",
                "display_id": "S00001",
                "owner": None,
            }
        # Genuine successful transition. No blocked_reason.
        return {
            "strategy_id": strategy_id,
            "from": "quick_screen",
            "to": target_stage,
            "display_id": "S00002",
            "owner": "gauntlet-agent",
        }

    monkeypatch.setattr(brain, "transition_stage", fake_transition_stage)

    body = BatchTransitionBody(
        ids=["wip-capped", "needs-approval", "already-archived", "good-one"],
        stage="archived",
        reason="batch transition from lab manager",
    )
    resp = batch_transition_strategies(body)

    # Blocked ids must NOT be reported as transitioned.
    assert "wip-capped" not in resp["transitioned"]
    assert "needs-approval" not in resp["transitioned"]

    # No-op re-archive and the genuine success report success.
    assert "already-archived" in resp["transitioned"]
    assert "good-one" in resp["transitioned"]

    failed_ids = {f["id"] for f in resp["failed"]}
    assert failed_ids == {"wip-capped", "needs-approval"}

    # The approval-required failure surfaces the approval id; the WIP one does not.
    by_id = {f["id"]: f for f in resp["failed"]}
    assert by_id["needs-approval"]["approval_id"] == "42"
    assert "approval_id" not in by_id["wip-capped"]
    assert "WIP cap" in by_id["wip-capped"]["error"]

    # Any failure flips ok to False.
    assert resp["ok"] is False


def test_operator_batch_forces_bypass(monkeypatch):
    """An operator's Forge batch transitions with force=True and a user actor, so an
    explicit Archive/Move bypasses the dethrone-approval gate (like the single-row
    Move Stage) instead of silently queuing approvals."""
    captured: dict = {}

    def fake_transition_stage(*, strategy_id, target_stage, reason, actor, force):
        captured["actor"] = actor
        captured["force"] = force
        return {"strategy_id": strategy_id, "to": target_stage}

    monkeypatch.setattr(brain, "transition_stage", fake_transition_stage)

    resp = batch_transition_strategies(BatchTransitionBody(ids=["a"], stage="archived"))

    assert resp["ok"] is True
    assert captured["force"] is True
    assert captured["actor"] == "ui"


def test_batch_promotion_to_capital_stage_does_not_force_the_gate(monkeypatch):
    """A batch transition to a CAPITAL stage (paper/live_graduated/deployed) must NOT pass
    force=True — that would bypass evaluate_promotion (transition_stage only runs the gate
    when force is False), letting any caller bulk-promote unvetted strategies straight to
    live. Promotions go through the gate (force=False); only non-capital moves (archive /
    reject / demote) force past the operator-approval gates."""
    forced: dict = {}

    def fake_transition_stage(*, strategy_id, target_stage, reason, actor, force):
        forced[target_stage] = force
        return {"strategy_id": strategy_id, "to": target_stage}

    monkeypatch.setattr(brain, "transition_stage", fake_transition_stage)

    for cap in ("paper", "live_graduated", "deployed"):
        batch_transition_strategies(BatchTransitionBody(ids=["a"], stage=cap))
        assert forced[cap] is False, f"{cap} must NOT be force-promoted (gate bypass)"
    # Non-capital moves (e.g. the frontend's hardcoded Archive) still force, as before.
    batch_transition_strategies(BatchTransitionBody(ids=["a"], stage="archived"))
    assert forced["archived"] is True


def test_all_success_reports_ok_true(monkeypatch):
    """When every transition succeeds (or is a no-op), ok is True and failed empty."""

    def fake_transition_stage(*, strategy_id, target_stage, reason, actor, force):
        return {
            "strategy_id": strategy_id,
            "from": "quick_screen",
            "to": target_stage,
            "display_id": "S00003",
            "owner": "gauntlet-agent",
        }

    monkeypatch.setattr(brain, "transition_stage", fake_transition_stage)

    body = BatchTransitionBody(ids=["a", "b", "c"], stage="archived")
    resp = batch_transition_strategies(body)

    assert resp["ok"] is True
    assert resp["failed"] == []
    assert resp["transitioned"] == ["a", "b", "c"]


def test_raised_exception_still_caught_as_failure(monkeypatch):
    """A genuine raise (e.g. ValueError for an invalid transition) is still
    caught and reported as a failure, preserving the existing try/except."""

    def fake_transition_stage(*, strategy_id, target_stage, reason, actor, force):
        if strategy_id == "boom":
            raise ValueError("Invalid transition: quick_screen -> live_graduated")
        return {"strategy_id": strategy_id, "to": target_stage}

    monkeypatch.setattr(brain, "transition_stage", fake_transition_stage)

    body = BatchTransitionBody(ids=["ok-id", "boom"], stage="archived")
    resp = batch_transition_strategies(body)

    assert resp["ok"] is False
    assert resp["transitioned"] == ["ok-id"]
    assert len(resp["failed"]) == 1
    assert resp["failed"][0]["id"] == "boom"
    assert "Invalid transition" in resp["failed"][0]["error"]
