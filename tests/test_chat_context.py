"""Chat-context grounding tests."""
from __future__ import annotations

import asyncio
import json

import forven.context as ctx


# ---------------------------------------------------------------------------
# build_chat_context grounding blocks
# ---------------------------------------------------------------------------

def test_build_chat_context_includes_pipeline_and_pending_approvals(monkeypatch):
    """Chat context should surface a condensed pipeline + pending-approvals block
    so the Brain can answer 'what's in the pipeline?' / 'anything on me?'."""

    # Keep the rest of the context cheap/empty so the test is deterministic.
    monkeypatch.setattr(ctx, "read_workspace", lambda *a, **k: "", raising=True)
    monkeypatch.setattr(ctx, "_render_operator_profile", lambda: None, raising=True)
    monkeypatch.setattr(ctx, "_format_portfolio_status", lambda: "", raising=True)
    monkeypatch.setattr(ctx, "_format_strategy_registry", lambda: "", raising=True)
    monkeypatch.setattr(ctx, "_format_market_regime", lambda: "", raising=True)
    monkeypatch.setattr(ctx, "_format_recent_trades", lambda *a, **k: "", raising=True)

    monkeypatch.setattr(
        ctx,
        "_format_evolution_status",
        lambda: "# EVOLUTION PIPELINE\n- gauntlet: 2 (S00719, S00825)",
        raising=True,
    )
    monkeypatch.setattr(
        ctx,
        "_format_recent_approval_feedback",
        lambda limit=20: (
            "# APPROVAL FEEDBACK\n"
            "- [PENDING_APPROVAL] #5 strategy/S00719 (promotion) — paper -> live\n"
            "- [APPROVED] #4 strategy/S00200 (promotion)"
        ),
        raising=True,
    )

    out = ctx.build_chat_context()
    assert "# EVOLUTION PIPELINE" in out
    assert "# PENDING APPROVALS (waiting on you)" in out
    # Only the pending entry should survive the compaction.
    assert "#5 strategy/S00719" in out
    assert "#4 strategy/S00200" not in out


def test_pending_approvals_compact_empty_when_none(monkeypatch):
    monkeypatch.setattr(
        ctx,
        "_format_recent_approval_feedback",
        lambda limit=20: "# APPROVAL FEEDBACK\n- [APPROVED] #1 strategy/S1 (promotion)",
        raising=True,
    )
    assert ctx._format_pending_approvals_compact() == ""


def test_pending_approvals_compact_caps_and_summarizes(monkeypatch):
    lines = ["# APPROVAL FEEDBACK"]
    for i in range(12):
        lines.append(f"- [PENDING_APPROVAL] #{i} strategy/S{i} (promotion)")
    monkeypatch.setattr(
        ctx,
        "_format_recent_approval_feedback",
        lambda limit=20: "\n".join(lines),
        raising=True,
    )
    out = ctx._format_pending_approvals_compact(limit=8)
    pending_rendered = [ln for ln in out.splitlines() if "[PENDING_APPROVAL]" in ln]
    assert len(pending_rendered) == 8
    assert "and 4 more" in out


# ---------------------------------------------------------------------------
# runtime_worker is_chat branch — CHAT_ACT toolset wiring
# ---------------------------------------------------------------------------

def test_run_brain_task_chat_uses_act_toolset(forven_db, monkeypatch):
    from forven import runtime_worker
    from forven.agents.tool_definitions import CHAT_ACT_TOOL_NAMES

    captured: dict = {}

    async def _fake_call_with_tools(provider, model, messages, context, tools=None):
        captured["tools"] = tools
        captured["last_message"] = messages[-1]["content"]
        return ("Looks healthy. — Forven", {})

    monkeypatch.setattr("forven.context.build_chat_context", lambda: "ctx")
    monkeypatch.setattr("forven.brain.resolve_brain_provider_model", lambda p, m: ("openai", "gpt-5.2"))
    monkeypatch.setattr("forven.agents.runner._call_with_tools", _fake_call_with_tools)
    monkeypatch.setattr("forven.agents.runner.set_tool_context", lambda *a, **k: ())
    monkeypatch.setattr("forven.agents.runner.reset_tool_context", lambda *_: None)

    task = {
        "id": 7,
        "payload": json.dumps(
            {
                "source": "ui_chat",
                "message": "How is S00719 doing in the gauntlet right now?",
            }
        ),
    }

    asyncio.run(runtime_worker._run_brain_task(task))

    # The chat branch must offer the CHAT_ACT tool tier (action-capable Command mode).
    tool_names = {t["name"] for t in (captured["tools"] or [])}
    assert tool_names, "expected a non-empty chat tool list"
    assert tool_names <= CHAT_ACT_TOOL_NAMES, f"unexpected tools leaked: {tool_names - CHAT_ACT_TOOL_NAMES}"
    assert "assign_agent_task" in tool_names  # an action tool reachable in Command mode
    assert "read_file" in tool_names           # a grounding tool
