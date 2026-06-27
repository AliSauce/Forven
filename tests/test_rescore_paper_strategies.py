"""Tests for the Phase 3 re-score tool (scripts/rescore_paper_strategies.py)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rescore_paper_strategies.py"
_spec = importlib.util.spec_from_file_location("rescore_paper_strategies", _SCRIPT)
rescore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rescore)


def test_classify_flags():
    assert rescore.classify({"num_trades": 0, "total_return": 0.2})[0] == "DEAD"
    assert rescore.classify({"num_trades": 25, "total_return": -0.05})[0] == "UNPROFITABLE"
    assert rescore.classify({"num_trades": 3, "total_return": 0.1})[0] == "THIN"
    assert rescore.classify({"num_trades": 40, "total_return": 0.3})[0] == "OK"
    # Missing metrics shouldn't crash and shouldn't false-flag.
    assert rescore.classify({})[0] == "OK"


def test_extract_metrics_handles_nesting_and_aliases():
    nested = rescore._extract_metrics({"metrics": {"total_return": 0.5, "sharpe": 1.2, "trade_count": 7}})
    assert nested["total_return"] == 0.5
    assert nested["sharpe_ratio"] == 1.2
    assert nested["num_trades"] == 7.0
    top = rescore._extract_metrics({"win_rate": 0.6, "max_dd": -0.2})
    assert top["win_rate"] == 0.6
    assert top["max_drawdown"] == -0.2


def _insert_strategy(conn, sid, stage, metrics):
    conn.execute(
        "INSERT INTO strategies (id, name, type, runtime_type, symbol, timeframe, params, metrics, stage, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, sid, "rsi_momentum", "rsi_momentum", "BTC/USDT", "1h",
         json.dumps({"rsi_period": 14}), json.dumps(metrics), stage, stage, "2024-01-01", "2024-01-01"),
    )


def test_apply_refreshes_only_paper_strategies(forven_db, monkeypatch):
    from forven.db import get_db

    with get_db() as conn:
        _insert_strategy(conn, "PAPER-1", "paper", {"total_return": 5.0, "num_trades": 50})
        _insert_strategy(conn, "LIVE-1", "live_graduated", {"total_return": 5.0, "num_trades": 50})

    # Stub the heavy backtest with a controlled "re-scored" result.
    def _fake_backtest(strategy_id, asset, strategy_type, params, **kwargs):
        # New engine makes it look much worse (the re-score effect).
        return {"metrics": {"total_return": -0.03, "num_trades": 22, "sharpe_ratio": -0.4, "rescored": True}}

    monkeypatch.setattr(rescore, "_rescore_one", rescore._rescore_one)  # keep real, but patch backtest_strategy it imports
    import forven.strategies.backtest as bt
    monkeypatch.setattr(bt, "backtest_strategy", _fake_backtest)

    rc = rescore.main(["--apply"])
    assert rc == 0

    with get_db() as conn:
        paper = dict(conn.execute("SELECT metrics, stage FROM strategies WHERE id='PAPER-1'").fetchone())
        live = dict(conn.execute("SELECT metrics, stage FROM strategies WHERE id='LIVE-1'").fetchone())

    paper_metrics = json.loads(paper["metrics"])
    # Paper metrics refreshed to the re-scored values; stage UNCHANGED (no auto-demote).
    assert paper_metrics.get("rescored") is True
    assert paper_metrics["total_return"] == -0.03
    assert paper["stage"] == "paper"
    # Live strategy untouched (paper-only scope).
    assert json.loads(live["metrics"]).get("rescored") is None
    assert live["stage"] == "live_graduated"
