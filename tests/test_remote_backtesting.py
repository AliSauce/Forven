"""Tests for remote backtesting integration and monthly return fixes."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. Remote URL resolution
# ---------------------------------------------------------------------------

def test_remote_resolution_returns_none_when_unconfigured(forven_db):
    """No env var + no settings → None."""
    from forven.api_core import _resolve_backtest_results_remote_api

    empty_settings = {"remote_engine_enabled": False, "remote_engine_url": ""}
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FORVEN_BACKTEST_RESULTS_REMOTE_API", None)
        with patch("forven.api_core._load_settings_payload", return_value=empty_settings):
            result = _resolve_backtest_results_remote_api()
    assert result is None


def test_remote_resolution_reads_env_var(forven_db):
    """Env var set → returns normalized URL."""
    from forven.api_core import _resolve_backtest_results_remote_api

    with patch.dict(os.environ, {"FORVEN_BACKTEST_RESULTS_REMOTE_API": "10.0.0.5:9050"}):
        result = _resolve_backtest_results_remote_api()
    assert result == "http://10.0.0.5:9050/api"


def test_remote_resolution_falls_back_to_settings(forven_db):
    """No env var + settings configured → returns settings URL."""
    from forven.api_core import _resolve_backtest_results_remote_api

    settings = {
        "remote_engine_enabled": True,
        "remote_engine_url": "http://192.168.1.100:9050",
    }
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FORVEN_BACKTEST_RESULTS_REMOTE_API", None)
        with patch("forven.api_core._load_settings_payload", return_value=settings):
            result = _resolve_backtest_results_remote_api()
    assert result == "http://192.168.1.100:9050/api"


def test_remote_resolution_ignores_disabled_settings(forven_db):
    """No env var + settings present but disabled → None."""
    from forven.api_core import _resolve_backtest_results_remote_api

    settings = {
        "remote_engine_enabled": False,
        "remote_engine_url": "http://192.168.1.100:9050",
    }
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FORVEN_BACKTEST_RESULTS_REMOTE_API", None)
        with patch("forven.api_core._load_settings_payload", return_value=settings):
            result = _resolve_backtest_results_remote_api()
    assert result is None


# ---------------------------------------------------------------------------
# 2. Remote-only behavior
# ---------------------------------------------------------------------------

def test_remote_unreachable_raises_503(forven_db):
    """Remote configured + unreachable → HTTPException(503)."""
    from fastapi import HTTPException
    from forven.api_core import get_backtest_results

    with patch("forven.api_core._is_remote_configured", return_value=True), \
         patch("forven.api_core._resolve_backtest_results_remote_api", return_value="http://dead-host:9050/api"), \
         patch("forven.api_core._fetch_remote_backtest_summaries", return_value=[]), \
         patch("forven.api_core._is_remote_backtest_results_available", return_value=False):
        with pytest.raises(HTTPException) as exc_info:
            get_backtest_results()
        assert exc_info.value.status_code == 503
        assert "unreachable" in str(exc_info.value.detail).lower()


def test_local_only_when_unconfigured(forven_db):
    """No remote → returns local Chroma data."""
    from forven.api_core import get_backtest_results

    fake_records = [
        {
            "id": "test-123",
            "metadata": {
                "strategy_id": "rsi_v1",
                "asset": "BTC",
                "total_return_pct": 0.5,
                "sharpe": 1.2,
                "win_rate": 0.6,
                "total_trades": 10,
                "profit_factor": 1.5,
                "max_drawdown_pct": 0.1,
                "recorded_at": "2025-01-01T00:00:00+00:00",
            },
        }
    ]
    with patch("forven.api_core._is_remote_configured", return_value=False), \
         patch("forven.api_core._chroma_backtest_records", return_value=fake_records), \
         patch("forven.api_core.get_db") as mock_db:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_db.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_db.return_value.__exit__ = MagicMock(return_value=False)
        results = get_backtest_results()
    assert len(results) == 1
    assert results[0]["id"] == "test-123"


def test_remote_detail_unreachable_raises_503(forven_db):
    """Remote configured + unreachable on detail → HTTPException(503)."""
    from fastapi import HTTPException
    from forven.api_core import get_backtest_result

    with patch("forven.api_core._is_remote_configured", return_value=True), \
         patch("forven.api_core._resolve_backtest_results_remote_api", return_value="http://dead-host:9050/api"), \
         patch("forven.api_core._fetch_remote_backtest_detail", return_value=None), \
         patch("forven.api_core._is_remote_backtest_results_available", return_value=False):
        with pytest.raises(HTTPException) as exc_info:
            get_backtest_result("some-id")
        assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# 3. Monthly return fixes
# ---------------------------------------------------------------------------

def test_monthly_return_derived_from_total(forven_db):
    """total_return=50%, monthly=None, duration ~6mo → correct geometric monthly."""
    from forven.api_core import _normalize_backtest_summary

    record = {
        "id": "test-derive",
        "metadata": {
            "strategy_id": "rsi_v2",
            "asset": "BTC",
            "total_return_pct": 0.5,  # 50%
            "monthly_return_pct": -999.0,  # sentinel
            "annualized_return_pct": -999.0,
            "backtest_months": -999.0,
            "sharpe": 1.0,
            "win_rate": 0.6,
            "total_trades": 20,
            "profit_factor": 1.3,
            "max_drawdown_pct": 0.1,
            "start_date": "2024-06-01T00:00:00+00:00",
            "end_date": "2024-12-01T00:00:00+00:00",
            "recorded_at": "2024-12-01T00:00:00+00:00",
        },
    }

    result = _normalize_backtest_summary(record)

    assert result["total_return"] == 50.0
    assert result["monthly_return_pct"] is not None
    assert result["monthly_return_pct"] != 0.0
    # Geometric monthly for 50% over ~6 months should be roughly ~6.9%
    assert 5.0 < result["monthly_return_pct"] < 9.0
    assert result["annualized_return_pct"] is not None
    assert result["annualized_return_pct"] > 50.0  # annualized should be larger


def test_monthly_return_sentinel_filtered(forven_db):
    """-999.0 in metadata → None in output (when no derivation possible)."""
    from forven.api_core import _normalize_backtest_summary

    record = {
        "id": "test-sentinel",
        "metadata": {
            "strategy_id": "rsi_v3",
            "asset": "BTC",
            "total_return_pct": 0.0,  # zero total return, no derivation
            "monthly_return_pct": -999.0,
            "annualized_return_pct": -999.0,
            "backtest_months": -999.0,
            "sharpe": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
            "recorded_at": "2024-12-01T00:00:00+00:00",
        },
    }

    result = _normalize_backtest_summary(record)

    assert result["monthly_return_pct"] is None
    assert result["annualized_return_pct"] is None
    assert result["backtest_months"] is None


def test_coerce_backtest_summary_filters_sentinel(forven_db):
    """_coerce_backtest_summary_payload filters -999.0 sentinels."""
    from forven.api_core import _coerce_backtest_summary_payload

    record = {
        "id": "remote-1",
        "strategy_name": "test_strat",
        "symbol": "BTC",
        "timeframe": "1h",
        "created_at": "2024-12-01T00:00:00+00:00",
        "total_return": 50.0,
        "monthly_return_pct": -999.0,
        "annualized_return_pct": -999.0,
        "backtest_months": -999.0,
        "sharpe_ratio": 1.0,
        "max_drawdown": 5.0,
        "win_rate": 60.0,
        "total_trades": 10,
        "profit_factor": 1.5,
    }
    result = _coerce_backtest_summary_payload(record)
    assert result is not None
    assert result["monthly_return_pct"] is None
    assert result["annualized_return_pct"] is None
    assert result["backtest_months"] is None


def test_leaderboard_does_not_use_total_as_monthly(forven_db):
    """Leaderboard monthly_return_pct defaults to 0.0, not total_return."""
    from forven.api_domains.analytics import _dashboard_leaderboard_entries

    fake_strategy = {
        "id": "strat-1",
        "name": "Test Strategy",
        "symbol": "BTC",
        "timeframe": "1h",
        "metrics": '{"total_return": 142.0, "sharpe_ratio": 2.0, "win_rate": 65.0}',
        "status": "active",
    }

    with patch("forven.api_domains.analytics.get_strategies", return_value=[fake_strategy]):
        entries = _dashboard_leaderboard_entries()

    assert len(entries) == 1
    # monthly_return should NOT be 142.0 (the total_return)
    assert entries[0]["monthly_return_pct"] != 142.0
    assert entries[0]["monthly_return_pct"] == 0.0


# ---------------------------------------------------------------------------
# 4. Backtesting status remote_error
# ---------------------------------------------------------------------------

def test_backtesting_status_reports_remote_error(forven_db):
    """Status endpoint reports remote_error when remote is configured but unreachable."""
    from forven.api_core import get_backtesting_status

    with patch("forven.api_core._resolve_backtest_results_remote_api", return_value="http://dead-host:9050/api"), \
         patch("forven.api_core._is_remote_backtest_results_available", return_value=False), \
         patch("forven.api_core.get_backtesting_runs", return_value={"runs": []}), \
         patch("forven.api_core.get_backtesting_outcomes", return_value={}):
        result = get_backtesting_status(remote_skip=False)
    assert "remote_error" in result
    assert "unreachable" in result["remote_error"].lower()
    assert result["remote_available"] is False
