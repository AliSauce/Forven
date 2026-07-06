"""GO-LIVE-1 hygiene: terminal strategies must not stay live-armed."""

import pytest

from forven.db import get_db, init_db


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FORVEN_HOME", str(tmp_path))
    import forven.config as config_module

    monkeypatch.setattr(config_module, "FORVEN_HOME", tmp_path)
    monkeypatch.setattr(config_module, "FORVEN_DB", tmp_path / "forven.db")
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.json")
    import forven.db as db_module

    monkeypatch.setattr(db_module, "FORVEN_DB", tmp_path / "forven.db")
    init_db()
    yield


def _insert_strategy(sid: str, stage: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status)"
            " VALUES (?, ?, 'ema_cross', 'BTC', '1h', '{}', ?, ?)",
            (sid, sid, stage, stage),
        )


def _seed_ceilings():
    from forven.exchange.risk import set_live_notional_ceiling

    set_live_notional_ceiling("S_LIVE", 500.0, asset="BTC", actor="test")
    set_live_notional_ceiling("S_PAPER", 100.0, actor="test")
    set_live_notional_ceiling("S_DEAD", 200.0, asset="ETH", actor="test")
    set_live_notional_ceiling("S_GONE", 150.0, actor="test")  # no strategies row
    set_live_notional_ceiling("bot:B1", 50.0, actor="test")  # bot key — never reaped


def test_reaper_revokes_terminal_and_missing_only():
    from forven.exchange.risk import get_live_notional_ceilings, revoke_dead_strategy_ceilings

    _insert_strategy("S_LIVE", "live_graduated")
    _insert_strategy("S_PAPER", "paper")
    _insert_strategy("S_DEAD", "archived")
    _seed_ceilings()

    revoked = revoke_dead_strategy_ceilings()

    assert sorted(revoked) == ["S_DEAD", "S_GONE"]
    remaining = set(get_live_notional_ceilings())
    assert remaining == {"S_LIVE", "S_PAPER", "bot:B1"}
    # idempotent
    assert revoke_dead_strategy_ceilings() == []


def test_snapshot_annotates_stage_and_hides_zombies():
    from forven.exchange.risk import live_portfolio_budget_snapshot

    _insert_strategy("S_LIVE", "live_graduated")
    _insert_strategy("S_PAPER", "paper")
    _insert_strategy("S_DEAD", "rejected")
    _seed_ceilings()

    ceilings = live_portfolio_budget_snapshot()["strategy_ceilings"]
    assert set(ceilings) == {"S_LIVE", "S_PAPER", "bot:B1"}
    assert ceilings["S_LIVE"]["stage"] == "live_graduated"
    assert ceilings["S_PAPER"]["stage"] == "paper"
    assert ceilings["bot:B1"]["stage"] == "bot"


def test_maintenance_reaps_stale_ceilings():
    from forven.exchange.risk import get_live_notional_ceilings
    from forven.maintenance import run_db_maintenance

    _insert_strategy("S_LIVE", "live_graduated")
    _insert_strategy("S_PAPER", "paper")
    _insert_strategy("S_DEAD", "archived")
    _seed_ceilings()

    summary = run_db_maintenance()

    assert summary["stale_live_ceilings_revoked"] == 2  # S_DEAD + S_GONE
    assert "S_DEAD" not in get_live_notional_ceilings()
