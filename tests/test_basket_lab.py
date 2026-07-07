"""Basket engine honesty checks: funding sign, neutrality, fill timing, costs."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forven.basket_lab import (
    BasketPanel,
    FundingCarryBasket,
    run_basket,
    run_placebo,
)


def _flat_panel(
    n_bars: int = 200,
    fundings: dict[str, float] | None = None,
) -> BasketPanel:
    """Constant prices (no price PnL) with constant per-hour funding rates."""
    fundings = fundings or {"AAA": 0.001, "BBB": -0.001, "CCC": 0.0, "DDD": 0.0}
    idx = pd.date_range("2025-01-01", periods=n_bars, freq="1h", tz="UTC")
    price = pd.DataFrame(100.0, index=idx, columns=list(fundings))
    funding = pd.DataFrame({s: [r] * n_bars for s, r in fundings.items()}, index=idx)
    return BasketPanel(
        index=idx, open=price.copy(), close=price.copy(), funding=funding, bar_hours=1.0
    )


def _carry(n_legs: int = 1, rebalance_hours: int = 8) -> FundingCarryBasket:
    strat = FundingCarryBasket()
    strat.n_legs = n_legs
    strat.rebalance_hours = rebalance_hours
    return strat


def test_funding_sign_short_positive_funding_earns():
    """Short the +funding perp, long the -funding perp -> both legs accrue."""
    panel = _flat_panel()
    result = run_basket(panel, _carry(), fee_bps=0.0, slippage_bps=0.0, min_history_bars=10)
    m = result.metrics
    assert m["price_pnl_sum"] == pytest.approx(0.0, abs=1e-12)
    # 0.5 short at +0.001/h earns 0.0005/bar; 0.5 long at -0.001/h earns 0.0005/bar.
    active_bars = (result.weights.abs().sum(axis=1) > 0).sum() - 1  # last bar accrues nothing
    assert m["funding_pnl_sum"] == pytest.approx(0.001 * active_bars, rel=1e-6)
    assert result.equity.iloc[-1] > 1.0


def test_dollar_neutral_and_gross_leverage():
    panel = _flat_panel()
    result = run_basket(panel, _carry(n_legs=2), fee_bps=0.0, slippage_bps=0.0, min_history_bars=10)
    w = result.weights
    active = w[w.abs().sum(axis=1) > 0]
    assert not active.empty
    assert np.allclose(active.sum(axis=1), 0.0)  # net zero every bar
    assert np.allclose(active.abs().sum(axis=1), 1.0)  # gross = gross_leverage


def test_no_lookahead_fill_is_next_bar():
    """A decision at bar b's close must not be in force during bar b."""
    panel = _flat_panel(n_bars=64)
    result = run_basket(panel, _carry(rebalance_hours=8), fee_bps=0.0, slippage_bps=0.0, min_history_bars=16)
    w = result.weights
    first_active = int((w.abs().sum(axis=1) > 0).values.argmax())
    # min_history=16 -> first eligible decision is the rebalance at b=16 (bars
    # 0..16 seen = 17 >= 16); the weights must take effect at b=17, not b=16.
    assert first_active % 8 == 1, "fill must land one bar AFTER a rebalance decision"
    assert w.iloc[first_active - 1].abs().sum() == 0.0


def test_turnover_cost_charged_on_fills():
    panel = _flat_panel()
    free = run_basket(panel, _carry(), fee_bps=0.0, slippage_bps=0.0, min_history_bars=10)
    paid = run_basket(panel, _carry(), fee_bps=4.5, slippage_bps=2.0, min_history_bars=10)
    assert paid.metrics["cost_sum"] > 0
    # Constant ranks -> one initial fill of gross 1.0, no further turnover.
    assert paid.metrics["cost_sum"] == pytest.approx(1.0 * 6.5 / 10_000.0, rel=1e-9)
    assert paid.equity.iloc[-1] < free.equity.iloc[-1]


def test_placebo_shuffles_ranks_but_keeps_costs():
    panel = _flat_panel(n_bars=400)
    placebos = run_placebo(panel, _carry(), n_runs=5, fee_bps=4.5, slippage_bps=2.0, min_history_bars=10)
    assert len(placebos) == 5
    # Shuffled ranks on a 4-symbol panel must not all replicate the true carry.
    real = run_basket(panel, _carry(), fee_bps=4.5, slippage_bps=2.0, min_history_bars=10)
    assert any(p["funding_pnl_sum"] < real.metrics["funding_pnl_sum"] for p in placebos)


def test_nan_prices_are_ineligible():
    """A symbol with NaN close at decision time (delisted/not yet listed) is skipped."""
    panel = _flat_panel()
    panel.close.loc[panel.index[:100], "BBB"] = np.nan
    panel.open.loc[panel.index[:100], "BBB"] = np.nan
    result = run_basket(panel, _carry(), fee_bps=0.0, slippage_bps=0.0, min_history_bars=10)
    w = result.weights
    assert w.loc[panel.index[:100], "BBB"].abs().sum() == 0.0


# ------------------------------------------------- funding interval normalization


def test_funding_interval_hours_from_observed_grid(tmp_path, monkeypatch):
    # Binance stores the per-SETTLEMENT rate on the settlement grid (8h for
    # most perps, 4h for some); the panel contract is per-hour. The interval
    # must come from the observed spacing, defaulting conservative to 8h.
    import forven.basket_lab as basket_lab
    import forven.data_manager as data_manager

    monkeypatch.setattr(data_manager, "FUNDING_DIR", tmp_path)

    def _write(sym, freq, n=30):
        d = tmp_path / sym
        d.mkdir()
        idx = pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC")
        pd.DataFrame({"timestamp": idx, "funding_rate": [0.0001] * n}).to_parquet(d / "history.parquet")

    _write("AAA-USDT", "8h")
    _write("BBB-USDT", "4h")
    assert basket_lab._funding_interval_hours("AAA-USDT") == 8.0
    assert basket_lab._funding_interval_hours("BBB-USDT") == 4.0
    assert basket_lab._funding_interval_hours("ZZZ-USDT") == 8.0  # no history -> default


def test_per_hour_funding_expires_after_final_print(tmp_path, monkeypatch):
    """A dead feed must go NaN one interval (+1h grace) past its final print —
    unbounded forward-fill let a delisted symbol's weeks-old rate rank as
    current forever (2026-07-07: TON-USDT, last print two weeks stale), and
    the runtime's 9h staleness mask measures this matrix, so it never saw it.
    Interior prints still fill normally up to the next print."""
    import forven.basket_lab as basket_lab
    import forven.data_manager as data_manager

    monkeypatch.setattr(data_manager, "FUNDING_DIR", tmp_path)
    d = tmp_path / "AAA-USDT"
    d.mkdir()
    prints = pd.date_range("2026-01-01", periods=4, freq="8h", tz="UTC")  # last: 01-02 00:00
    pd.DataFrame({"timestamp": prints, "funding_rate": [0.0008] * 4}).to_parquet(
        d / "history.parquet"
    )
    # Panel keeps ticking hourly for 3 more days after the feed dies.
    index = pd.date_range("2026-01-01", "2026-01-05", freq="1h", tz="UTC")
    series = basket_lab._per_hour_funding_series("AAA-USDT", index)

    # Backed bars: per-print conversion (0.0008 / 8h) up to expiry.
    assert series.loc["2026-01-01T12:00:00Z"] == pytest.approx(0.0001)
    # Still current through final print + 8h interval + 1h grace...
    assert series.loc["2026-01-02T09:00:00Z"] == pytest.approx(0.0001)
    # ...and NaN after the final print expires.
    assert pd.isna(series.loc["2026-01-02T10:00:00Z"])
    assert series.loc["2026-01-02T10:00:00Z":].isna().all()
