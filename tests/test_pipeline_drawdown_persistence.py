"""Regression: an explicit quick-screen max-drawdown edit must persist.

The Settings save round-trips the WHOLE pipeline config, including the
``paper_gate`` alias that ``_normalize_pipeline_config`` republishes on every save.
The legacy ``paper_gate -> quick_screen`` back-mapping used to run unconditionally,
so a user who raised ``quick_screen.max_drawdown_pct`` to 0.50 had it silently
reverted to the stale alias's 0.30 on the very next load. The gauntlet gate has no
such alias, which is why only quick-screen reverted (gauntlet 0.50 stuck fine).
"""
from __future__ import annotations


def test_explicit_quick_screen_drawdown_survives_stale_paper_gate_alias(forven_db):
    from forven.policy import load_pipeline_config, save_pipeline_config

    cfg = load_pipeline_config()
    # User raises the quick-screen drawdown ceiling to 50% (crypto-appropriate).
    cfg["quick_screen"]["max_drawdown_pct"] = 0.50
    # The UI round-trips the republished alias, which still carries the OLD value.
    cfg.setdefault("paper_gate", {})["max_drawdown_pct"] = 0.30
    save_pipeline_config(cfg)

    reloaded = load_pipeline_config()
    assert reloaded["quick_screen"]["max_drawdown_pct"] == 0.50  # not reverted to 0.30
    # Idempotent across a second load (the self-heal write must not flip it back).
    assert load_pipeline_config()["quick_screen"]["max_drawdown_pct"] == 0.50


def test_gauntlet_drawdown_edit_persists(forven_db):
    from forven.policy import load_pipeline_config, save_pipeline_config

    cfg = load_pipeline_config()
    cfg["gauntlet"]["max_drawdown_pct"] = 0.50
    save_pipeline_config(cfg)
    assert load_pipeline_config()["gauntlet"]["max_drawdown_pct"] == 0.50


def test_legacy_paper_gate_still_back_maps_without_explicit_quick_screen(forven_db):
    """Back-compat: a genuinely legacy payload (paper_gate only, no quick_screen
    knobs) must still map paper_gate.max_drawdown_pct onto the modern gate."""
    from forven.db import kv_set
    from forven.policy import load_pipeline_config

    kv_set("forven:pipeline_thresholds", {"paper_gate": {"max_drawdown_pct": 0.22}})
    cfg = load_pipeline_config()
    assert cfg["quick_screen"]["max_drawdown_pct"] == 0.22
