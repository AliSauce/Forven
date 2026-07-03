"""Discord channel alias map — shared by the bot gateway and the API.

Lives outside ``forven/bot.py`` so API code (e.g. the /routines channel
dropdown) can resolve aliases without importing the discord package.
Defaults are overridden by config.json ``discord_channels``.
"""
from __future__ import annotations

from forven.config import load_config

DEFAULT_CHANNELS = {
    "general": "1472929176213393505",
    "ops": "1473714175300603924",
    "approvals": "1473006244171354123",
    "risk": "1473006244171354123",
    "morning-brief": "1473323213143539868",
    "evening-brief": "1473323214083199093",
    "evening-summary": "1473323214083199093",
    "chat": "1473412370528338003",
    "heartbeat": "1473654720735481947",
    "development": "1473714175300603924",
    "strategies": "1473006243147808829",
    "alerts": "1473006244171354123",
    "research": "1473006245211275304",
    "backtesting": "1473036255716577420",
    "paper-trades": "1473036257625112808",
    "market-data": "1473036258962968842",
    "autopilot": "1473036260103815351",
    "news": "1473036261345202340",
    "full-stack-engineer": "1474937376928301169",
    # Backward-compatible aliases that collapse old room-specific names onto the
    # reduced notification/channel model.
    "quant-researcher": "1473006245211275304",
    "back-test-engineer": "1473036255716577420",
    "risk-manager": "1473006244171354123",
    "sentiment": "1473036261345202340",
    "full-stack-engineers": "1473714175300603924",
}


def load_channel_map() -> dict[str, str]:
    """Alias -> channel-id map with config.json overrides applied."""
    cfg = load_config()
    overrides = cfg.get("discord_channels", {}) or {}
    return {**DEFAULT_CHANNELS, **overrides}


def channel_aliases() -> list[str]:
    """Sorted alias names for UI dropdowns (aliases may share a channel id)."""
    return sorted(load_channel_map().keys())


# The bot gateway publishes the text channels it can actually post to (per
# guild it is connected to) under this kv key on connect and on channel
# create/delete/rename. This is what makes the routines channel picker work
# on ANY user's Discord server — the alias map above is only a fallback for
# setups where the bot has never connected.
AVAILABLE_CHANNELS_KV_KEY = "discord:available_channels"


def available_channels() -> list[dict[str, str]] | None:
    """Live guild text-channel list published by the bot, or None if the bot
    has never published (callers fall back to the static alias map)."""
    try:
        from forven.db import kv_get

        data = kv_get(AVAILABLE_CHANNELS_KV_KEY)
        raw = data.get("channels") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return None
        out: list[dict[str, str]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            channel_id = str(entry.get("id") or "").strip()
            name = str(entry.get("name") or "").strip()
            if channel_id and name:
                out.append({"id": channel_id, "name": name})
        return out or None
    except Exception:
        return None
