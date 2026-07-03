"""CORR-1: measured-correlation effective exposure for the live budget.

Covers the correlation math on synthetic stored series, the directional
aggregation (aligned adds, opposing offsets, uncorrelated ignores), the
conservative missing-data fallback, and the budget integration."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

import forven.portfolio_correlation as pc
from forven.db import get_db, kv_set
from forven.exchange import risk


@pytest.fixture(autouse=True)
def _fresh_caches():
    pc.clear_correlation_caches()
    yield
    pc.clear_correlation_caches()


def _frame(closes: np.ndarray) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame({"close": closes}, index=idx)


def _install_lake(monkeypatch, series_by_asset: dict[str, np.ndarray]):
    def _fake_load_parquet(symbol, timeframe, *, as_of=None):
        base = str(symbol).split("/")[0].upper()
        closes = series_by_asset.get(base)
        return _frame(closes) if closes is not None else None

    import forven.data as data_mod

    monkeypatch.setattr(data_mod, "load_parquet", _fake_load_parquet)


def _random_walk(rng, n=800, drift=0.0):
    returns = rng.normal(drift, 0.01, n)
    return 100.0 * np.exp(np.cumsum(returns))


# ---------------------------------------------------------------- correlation math


def test_pair_correlation_detects_aligned_inverted_independent(forven_db, monkeypatch):
    rng = np.random.default_rng(7)
    base_returns = rng.normal(0, 0.01, 800)
    other_returns = rng.normal(0, 0.01, 800)
    series = {
        "BTC": 100.0 * np.exp(np.cumsum(base_returns)),
        "ETH": 50.0 * np.exp(np.cumsum(base_returns)),        # identical returns
        "INV": 80.0 * np.exp(np.cumsum(-base_returns)),       # inverted
        "IND": 60.0 * np.exp(np.cumsum(other_returns)),       # independent
    }
    _install_lake(monkeypatch, series)

    assert pc.pair_correlation("BTC", "ETH") == pytest.approx(1.0, abs=1e-6)
    assert pc.pair_correlation("BTC", "INV") == pytest.approx(-1.0, abs=1e-6)
    assert abs(pc.pair_correlation("BTC", "IND")) < 0.2
    assert pc.pair_correlation("BTC", "BTC") == 1.0


def test_pair_correlation_missing_or_short_series_is_none(forven_db, monkeypatch):
    rng = np.random.default_rng(3)
    _install_lake(monkeypatch, {
        "BTC": _random_walk(rng),
        "SHORTY": _random_walk(rng, n=10),  # below the overlap floor
    })
    assert pc.pair_correlation("BTC", "NODATA") is None
    assert pc.pair_correlation("BTC", "SHORTY") is None


def test_pair_correlation_uses_cache_within_ttl(forven_db, monkeypatch):
    rng = np.random.default_rng(11)
    loads = {"n": 0}

    def _counting_load(symbol, timeframe, *, as_of=None):
        loads["n"] += 1
        return _frame(_random_walk(rng))

    import forven.data as data_mod

    monkeypatch.setattr(data_mod, "load_parquet", _counting_load)
    first = pc.pair_correlation("BTC", "ETH")
    loads_after_first = loads["n"]
    second = pc.pair_correlation("ETH", "BTC")  # symmetric key
    assert second == first
    assert loads["n"] == loads_after_first  # no re-load inside the TTL


# ---------------------------------------------------------------- effective exposure


def _positions(*rows):
    return [
        {"asset": a, "direction": d, "notional_usd": n}
        for a, d, n in rows
    ]


def test_correlated_longs_stack_and_block(monkeypatch):
    monkeypatch.setattr(pc, "pair_correlation", lambda a, b, w=720: 1.0)
    settings = {"live_max_effective_exposure_pct": 200.0}
    # $1000 equity → cap $2000. Existing $1500 correlated long + $800 more = $2300.
    ok, reason = pc.check_effective_correlated_exposure(
        "SOL", "long", 800.0, _positions(("ETH", "long", 1500.0)), 1000.0, settings,
    )
    assert not ok
    assert "one bet" in reason
    # The same open with the static-group-invisible pair UNDER the cap passes.
    ok, reason = pc.check_effective_correlated_exposure(
        "SOL", "long", 400.0, _positions(("ETH", "long", 1500.0)), 1000.0, settings,
    )
    assert ok, reason


def test_correlated_short_offsets(monkeypatch):
    monkeypatch.setattr(pc, "pair_correlation", lambda a, b, w=720: 1.0)
    settings = {"live_max_effective_exposure_pct": 200.0}
    # A correlated SHORT hedges the long book: |1800 - 1500| = 300 << 2000.
    ok, reason = pc.check_effective_correlated_exposure(
        "SOL", "long", 1800.0, _positions(("ETH", "short", 1500.0)), 1000.0, settings,
    )
    assert ok, reason


def test_uncorrelated_positions_do_not_stack(monkeypatch):
    monkeypatch.setattr(pc, "pair_correlation", lambda a, b, w=720: 0.0)
    settings = {"live_max_effective_exposure_pct": 200.0}
    ok, reason = pc.check_effective_correlated_exposure(
        "SOL", "long", 1900.0, _positions(("XAU", "long", 1900.0)), 1000.0, settings,
    )
    assert ok, reason  # each ~190% alone; corr 0 → they don't combine


def test_missing_correlation_falls_back_conservative(monkeypatch):
    monkeypatch.setattr(pc, "pair_correlation", lambda a, b, w=720: None)
    settings = {"live_max_effective_exposure_pct": 200.0}
    ok, reason = pc.check_effective_correlated_exposure(
        "SOL", "long", 800.0, _positions(("ETH", "long", 1500.0)), 1000.0, settings,
    )
    assert not ok  # unmeasurable pair treated as corr 1.0 → blocked
    # Operator can relax the fallback (full editability) — corr 0 → passes.
    settings["live_correlation_missing_default"] = 0.0
    ok, reason = pc.check_effective_correlated_exposure(
        "SOL", "long", 800.0, _positions(("ETH", "long", 1500.0)), 1000.0, settings,
    )
    assert ok, reason


def test_disabled_and_failclosed_paths(monkeypatch):
    ok, reason = pc.check_effective_correlated_exposure(
        "SOL", "long", 800.0, [], 1000.0, {"live_correlation_budget_enabled": False},
    )
    assert ok and "disabled" in reason
    ok, reason = pc.check_effective_correlated_exposure(
        "SOL", "long", 800.0, [], 0.0, {},
    )
    assert not ok and "fail closed" in reason


# ---------------------------------------------------------------- budget integration


def _insert_open(conn, trade_id, asset, direction, entry, size, stop):
    sid = f"S-{trade_id}"
    conn.execute(
        "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, "
        "status, execution_type, signal_data, opened_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', 'live', ?, '2026-01-01T00:00:00+00:00')",
        (trade_id, sid, sid, asset, direction, entry, size, json.dumps({"stop_loss_price": stop})),
    )


def test_budget_blocks_on_effective_exposure(forven_db, monkeypatch):
    kv_set("daemon_state", {"account_equity": 1000.0})
    with get_db() as conn:
        _insert_open(conn, "L1", "ETH", "long", 100.0, 15.0, stop=99.0)  # $1500 notional, tiny risk
    monkeypatch.setattr(pc, "pair_correlation", lambda a, b, w=720: 1.0)

    # $800 more of a perfectly correlated asset → effective $2300 > $2000 cap.
    # Static checks all pass (SOL alone is $800 = 80% asset; group SOL≠crypto_major
    # bucket contains SOL... use an out-of-group asset to isolate CORR-1).
    ok, reason = risk.check_live_portfolio_budget(
        "DOGE", "long", add_risk_usd=8.0, add_notional_usd=800.0,
    )
    assert not ok
    assert "correlation budget" in reason

    # Same order with the correlation gate disabled sails through the static checks.
    kv_set("forven:settings", {"live_correlation_budget_enabled": False})
    ok, reason = risk.check_live_portfolio_budget(
        "DOGE", "long", add_risk_usd=8.0, add_notional_usd=800.0,
    )
    assert ok, reason


def test_budget_snapshot_reports_held_pair_correlations(forven_db, monkeypatch):
    kv_set("daemon_state", {"account_equity": 1000.0})
    with get_db() as conn:
        _insert_open(conn, "L1", "ETH", "long", 100.0, 5.0, stop=99.0)
        _insert_open(conn, "L2", "SOL", "long", 50.0, 4.0, stop=49.0)
    monkeypatch.setattr(pc, "pair_correlation", lambda a, b, w=720: 0.83)

    snap = risk.live_portfolio_budget_snapshot()
    assert snap["held_pair_correlations"] == {"ETH|SOL": 0.83}
    assert snap["effective_exposure_limit_usd"] == pytest.approx(2000.0)
    assert snap["limits_pct"]["live_max_effective_exposure_pct"] == pytest.approx(200.0)