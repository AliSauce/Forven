"""Runner tool-call context and legacy compatibility helpers."""

import os
import re
import shlex
from contextvars import ContextVar, Token

_current_agent_id_var: ContextVar[str | None] = ContextVar("forven_current_agent_id", default=None)
_current_task_display_id_var: ContextVar[str | None] = ContextVar("forven_current_task_display_id", default=None)
_current_strategy_id_var: ContextVar[str | None] = ContextVar("forven_current_strategy_id", default=None)
# Phase 5 / P5-T05: the per-task tools_context (scheduled/interactive/recovery/
# research). Consulted by tool_registry.get_tools_for_agent (list filtering) and
# execute_tool (dispatch boundary) so the operator-configured per-context
# default-deny rules actually bind at runtime. None = no context gating.
_current_tools_context_var: ContextVar[str | None] = ContextVar("forven_current_tools_context", default=None)
# Backward compatibility: older tests/modules imported this symbol directly.
_current_agent_id = _current_agent_id_var


def set_tool_context(
    agent_id: str | None,
    task_display_id: str | None = None,
    strategy_id: str | None = None,
    tools_context: str | None = None,
) -> tuple[Token, Token, Token, Token]:
    """Set per-task tool-call context and return reset tokens."""
    return (
        _current_agent_id_var.set(agent_id),
        _current_task_display_id_var.set(task_display_id),
        _current_strategy_id_var.set(strategy_id),
        _current_tools_context_var.set(tools_context),
    )


def reset_tool_context(tokens: tuple[Token, ...]) -> None:
    """Restore previous tool-call context using reset tokens.

    Tolerates both the legacy 3-tuple and the 4-tuple (with tools_context) so
    any caller that built tokens before this field existed still resets cleanly.
    """
    _current_agent_id_var.reset(tokens[0])
    _current_task_display_id_var.reset(tokens[1])
    _current_strategy_id_var.reset(tokens[2])
    if len(tokens) > 3:
        _current_tools_context_var.reset(tokens[3])


def _translate_find_command_for_windows(command: str) -> str:
    """Translate common Unix-style find invocations into PowerShell."""
    stripped = str(command or "").strip()
    if not stripped.lower().startswith("find "):
        return command

    try:
        tokens = shlex.split(stripped, posix=True)
    except ValueError:
        return command

    if len(tokens) < 2 or tokens[0] != "find":
        return command

    search_path = tokens[1]
    patterns: list[str] = []
    limit: int | None = None
    idx = 2
    while idx < len(tokens):
        token = tokens[idx]
        if token == "-name" and idx + 1 < len(tokens):
            patterns.append(tokens[idx + 1])
            idx += 2
            continue
        if token == "head" and idx + 1 < len(tokens):
            raw_limit = str(tokens[idx + 1]).lstrip("-")
            if raw_limit.isdigit():
                limit = int(raw_limit)
            idx += 2
            continue
        idx += 1

    if not patterns:
        return command

    def _ps_quote(value: str) -> str:
        return "'" + str(value or "").replace("'", "''") + "'"

    normalized_path = str(search_path or "").replace("/", "\\")
    include_expr = ", ".join(_ps_quote(pattern) for pattern in patterns)
    limit_expr = f" | Select-Object -First {limit}" if limit else ""
    return (
        "powershell -NoProfile -Command "
        f"\"$ErrorActionPreference='SilentlyContinue'; "
        f"Get-ChildItem -Path {_ps_quote(normalized_path)} -Recurse -File -Include {include_expr}"
        f"{limit_expr} | Select-Object -ExpandProperty FullName\""
    )


def _normalize_legacy_paths(
    command: str,
    *,
    is_windows: bool | None = None,
    home: str | None = None,
) -> str:
    """Normalize agent shell commands for the current runtime."""
    if not command:
        return command

    runtime_is_windows = os.name == "nt" if is_windows is None else bool(is_windows)
    resolved_home = home or os.path.expanduser("~")

    # Normalize slashes for comparison
    home_norm = resolved_home.replace("\\", "/")
    cmd_norm = command.replace("\\", "/")

    normalized = command

    # Historically used path: ~/judex and /home/<user>/judex.
    # Current runtime home is ~/.forven.
    normalized = re.sub(r"(?<!\w)~/judex(?=[^\w]|$)", "~/.forven", normalized)
    normalized = re.sub(r"(?<!\w)~/\.judex(?=[^\w]|$)", "~/.forven", normalized)

    current_home_alias = home_norm + "/.forven"
    legacy_home_aliases = (
        home_norm + "/judex",
        home_norm + "/.judex",
    )
    for legacy_home_path in legacy_home_aliases:
        if legacy_home_path in cmd_norm:
            normalized = normalized.replace(legacy_home_path, current_home_alias)
            normalized = normalized.replace(legacy_home_path.replace("/", "\\"), current_home_alias.replace("/", "\\"))

    if not runtime_is_windows:
        return normalized

    # cmd.exe does not expand ~ or /dev/null the way Unix shells do.
    normalized = normalized.replace("~/", home_norm + "/")
    normalized = normalized.replace("~\\", resolved_home + "\\")
    normalized = normalized.replace("/dev/null", "nul")

    # Translate the most common Unix utilities agents reach for first.
    normalized = re.sub(r"(?<![\w-])ls(?:\s+-[A-Za-z]+)?(?=(?:\s|$|[|&]))", "dir", normalized)
    normalized = re.sub(r"(?<![\w-])pwd(?=(?:\s|$|[|&]))", "cd", normalized)
    normalized = re.sub(r"(?<![\w-])cat(?=\s)", "type", normalized)

    translated_find = _translate_find_command_for_windows(normalized)
    if translated_find != normalized:
        return translated_find

    return normalized
