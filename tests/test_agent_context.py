"""Tests for build_agent_context() data schema injection."""
from datetime import date
from unittest.mock import patch


def _make_read_workspace_mock(schema_content: str | None):
    """Return a mock for read_workspace that returns schema_content for DATA_SCHEMA.md."""
    def _mock(filename: str, optional: bool = False) -> str | None:
        if filename == "DATA_SCHEMA.md":
            return schema_content
        # Return None for all other files so parts list stays short
        return None
    return _mock


def test_data_schema_injected_when_present():
    """build_agent_context includes DATA SCHEMA section when DATA_SCHEMA.md exists."""
    schema = "## Core Columns\n- timestamp\n- close"
    with patch("forven.context.read_workspace", side_effect=_make_read_workspace_mock(schema)):
        # Patch at the source (forven.db.get_db) not forven.context.get_db — context.py
        # imports get_db locally inside the function body, so there is no module-level
        # name to intercept at forven.context.get_db.
        with patch("forven.db.get_db", side_effect=Exception("no db")):
            from forven.context import build_agent_context
            result = build_agent_context(
                agent_id="quant-researcher",
                role_md="You are a quant researcher.",
            )
    assert "# DATA SCHEMA" in result
    assert "## Core Columns" in result


def test_data_schema_absent_does_not_raise():
    """build_agent_context works fine when DATA_SCHEMA.md is missing (optional=True)."""
    with patch("forven.context.read_workspace", side_effect=_make_read_workspace_mock(None)):
        # See first test for why we patch forven.db.get_db and not forven.context.get_db
        with patch("forven.db.get_db", side_effect=Exception("no db")):
            from forven.context import build_agent_context
            result = build_agent_context(
                agent_id="quant-researcher",
                role_md="You are a quant researcher.",
            )
    assert "# DATA SCHEMA" not in result
    # Original role block still present
    assert "# YOUR ROLE" in result


def test_build_agent_context_includes_strategy_diversity_guard_when_saturated():
    with patch("forven.context.read_workspace", side_effect=_make_read_workspace_mock(None)):
        with patch(
            "forven.context.render_strategy_diversity_guard",
            return_value="# STRATEGY DIVERSITY GUARD\n- RSI is cooled down.",
        ):
            with patch("forven.db.get_db", side_effect=Exception("no db")):
                from forven.context import build_agent_context

                result = build_agent_context(
                    agent_id="strategy-developer",
                    role_md="You create strategies.",
                    task_description="Generate a new strategy",
                )

    assert "# STRATEGY DIVERSITY GUARD" in result
    assert "RSI is cooled down" in result


def test_build_agent_context_uses_utc_daily_memory_paths_when_enabled():
    """Daily memory reads should align with the UTC-based filenames used by the runner."""
    requested_files: list[str] = []

    def _mock_read_workspace(filename: str, optional: bool = False) -> str | None:
        requested_files.append(filename)
        return None

    with patch("forven.context.read_workspace", side_effect=_mock_read_workspace):
        with patch("forven.context._utc_today", return_value=date(2026, 4, 14)):
            with patch("forven.context._utc_yesterday", return_value=date(2026, 4, 13)):
                with patch("forven.db.get_db", side_effect=Exception("no db")):
                    from forven.context import build_agent_context
                    build_agent_context(
                        agent_id="quant-researcher",
                        role_md="You are a quant researcher.",
                        include_daily_memory=True,
                    )

    assert "agents/quant-researcher/memory/2026-04-14.md" in requested_files
    assert "agents/quant-researcher/memory/2026-04-13.md" in requested_files
