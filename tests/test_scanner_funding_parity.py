"""Scanner-side funding parity: paper/live kernel accrues funding IN-WALK like the backtest.

Before this change the scanner ran the shared kernel PRICE-ONLY (``include_funding=False``)
and applied perp funding post-hoc via ``_apply_funding_to_trades``. That kept a faithful
trade's closed PnL correct, but the kelly-sizing evidence the kernel produces
(``res.closed_gross``) was price-only — so a kelly-mode strategy sized funding-blind in
paper while the backtest (v5) sizes funding-aware. That is a backtest<->paper parity gap.

The scanner now passes ``include_funding=_paper_include_funding_enabled()`` into the SAME
``run_strategy_execution`` the backtest uses, so:

  * ``res.closed_gross`` (kelly evidence) carries funding — paper kelly == backtest kelly.
  * each in-walk-funded trade is stamped ``_funding_from_kernel`` and the scanner's post-hoc
    ``_apply_funding_to_trades`` SKIPS it (single-application invariant — no double funding).
  * the resulting net PnL per trade is IDENTICAL to the pre-change price-only-kernel +
    post-hoc path (same rates, same window → same drag).

These tests exercise the scanner's exact kernel invocation (``run_strategy_execution``) and
the scanner's own wiring (``manage_positions_via_kernel`` passing the flag), mirroring
tests/test_funding_kelly_single_application.py for the backtest side.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import forven.scanner as scanner
from forven.strategies import backtest as bt
from forven.strategies import execution_kernel as ek
from forven.strategies.builtin.rsi_momentum import RSIMomentumStrategy

LEV = 2.0
WARMUP = 200


def _frame(n: int = 420, seed: int = 4, funding_rate: float | None = 0.001) -> pd.DataFrame:
    """Trending OHLCV frame (RSI momentum trades on it) with an optional constant
    per-bar ``funding_rate`` column — the same shape the scanner enriches for perps."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0005, 0.02, size=n).cumsum()
    close = 100.0 * np.exp(steps)
    spread = np.abs(rng.normal(0.0, 0.012, size=n)) + 0.004
    high = close * (1.0 + spread)
    low = close * (1.0 - spread)
    openp = np.empty(n)
    openp[0] = close[0]
    openp[1:] = close[:-1] * (1.0 + rng.normal(0.0, 0.004, size=n - 1))
    high = np.maximum.reduce([high, openp, close])
    low = np.minimum.reduce([low, openp, close])
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    data = {"open": openp, "high": high, "low": low, "close": close, "volume": 1000.0}
    if funding_rate is not None:
        data["funding_rate"] = float(funding_rate)
    return pd.DataFrame(data, index=idx)


_PARAMS = {
    "rsi_period": 14, "rsi_entry": 45, "rsi_exit": 55,
    "ema_fast": 10, "ema_slow": 30, "adx_period": 14, "adx_min": 0,
    "leverage": LEV,
    "execution_profile": {
        "sizing_mode": "fraction", "risk_per_trade": 0.01,
        "stop_loss_pct": 3.0, "take_profit_pct": 5.0,
    },
}


def _strategy() -> RSIMomentumStrategy:
    return RSIMomentumStrategy("PAPER-FUND-1", dict(_PARAMS, _asset="BTC"))


def _run(df: pd.DataFrame, include_funding: bool) -> ek.KernelResult:
    """The scanner's exact kernel invocation (regime_gate=False, fraction sizing)."""
    ec = bt.execution_controls_from_params(_PARAMS)
    return bt.run_strategy_execution(
        df, _strategy(), params=_PARAMS, warmup=WARMUP, leverage=LEV,
        fee_bps=4.5, slippage_bps=2.0, regime_gate=False,
        trade_mode="long_only", execution_controls=ec, initial_capital=10000.0,
        strategy_type="rsi_momentum", symbol="BTC/USDT", timeframe="1h",
        include_funding=include_funding,
    )


def test_scanner_kernel_funding_matches_price_only_plus_posthoc_per_trade():
    """PARITY (fraction sizing): the scanner's funding-aware kernel run yields the SAME
    funding drag and (modulo 5dp display rounding) the same net PnL per trade as the
    pre-change path (price-only kernel + scanner post-hoc _apply_funding_to_trades).

    The funding term is bit-identical (same rates, same held-bar window, same size). The
    net ``pnl_pct`` can differ by at most one unit in the 5th decimal: the post-hoc path
    rounds price-PnL to 5dp and THEN adds funding (a second rounding), while the in-walk
    path folds funding into gross and rounds once — a pre-existing display artifact of the
    5dp rounding in finalize/_apply_funding_to_trades, not a semantic divergence. The
    bit-exact case is covered by test_...full_size below (size_fraction == 1.0)."""
    df = _frame()

    # (A) pre-change scanner path: price-only kernel, THEN scanner's post-hoc funding pass.
    res_a = _run(df, include_funding=False)
    assert res_a.closed_trades, "no closed trades — test is vacuous"
    assert not any(t.get("_funding_from_kernel") for t in res_a.closed_trades)
    bt._apply_funding_to_trades(res_a.closed_trades, df, LEV, "1h")

    # (B) new scanner path: funding accrued IN-WALK; post-hoc pass then runs and must skip.
    res_b = _run(df, include_funding=True)
    assert res_b.closed_trades
    assert all(t.get("_funding_from_kernel") for t in res_b.closed_trades)
    bt._apply_funding_to_trades(res_b.closed_trades, df, LEV, "1h")  # scanner still calls this

    a = {t["entry_time"]: t for t in res_a.closed_trades}
    b = {t["entry_time"]: t for t in res_b.closed_trades}
    assert a.keys() == b.keys()
    for k in a:
        # Funding drag is applied ONCE and identically on both paths.
        assert b[k]["funding_cost_pct"] == a[k]["funding_cost_pct"], f"funding diverged at {k}"
        # Net PnL identical to within one 5dp rounding unit (see docstring).
        assert abs(b[k]["pnl_pct"] - a[k]["pnl_pct"]) <= 1e-5, f"net pnl diverged at {k}"


def test_scanner_kernel_funding_matches_price_only_plus_posthoc_full_size_bit_exact():
    """PARITY (full size, BIT-EXACT): at size_fraction == 1.0 there is no double-rounding,
    so the in-walk and post-hoc paths produce byte-identical net ``pnl_pct`` — the strongest
    single-application proof (mirrors the backtest test's full-mode assertion)."""
    df = _frame()
    ec = bt.execution_controls_from_params(
        dict(_PARAMS, execution_profile={"sizing_mode": "full", "stop_loss_pct": 50.0})
    )
    common = dict(
        params=_PARAMS, warmup=WARMUP, leverage=LEV, fee_bps=4.5, slippage_bps=2.0,
        regime_gate=False, trade_mode="long_only", execution_controls=ec,
        initial_capital=10000.0, strategy_type="rsi_momentum", symbol="BTC/USDT",
        timeframe="1h",
    )
    res_a = bt.run_strategy_execution(df, _strategy(), include_funding=False, **common)
    res_b = bt.run_strategy_execution(df, _strategy(), include_funding=True, **common)
    assert res_a.closed_trades, "no closed trades — test is vacuous"
    bt._apply_funding_to_trades(res_a.closed_trades, df, LEV, "1h")
    bt._apply_funding_to_trades(res_b.closed_trades, df, LEV, "1h")

    a = {t["entry_time"]: t for t in res_a.closed_trades}
    b = {t["entry_time"]: t for t in res_b.closed_trades}
    assert a.keys() == b.keys()
    for k in a:
        assert b[k]["pnl_pct"] == a[k]["pnl_pct"], f"net pnl diverged at {k}"
        assert b[k]["funding_cost_pct"] == a[k]["funding_cost_pct"], f"funding diverged at {k}"


def test_scanner_posthoc_pass_skips_kernel_funded_trades_single_application():
    """SINGLE APPLICATION on the scanner path: the exact post-hoc call the scanner makes
    (``_apply_funding_to_trades`` on res.closed_trades) must be a PnL no-op for trades the
    kernel already funded in-walk — never double-funded."""
    df = _frame()
    res = _run(df, include_funding=True)
    assert res.closed_trades and all(t.get("_funding_from_kernel") for t in res.closed_trades)
    pnl_before = [t["pnl_pct"] for t in res.closed_trades]
    funding_before = [t["funding_cost_pct"] for t in res.closed_trades]

    bt._apply_funding_to_trades(res.closed_trades, df, LEV, "1h")

    assert [t["pnl_pct"] for t in res.closed_trades] == pnl_before, "funding double-applied"
    assert [t["funding_cost_pct"] for t in res.closed_trades] == funding_before


def test_scanner_kernel_closed_gross_is_funding_aware():
    """KELLY EVIDENCE: with funding on, the scanner kernel's ``closed_gross`` (the kelly
    sizing input the pending/fill-now path feeds to sizing.size_fraction) carries the
    pre-size funding term — a long PAYS positive funding, so each funded gross is BELOW the
    price-only gross → paper kelly sizes DOWN, matching the backtest instead of over-sizing."""
    df = _frame(funding_rate=0.001)
    res_price = _run(df, include_funding=False)
    res_fund = _run(df, include_funding=True)

    assert len(res_price.closed_gross) == len(res_fund.closed_gross) >= 1
    # Every funded gross is strictly below its price-only twin (long pays funding > 0).
    for g_price, g_fund in zip(res_price.closed_gross, res_fund.closed_gross):
        assert g_fund < g_price

    # Hand-check the delta on the first trade equals -Σrate*hours*lev over its held window.
    tr = res_fund.closed_trades[0]
    held = int(tr["bars_held"])
    expected_delta = -1.0 * (held * 0.001) * 1.0 * LEV  # long, hours=1, funding=0.001/bar
    assert abs((res_fund.closed_gross[0] - res_price.closed_gross[0]) - expected_delta) < 1e-9


def test_scanner_no_funding_column_leaves_kernel_price_only():
    """A frame with NO funding_rate column keeps the scanner kernel byte-identical to the
    price-only path — no trade is stamped, funding stays owned by the (no-op) post-hoc pass."""
    df = _frame(funding_rate=None)
    res = _run(df, include_funding=True)
    assert res.closed_trades
    assert not any(t.get("_funding_from_kernel") for t in res.closed_trades)


def _drive_scanner_once(monkeypatch, df: pd.DataFrame, funding_enabled: bool) -> dict:
    """Drive one manage_positions_via_kernel cycle with the candle path stubbed to ``df``
    and run_strategy_execution spied, returning the captured kwargs."""
    from forven.db import kv_set

    kv_set("forven:settings", {"backtest_include_funding": funding_enabled})

    strat_id = "PAPER-FUND-SPY"
    strat = {
        "id": strat_id, "asset": "BTC", "type": "rsi_momentum",
        "runtime_type": "rsi_momentum", "timeframe": "1h",
        "stage": "paper", "params": _PARAMS,
    }
    strategy = RSIMomentumStrategy(strat_id, dict(_PARAMS, _asset="BTC"))

    monkeypatch.setattr(scanner, "fetch_candles", lambda coin, bars=300, interval="1h": df.copy())
    monkeypatch.setattr(scanner, "_enrich_scan_frame", lambda d, *a, **k: d)
    monkeypatch.setattr(scanner, "_trim_unclosed_latest_candle", lambda d, *a, **k: d)
    monkeypatch.setattr(scanner, "register", lambda *a, **k: None)
    monkeypatch.setattr("forven.strategies.registry.get_active", lambda: {strat_id: strategy})
    monkeypatch.setattr(scanner, "get_now",
                        lambda: (df.index[-1] + pd.Timedelta(hours=1)).to_pydatetime())

    captured: dict = {}
    real = bt.run_strategy_execution

    def _spy(*args, **kwargs):
        captured["include_funding"] = kwargs.get("include_funding")
        return real(*args, **kwargs)

    # manage_positions_via_kernel does `from forven.strategies import backtest as _bt`
    # inside the function, so patching the backtest module is what the scanner call sees.
    monkeypatch.setattr(bt, "run_strategy_execution", _spy)

    scanner.manage_positions_via_kernel(strat_id, strat, account_equity=10000.0)
    return captured


def test_scanner_passes_include_funding_true_when_enabled(forven_db, monkeypatch):
    """WIRING: the scanner forwards include_funding=True into the shared kernel when the
    funding setting is on (the default), so paper funds in-walk exactly like the backtest."""
    captured = _drive_scanner_once(monkeypatch, _frame(), funding_enabled=True)
    assert captured.get("include_funding") is True


def test_scanner_passes_include_funding_false_when_disabled(forven_db, monkeypatch):
    """WIRING: with the funding setting off, the scanner runs the kernel price-only
    (include_funding=False) — respecting the existing operator knob, one switch for both
    the in-walk and post-hoc funding legs."""
    captured = _drive_scanner_once(monkeypatch, _frame(), funding_enabled=False)
    assert captured.get("include_funding") is False
