"""Chat toolset tiers — single-source-of-truth validation.

The operator <-> Forven chat exposes two read/act tiers defined ONCE in
``forven.agents.tool_definitions``:

  * ``CHAT_ASK_TOOL_NAMES`` — read-only grounding tools ("Chat" mode).
  * ``CHAT_ACT_TOOL_NAMES`` — Ask set + a few safe action tools ("Command" mode).

These tests guarantee every name in both sets resolves to a registered tool
(importing ``forven.agents.runner`` triggers tool registration), that the Ask
set is read-only (no mutating tools leak in), and that Act is a strict superset
of Ask containing the expected action tools.
"""
from __future__ import annotations


def _registry():
    # Importing the runner triggers @register_tool decorators across all
    # tool modules so the global registry is populated.
    import forven.agents.runner  # noqa: F401
    from forven.agents.tool_definitions import _ensure_tools_imported
    from forven.agents.tool_registry import _REGISTRY

    _ensure_tools_imported()
    return _REGISTRY


def test_chat_ask_tool_names_all_registered():
    from forven.agents.tool_definitions import CHAT_ASK_TOOL_NAMES

    registry = _registry()
    missing = sorted(name for name in CHAT_ASK_TOOL_NAMES if name not in registry)
    assert not missing, f"CHAT_ASK_TOOL_NAMES not in registry: {missing}"


def test_chat_act_tool_names_all_registered():
    from forven.agents.tool_definitions import CHAT_ACT_TOOL_NAMES

    registry = _registry()
    missing = sorted(name for name in CHAT_ACT_TOOL_NAMES if name not in registry)
    assert not missing, f"CHAT_ACT_TOOL_NAMES not in registry: {missing}"


def test_validate_helper_reports_no_missing():
    from forven.agents.tool_definitions import _validate_chat_toolsets

    assert _validate_chat_toolsets() == []


def test_act_is_strict_superset_of_ask():
    from forven.agents.tool_definitions import (
        CHAT_ACT_TOOL_NAMES,
        CHAT_ASK_TOOL_NAMES,
    )

    assert CHAT_ASK_TOOL_NAMES <= CHAT_ACT_TOOL_NAMES
    # Act adds the safe action tools on top of the read-only Ask set.
    added = CHAT_ACT_TOOL_NAMES - CHAT_ASK_TOOL_NAMES
    assert {
        "assign_agent_task",
        "promote_strategy",
        "create_strategy",
        "forven_run_backtest",
    } <= added


def test_ask_set_is_read_only():
    """The Ask tier must not contain any mutating / side-effecting tool."""
    from forven.agents.tool_definitions import CHAT_ASK_TOOL_NAMES

    forbidden = {
        # mutation / lifecycle
        "assign_agent_task",
        "promote_strategy",
        "create_strategy",
        "factory_reset",
        "transition_stage",
        # writes / code exec
        "write_file",
        "run_shell",
        "run_code",
        "register_strategy",
        # backtesting jobs that spawn work / write results
        "forven_run_backtest",
        "forven_create_strategy",
        "forven_run_optimization",
        "forven_run_verdict",
        # exchange order placement
        "place_order",
        "close_position",
        "cancel_orders",
        "update_trade",
    }
    leaked = sorted(CHAT_ASK_TOOL_NAMES & forbidden)
    assert not leaked, f"read-only Ask set leaked mutating tools: {leaked}"


def test_act_set_contains_no_destructive_or_exchange_tools():
    """Command mode is 'safe actions' only — no destructive or order tools."""
    from forven.agents.tool_definitions import CHAT_ACT_TOOL_NAMES

    forbidden = {
        "factory_reset",
        "transition_stage",
        "place_order",
        "close_position",
        "cancel_orders",
        "update_trade",
        "run_shell",
    }
    leaked = sorted(CHAT_ACT_TOOL_NAMES & forbidden)
    assert not leaked, f"Act set leaked unsafe tools: {leaked}"
