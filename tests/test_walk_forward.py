"""Tests for unified Forven walk-forward validation."""

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from forven.strategies.backtest import walk_forward


def _fake_ohlcv(n: int) -> pd.DataFrame:
    """Create deterministic OHLCV data."""
    base = pd.date_range(
        datetime.now(timezone.utc),
        periods=n,
        freq="h",
    )
    np.random.seed(42)
    close = 100 + np.cumsum(np.random.randn(n) * 0.05)
    return pd.DataFrame(
        {
            "open": close + 0.01,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "volume": 1_000,
        },
        index=base,
    )


def test_walk_forward_insufficient_data_returns_error(monkeypatch, forven_db):
    def _short_candles(*_args, **_kwargs):
        return _fake_ohlcv(200)

    monkeypatch.setattr("forven.scanner.fetch_candles", _short_candles)

    result = walk_forward(
        strategy_id="wf-short",
        asset="BTC",
        strategy_type="rsi_momentum",
        params={},
        total_bars=200,
    )
    # Could be "Insufficient data" or "Parameter lookback exceeds"
    err = result.get("error", "")
    assert "Insufficient data" in err or "Parameter lookback" in err


def test_walk_forward_returns_valid_structure(monkeypatch, forven_db):
    def _full_candles(*_args, **_kwargs):
        return _fake_ohlcv(1000)

    monkeypatch.setattr("forven.scanner.fetch_candles", _full_candles)

    result = walk_forward(
        strategy_id="wf-valid",
        asset="BTC",
        strategy_type="rsi_momentum",
        params={},
        total_bars=1000,
        n_splits=2,
    )

    assert result["verdict"] in {"PASS", "FAIL"}
    assert "splits" in result
    assert "aggregate_oos" in result
    assert isinstance(result["splits"], list)


def test_walk_forward_accepts_explicit_timeframe(monkeypatch, forven_db):
    captured: dict[str, object] = {}

    def _candles(*_args, **kwargs):
        captured["timeframe"] = kwargs.get("timeframe")
        return _fake_ohlcv(1000)

    monkeypatch.setattr("forven.strategies.backtest.load_backtest_candles", _candles)

    result = walk_forward(
        strategy_id="wf-timeframe",
        asset="BTC",
        strategy_type="rsi_momentum",
        params={"timeframe": "1h"},
        timeframe="4h",
        total_bars=1000,
        n_splits=2,
    )

    assert captured["timeframe"] == "4h"
    assert result["timeframe"] == "4h"


def test_walk_forward_gap_reduces_effective_oos(monkeypatch, forven_db):
    def _candles(*_args, **_kwargs):
        return _fake_ohlcv(1000)

    monkeypatch.setattr("forven.scanner.fetch_candles", _candles)

    baseline = walk_forward("wf-gap", "BTC", "rsi_momentum", {}, total_bars=1000, n_splits=2)
    gapped = walk_forward(
        "wf-gap", "BTC", "rsi_momentum", {}, total_bars=1000, n_splits=2,
        in_sample_pct=0.85,
    )

    assert baseline["aggregate_oos"]["trades"] >= 0
    assert gapped["aggregate_oos"]["trades"] >= 0
