"""Notification routing policy for Discord and in-app delivery."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

DEFAULT_RESPONSE_CHANNEL_ALIASES = ("chat",)

DEFAULT_NOTIFICATION_PREFERENCES: dict[str, Any] = {
    "discord_mode": "policy",
    "response_channels": list(DEFAULT_RESPONSE_CHANNEL_ALIASES),
    "approval_required_to_discord": True,
    "approval_resolved_to_discord": False,
    "trade_opened_to_discord": True,
    "trade_closed_to_discord": True,
    "trade_failed_to_discord": True,
    "trade_blocked_to_discord": True,
    "agent_completion_to_discord": False,
    "agent_failure_to_discord": True,
    "pipeline_transition_to_discord": False,
    "system_degraded_to_discord": True,
    "system_recovered_to_discord": True,
    "risk_critical_to_discord": True,
    "brain_response_to_discord": True,
    "digests_to_discord": True,
}


def default_notification_preferences() -> dict[str, Any]:
    """Return a detached default preferences payload."""
    return deepcopy(DEFAULT_NOTIFICATION_PREFERENCES)


def merge_notification_preferences(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge persisted preferences onto defaults with safe coercion."""
    merged = default_notification_preferences()
    if not isinstance(raw, Mapping):
        return merged

    for key, default in DEFAULT_NOTIFICATION_PREFERENCES.items():
        if key not in raw:
            continue
        value = raw.get(key)
        if isinstance(default, bool):
            merged[key] = _coerce_bool(value, default)
        elif isinstance(default, list):
            merged[key] = _coerce_str_list(value, default)
        else:
            merged[key] = str(value or default).strip() or default

    mode = str(merged.get("discord_mode") or "policy").strip().lower()
    if mode not in {"legacy", "shadow", "policy"}:
        mode = "policy"
    merged["discord_mode"] = mode
    merged["response_channels"] = _coerce_str_list(
        merged.get("response_channels"),
        list(DEFAULT_RESPONSE_CHANNEL_ALIASES),
    )
    return merged


def resolve_notification_policy(
    event: Mapping[str, Any],
    preferences: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve routing for a notification event."""
    prefs = merge_notification_preferences(preferences)
    event_type = str(event.get("event_type") or "info").strip().lower()
    severity = str(event.get("severity") or "info").strip().lower()
    metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
    channel_name = str(event.get("channel_name") or metadata.get("channel_name") or "").strip() or None
    channel_id = str(event.get("channel_id") or metadata.get("channel_id") or "").strip() or None

    execution_mode = str(metadata.get("execution_mode") or metadata.get("execution_type") or "").strip().lower()
    is_live_trade = execution_mode in {"live", "mainnet"} or execution_mode.startswith("live")
    trade_channel = "autopilot" if is_live_trade else "paper-trades"

    policy: dict[str, Any] = {
        "delivery_mode": "app_only",
        "channel_name": channel_name,
        "channel_id": channel_id,
        "cooldown_seconds": 0,
        "send_to_app": True,
        "send_to_discord": False,
    }

    if event_type == "approval_required":
        policy.update(
            delivery_mode="discord_immediate",
            channel_name=channel_name or "approvals",
            send_to_discord=bool(prefs.get("approval_required_to_discord", True)),
            cooldown_seconds=120,
        )
    elif event_type == "approval_resolved":
        policy.update(
            delivery_mode="app_only",
            channel_name=channel_name or "approvals",
            send_to_discord=bool(prefs.get("approval_resolved_to_discord", False)),
        )
        if policy["send_to_discord"]:
            policy["delivery_mode"] = "discord_immediate"
    elif event_type in {"trade_opened", "trade_closed"}:
        pref_key = f"{event_type}_to_discord"
        policy.update(
            delivery_mode="discord_immediate",
            channel_name=channel_name or trade_channel,
            send_to_discord=bool(prefs.get(pref_key, True)),
            cooldown_seconds=300,
        )
    elif event_type == "trade_failed":
        policy.update(
            delivery_mode="discord_immediate",
            channel_name=channel_name or "alerts",
            send_to_discord=bool(prefs.get("trade_failed_to_discord", True)),
            cooldown_seconds=300,
        )
    elif event_type == "equity_anomaly":
        # A persistently-implausible live equity reading (fail-closed rejects).
        # One alert per hour while it persists; gated by the risk alerts pref.
        policy.update(
            delivery_mode="discord_immediate",
            channel_name=channel_name or "risk",
            send_to_discord=bool(prefs.get("risk_critical_to_discord", True)),
            cooldown_seconds=3600,
        )
    elif event_type == "trade_blocked":
        # A LIVE open refused by a risk gate (hard caps, portfolio budget,
        # go-live ceiling, fail-closed equity). The scan re-attempts while the
        # signal stays active, so the cooldown is 1h per dedupe key — one alert
        # per strategy per cause, not one per 5-minute scan.
        policy.update(
            delivery_mode="discord_immediate",
            channel_name=channel_name or "alerts",
            send_to_discord=bool(prefs.get("trade_blocked_to_discord", True)),
            cooldown_seconds=3600,
        )
    elif event_type == "agent_task_completed":
        send_to_discord = bool(prefs.get("agent_completion_to_discord", False)) or bool(
            metadata.get("needs_review") or metadata.get("approval_required")
        )
        policy.update(
            delivery_mode="discord_immediate" if send_to_discord else "app_only",
            channel_name=channel_name or "alerts",
            send_to_discord=send_to_discord,
            cooldown_seconds=60,
        )
    elif event_type == "agent_task_failed":
        policy.update(
            delivery_mode="discord_immediate",
            channel_name=channel_name or "ops",
            send_to_discord=bool(prefs.get("agent_failure_to_discord", True)),
            cooldown_seconds=300,
        )
    elif event_type == "pipeline_transition":
        send_to_discord = bool(prefs.get("pipeline_transition_to_discord", False))
        policy.update(
            delivery_mode="discord_immediate" if send_to_discord else "app_only",
            channel_name=channel_name or "strategies",
            send_to_discord=send_to_discord,
            cooldown_seconds=120,
        )
    elif event_type == "system_degraded":
        policy.update(
            delivery_mode="discord_immediate",
            channel_name=channel_name or "ops",
            send_to_discord=bool(prefs.get("system_degraded_to_discord", True)),
            cooldown_seconds=900,
        )
    elif event_type == "system_recovered":
        policy.update(
            delivery_mode="discord_immediate",
            channel_name=channel_name or "ops",
            send_to_discord=bool(prefs.get("system_recovered_to_discord", True)),
            cooldown_seconds=900,
        )
    elif event_type == "risk_critical":
        policy.update(
            delivery_mode="discord_immediate",
            channel_name=channel_name or "risk",
            send_to_discord=bool(prefs.get("risk_critical_to_discord", True)),
            cooldown_seconds=300,
        )
    elif event_type == "brain_response":
        send_to_discord = bool(prefs.get("brain_response_to_discord", True)) and bool(channel_id or channel_name)
        policy.update(
            delivery_mode="discord_immediate" if send_to_discord else "app_only",
            channel_name=channel_name or "chat",
            channel_id=channel_id,
            send_to_discord=send_to_discord,
            cooldown_seconds=0,
        )
    elif event_type in {"digest_ops", "digest_trading", "digest_daily", "digest_weekly"}:
        channel = channel_name or (
            "morning-brief" if event_type in {"digest_ops", "digest_daily", "digest_weekly"} else "evening-summary"
        )
        policy.update(
            delivery_mode="discord_thread",
            channel_name=channel,
            send_to_discord=bool(prefs.get("digests_to_discord", True)),
            cooldown_seconds=0,
        )
    elif event_type == "health_critical":
        # Critical component-down alerts are emitted under a "never suppress
        # entirely" invariant (health_monitor): always deliver, regardless of the
        # routine "Health reports" toggle (which only governs AMBER warnings).
        policy.update(
            delivery_mode="discord_immediate",
            channel_name=channel_name or "ops",
            send_to_discord=True,
            cooldown_seconds=900,
        )
    elif event_type == "health_warning":
        # Routine warnings ARE gated by the "Health reports" toggle so operators
        # can quiet chatter without losing the critical/recovery signals.
        policy.update(
            delivery_mode="discord_immediate",
            channel_name=channel_name or "ops",
            send_to_discord=bool(prefs.get("system_degraded_to_discord", True)),
            cooldown_seconds=900,
        )
    elif event_type == "health_recovery":
        policy.update(
            delivery_mode="discord_immediate",
            channel_name=channel_name or "ops",
            send_to_discord=bool(prefs.get("system_recovered_to_discord", True)),
            cooldown_seconds=900,
        )
    elif event_type == "overnight_summary":
        # The "Daily summary" toggle drives digests_to_discord; honor it here too
        # (overnight_summary previously fell through to app-only on its info severity).
        policy.update(
            delivery_mode="discord_thread",
            channel_name=channel_name or "evening-summary",
            send_to_discord=bool(prefs.get("digests_to_discord", True)),
            cooldown_seconds=0,
        )
    elif severity in {"warn", "fail", "critical"}:
        policy.update(
            delivery_mode="discord_immediate",
            channel_name=channel_name or "alerts",
            send_to_discord=True,
            cooldown_seconds=300,
        )

    if str(prefs.get("discord_mode") or "policy").strip().lower() == "shadow":
        policy["delivery_mode"] = "app_only"
        policy["send_to_discord"] = False
    elif str(prefs.get("discord_mode") or "policy").strip().lower() == "legacy":
        policy["send_to_discord"] = True
        if policy["delivery_mode"] == "app_only":
            policy["delivery_mode"] = "discord_immediate"

    return policy


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "y"}:
        return True
    if normalized in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _coerce_str_list(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        values = [str(item).strip() for item in value if str(item).strip()]
        return values or list(default)
    if isinstance(value, str):
        values = [item.strip() for item in value.split(",") if item.strip()]
        return values or list(default)
    return list(default)
