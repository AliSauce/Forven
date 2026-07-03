"""LIQ-1: the order-time liquidity guard on live opens.

The forced-buyer / exit-liquidity threat: an adversarial strategy signal
marches the bot into a thin book. The guard sits inside market_order (the one
chokepoint every live open passes through), measures the MAINNET book, and
fails closed when market data is unavailable. Closes never route through it.
"""

from __future__ import annotations

import pytest

from forven.db import kv_set
from forven.exchange import liquidity


# A healthy book around mid=100: ~$2k resting within 100bps each side.
HEALTHY_BIDS = [{"px": 99.95, "sz": 10.0}, {"px": 99.5, "sz": 10.0}, {"px": 98.0, "sz": 50.0}]
HEALTHY_ASKS = [{"px": 100.05, "sz": 10.0}, {"px": 100.5, "sz": 10.0}, {"px": 102.0, "sz": 50.0}]
HEALTHY_CTX = {"dayNtlVlm": "25000000", "markPx": "100.0"}


@pytest.fixture
def healthy_market(monkeypatch):
    monkeypatch.setattr(liquidity, "fetch_asset_ctx", lambda asset: dict(HEALTHY_CTX))
    monkeypatch.setattr(
        liquidity, "fetch_l2_book",
        lambda asset: ([dict(l) for l in HEALTHY_BIDS], [dict(l) for l in HEALTHY_ASKS]),
    )


# ---------------------------------------------------------------- admission checks


def test_healthy_market_small_order_passes(forven_db, healthy_market):
    ok, why = liquidity.check_order_liquidity("BTC", True, 1.0, 100.0)  # $100 order
    assert ok and "liquidity OK" in why


def test_guard_disabled_allows(forven_db):
    kv_set("forven:settings", {"live_liquidity_guard_enabled": False})
    ok, why = liquidity.check_order_liquidity("SCAMCOIN", True, 1e9, 100.0)
    assert ok and "disabled" in why


def test_volume_floor_blocks_thin_asset(forven_db, monkeypatch):
    monkeypatch.setattr(liquidity, "fetch_asset_ctx", lambda asset: {"dayNtlVlm": "800000"})
    ok, why = liquidity.check_order_liquidity("THIN", True, 1.0, 100.0)
    assert not ok and "24h volume" in why and "floor" in why


def test_missing_market_context_fails_closed(forven_db, monkeypatch):
    monkeypatch.setattr(liquidity, "fetch_asset_ctx", lambda asset: None)
    ok, why = liquidity.check_order_liquidity("GHOST", True, 1.0, 100.0)
    assert not ok and "fail closed" in why


def test_missing_book_fails_closed(forven_db, monkeypatch):
    monkeypatch.setattr(liquidity, "fetch_asset_ctx", lambda asset: dict(HEALTHY_CTX))
    monkeypatch.setattr(liquidity, "fetch_l2_book", lambda asset: None)
    ok, why = liquidity.check_order_liquidity("BTC", True, 1.0, 100.0)
    assert not ok and "order book" in why


def test_wide_spread_blocks(forven_db, monkeypatch):
    monkeypatch.setattr(liquidity, "fetch_asset_ctx", lambda asset: dict(HEALTHY_CTX))
    # bid 99, ask 101 → ~200bps spread > 50bps default
    monkeypatch.setattr(
        liquidity, "fetch_l2_book",
        lambda asset: ([{"px": 99.0, "sz": 100.0}], [{"px": 101.0, "sz": 100.0}]),
    )
    ok, why = liquidity.check_order_liquidity("BTC", True, 1.0, 100.0)
    assert not ok and "spread" in why


def test_participation_cap_blocks_order_that_is_the_market(forven_db, healthy_market):
    # near-mid ask depth (within 100bps of ~100 mid) is 10+10 = $2005;
    # 25% default cap → a ~$600 buy (6 units) is over it
    ok, why = liquidity.check_order_liquidity("BTC", True, 6.0, 100.0)
    assert not ok and "resting within" in why


def test_impact_cap_blocks_book_walking_order(forven_db, monkeypatch):
    monkeypatch.setattr(liquidity, "fetch_asset_ctx", lambda asset: dict(HEALTHY_CTX))
    # plenty of depth WITHIN the 100bps window (participation passes: $3k order vs
    # ~$51k near-mid), but most of it sits at the window's far edge — filling 30
    # units walks to 100.95 → vwap ~100.8 → ~80bps impact > 50bps default
    monkeypatch.setattr(
        liquidity, "fetch_l2_book",
        lambda asset: (
            [{"px": 99.95, "sz": 500.0}],
            [{"px": 100.05, "sz": 5.0}, {"px": 100.95, "sz": 500.0}],
        ),
    )
    ok, why = liquidity.check_order_liquidity("BTC", True, 30.0, 100.0)
    assert not ok and "price impact" in why


def test_unfillable_order_blocks(forven_db, monkeypatch):
    # disarm the participation cap so the walk-the-book branch is what fires
    kv_set("forven:settings", {"live_max_book_participation_pct": 100000.0})
    monkeypatch.setattr(liquidity, "fetch_asset_ctx", lambda asset: dict(HEALTHY_CTX))
    monkeypatch.setattr(
        liquidity, "fetch_l2_book",
        lambda asset: ([{"px": 99.95, "sz": 5000.0}], [{"px": 100.05, "sz": 2.0}]),
    )
    ok, why = liquidity.check_order_liquidity("BTC", True, 10.0, 100.0)
    assert not ok and "cannot absorb" in why


def test_sell_side_uses_bids(forven_db, monkeypatch):
    monkeypatch.setattr(liquidity, "fetch_asset_ctx", lambda asset: dict(HEALTHY_CTX))
    # tight spread, deep asks, near-empty bids: a BUY passes, a SELL of the same
    # size blocks — proving the guard measures the TAKER side of the book
    monkeypatch.setattr(
        liquidity, "fetch_l2_book",
        lambda asset: (
            [{"px": 99.95, "sz": 0.1}, {"px": 90.0, "sz": 1000.0}],
            [{"px": 100.05, "sz": 1000.0}],
        ),
    )
    ok, why = liquidity.check_order_liquidity("BTC", True, 1.0, 100.0)
    assert ok, why
    ok, why = liquidity.check_order_liquidity("BTC", False, 1.0, 100.0)
    assert not ok and "resting within" in why


def test_thresholds_editable_via_settings(forven_db, healthy_market):
    kv_set("forven:settings", {"live_max_book_participation_pct": 90.0, "live_max_price_impact_bps": 500.0})
    # the $600 order blocked at 25% participation now passes at 90%
    ok, why = liquidity.check_order_liquidity("BTC", True, 6.0, 100.0)
    assert ok, why


def test_settings_section_persists_liquidity_keys(forven_db):
    from forven import api_core
    from forven.db import kv_get
    api_core.put_settings_section("risk", {
        "live_liquidity_guard_enabled": False,
        "live_min_daily_volume_usd": 250000,
        "live_max_spread_bps": 80,
        "live_book_depth_window_bps": 200,
        "live_max_book_participation_pct": 10,
        "live_max_price_impact_bps": 30,
    })
    s = kv_get("forven:settings", {}) or {}
    assert s.get("live_liquidity_guard_enabled") is False
    assert s.get("live_min_daily_volume_usd") == 250000
    assert s.get("live_max_spread_bps") == 80
    assert s.get("live_book_depth_window_bps") == 200
    assert s.get("live_max_book_participation_pct") == 10
    assert s.get("live_max_price_impact_bps") == 30


# ---------------------------------------------------------------- snapshot


def test_snapshot_shape_and_decision_trail(forven_db, healthy_market):
    liquidity.check_order_liquidity("BTC", True, 1.0, 100.0)
    snap = liquidity.liquidity_guard_snapshot()
    assert snap["enabled"] is True
    assert set(snap["limits"]) == {
        "live_min_daily_volume_usd", "live_max_spread_bps", "live_book_depth_window_bps",
        "live_max_book_participation_pct", "live_max_price_impact_bps",
    }
    latest = snap["recent_decisions"][0]
    assert latest["asset"] == "BTC" and latest["allowed"] is True
    # and it rides the /api/risk payload
    from forven.exchange import risk
    status = risk.get_risk_status()
    assert status["liquidity_guard_live"]["enabled"] is True


# ---------------------------------------------------------------- market_order chokepoint


def test_market_order_refuses_blocked_open(forven_db, monkeypatch):
    """The guard is enforced INSIDE market_order — a block returns an error
    payload before any exchange submit happens."""
    from forven.exchange import hyperliquid as hl

    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(hl, "_exchange_for_trading", lambda testnet, vault_address=None: (object(), None, "0x0"))
    monkeypatch.setattr(hl, "_resolve_exchange_url", lambda exchange: None)
    monkeypatch.setattr(hl, "quantize_size", lambda asset, size, url: size)
    monkeypatch.setattr(hl, "get_all_mids", lambda testnet: {"BTC": 100.0})
    monkeypatch.setattr(
        liquidity, "check_order_liquidity",
        lambda asset, is_buy, size, mid: (False, "liquidity guard: test block"),
    )

    def _no_submit(*a, **k):
        raise AssertionError("order must not reach the exchange when the guard blocks")

    monkeypatch.setattr(hl, "_submit", _no_submit)

    result = hl.market_order("BTC", "buy", 1.0, stop_loss_price=95.0, testnet=True)
    assert result.get("liquidity_blocked") is True
    assert "test block" in str(result.get("error"))
