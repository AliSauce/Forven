"""Data-manager overhaul — remaining phases (fable-updates).

Covers:
- Phase 1: perp-canonical OHLCV target resolution (binanceusdm when a USD-M
  perp is listed, spot fallback otherwise) in both data.py and market_data.py,
  plus the soft market-splice write guard.
- Phase 3: candle-path circuit breaker (fail fast after repeated venue
  failures; empty windows are benign) and scaffolding removal.
- Phase 4: ingestion runs surviving restart via KV (interrupted runs surfaced
  as failed), backfill progress/cancel, /data/versions from the revision log.
- Phase 5: completeness-aware catch-up planning (gappy-but-current series get
  a "gaps" task).
- Sim coverage gate in scanner.fetch_candles.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

import forven.data as data_mod


SYMBOL = "BTC-USDT"
TF = "1h"


def _bars(start: datetime, count: int) -> pd.DataFrame:
    rows = []
    for i in range(count):
        p = 100.0 + i
        rows.append(
            {
                "timestamp": start + timedelta(hours=i),
                "open": p, "high": p + 1.0, "low": p - 1.0, "close": p + 0.5, "volume": 10.0,
            }
        )
    return pd.DataFrame(rows)


def _closed_start(bars_ago: int) -> datetime:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return now - timedelta(hours=bars_ago + 2)


@pytest.fixture()
def lake(tmp_path):
    with patch("forven.data.DATA_DIR", tmp_path / "ohlcv"):
        data_mod._invalidate_catalog_cache()
        yield tmp_path / "ohlcv"
        data_mod._invalidate_catalog_cache()


# ---------------------------------------------------------------------------
# Phase 1: perp-canonical resolution
# ---------------------------------------------------------------------------


class TestPerpResolution:
    def test_data_resolves_perp_when_listed(self, monkeypatch):
        monkeypatch.setattr(
            data_mod, "_cached_markets",
            lambda ex: {"BTC/USDT:USDT": {}} if ex == "binanceusdm" else {},
        )
        _, ccxt_symbol, source = data_mod._resolve_ohlcv_target("binance", SYMBOL)
        assert ccxt_symbol == "BTC/USDT:USDT"
        assert source == "binanceusdm"

    def test_data_falls_back_to_spot_without_perp(self, monkeypatch):
        monkeypatch.setattr(data_mod, "_cached_markets", lambda ex: {})
        _, ccxt_symbol, source = data_mod._resolve_ohlcv_target("binance", "ZZZ-USDT")
        assert ccxt_symbol == "ZZZ/USDT"
        assert source == "binance"

    def test_explicit_exchange_honoured(self, monkeypatch):
        monkeypatch.setattr(
            data_mod, "_cached_markets",
            lambda ex: {"BTC/USDT:USDT": {}},
        )
        _, ccxt_symbol, source = data_mod._resolve_ohlcv_target("kraken", SYMBOL)
        assert ccxt_symbol == "BTC/USDT"
        assert source == "kraken"

    def test_market_data_resolver(self, monkeypatch):
        import forven.market_data as md

        monkeypatch.setattr(md, "_perp_symbols", lambda: frozenset({"BTC/USDT:USDT"}))
        _, symbol, market = md.resolve_binance_market("BTC")
        assert symbol == "BTC/USDT:USDT"
        assert market == "perp"
        _, symbol, market = md.resolve_binance_market("ZZZ")
        assert symbol == "ZZZ/USDT"
        assert market == "spot"

    def test_market_splice_write_guard_logs_once(self, lake, caplog):
        import logging

        start = _closed_start(30)
        data_mod.save_parquet(_bars(start, 10), SYMBOL, TF, source="binance")  # spot
        data_mod._market_mismatch_logged.clear()
        with caplog.at_level(logging.WARNING, logger="forven.data"):
            data_mod.save_parquet(
                data_mod.load_parquet(SYMBOL, TF), SYMBOL, TF, source="binanceusdm"
            )  # perp over spot
        assert any("MARKET SPLICE" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Phase 3: candle-path circuit breaker
# ---------------------------------------------------------------------------


class TestCandleBreaker:
    @pytest.fixture(autouse=True)
    def _reset_breakers(self):
        with data_mod._candle_breakers_lock:
            data_mod._candle_breakers.clear()
        yield
        with data_mod._candle_breakers_lock:
            data_mod._candle_breakers.clear()

    def test_breaker_opens_after_repeated_failures(self, lake, monkeypatch):
        calls = {"n": 0}

        def _boom(*args, **kwargs):
            calls["n"] += 1
            raise RuntimeError("venue down")

        monkeypatch.setattr(data_mod, "_fetch_ohlcv_once", _boom)
        monkeypatch.setattr(data_mod, "_fetch_range", _boom)
        monkeypatch.setattr(data_mod, "get_exchange", lambda ex: object())
        monkeypatch.setattr(data_mod, "_cached_markets", lambda ex: {})

        for _ in range(3):
            with pytest.raises(RuntimeError, match="venue down"):
                data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id="kraken", limit=10)
        assert calls["n"] == 3

        with pytest.raises(RuntimeError, match="circuit is open"):
            data_mod.fetch_ohlcv_chunked(SYMBOL, TF, exchange_id="kraken", limit=10)
        assert calls["n"] == 3  # failed fast, no venue call

    def test_empty_window_is_benign(self, lake, monkeypatch):
        start = _closed_start(30)
        data_mod.save_parquet(_bars(start, 10), SYMBOL, TF)

        monkeypatch.setattr(data_mod, "_fetch_range", lambda *a, **k: data_mod._normalize_ohlcv_frame(pd.DataFrame()))
        monkeypatch.setattr(data_mod, "get_exchange", lambda ex: object())
        monkeypatch.setattr(data_mod, "_cached_markets", lambda ex: {})

        for _ in range(5):
            record = data_mod.fetch_ohlcv_chunked(
                SYMBOL, TF, exchange_id="kraken", since_ms=int(datetime.now(timezone.utc).timestamp() * 1000)
            )
            assert record["bars_new"] == 0
        assert data_mod._candle_breaker("kraken").status == "closed"


# ---------------------------------------------------------------------------
# Phase 4: run persistence / backfill cancel / versions
# ---------------------------------------------------------------------------


class TestRunPersistence:
    @pytest.fixture(autouse=True)
    def _isolate_runs(self):
        with data_mod._ingestion_runs_lock:
            saved = dict(data_mod._ingestion_runs)
            loaded = data_mod._ingestion_runs_loaded
            data_mod._ingestion_runs.clear()
            data_mod._ingestion_runs_loaded = False
        yield
        with data_mod._ingestion_runs_lock:
            data_mod._ingestion_runs.clear()
            data_mod._ingestion_runs.update(saved)
            data_mod._ingestion_runs_loaded = loaded

    def test_interrupted_runs_surface_as_failed_after_restart(self, monkeypatch):
        monkeypatch.setattr(
            "forven.db.kv_get",
            lambda key, default=None: [
                {"id": "run-a", "status": "running", "started_at": "2026-07-01T00:00:00Z"},
                {"id": "run-b", "status": "completed", "started_at": "2026-07-01T01:00:00Z"},
            ] if key == data_mod._INGESTION_RUNS_KV_KEY else default,
        )
        runs = {run["id"]: run for run in data_mod.get_active_ingestion_runs()}
        assert runs["run-a"]["status"] == "failed"
        assert "restarted" in runs["run-a"]["error"]
        assert runs["run-b"]["status"] == "completed"


class TestBackfillCancelProgress:
    def test_cancel_between_symbols(self, lake, monkeypatch):
        from forven.data_manager import DataManager

        (lake / "AAA-USDT").mkdir(parents=True)
        (lake / "AAA-USDT" / "1h.parquet").write_bytes(b"x")
        (lake / "BBB-USDT").mkdir(parents=True)
        (lake / "BBB-USDT" / "1h.parquet").write_bytes(b"x")

        processed: list[str] = []
        monkeypatch.setattr(DataManager, "_backfill_ohlcv", lambda self, fs, bv: processed.append(fs) or {})
        monkeypatch.setattr(DataManager, "_backfill_funding", lambda self, fs, bv: {})
        monkeypatch.setattr(DataManager, "_backfill_metrics", lambda self, fs, bv, **kw: {})

        cancel = threading.Event()
        progress: list[tuple[int, int, str]] = []

        def _cb(done, total, sym):
            progress.append((done, total, sym))
            cancel.set()  # cancel after the first symbol starts

        summary = DataManager().backfill(progress_cb=_cb, cancel_event=cancel)
        assert summary.get("cancelled") is True
        assert processed == ["AAA-USDT"]
        assert progress[0] == (0, 2, "AAA-USDT")


class TestDatasetVersions:
    def test_versions_include_current_and_restatements(self, lake):
        from forven.api_domains.data import get_dataset_versions

        start = _closed_start(30)
        data_mod.save_parquet(_bars(start, 10), SYMBOL, TF)
        # Restate one bar (within [low, high]) so the revision log gets a row.
        frame = data_mod.load_parquet(SYMBOL, TF)
        mask = frame["timestamp"] == frame["timestamp"].iloc[3]
        frame.loc[mask, "close"] = frame.loc[mask, "low"] + 0.1
        data_mod.save_parquet(frame, SYMBOL, TF)
        data_mod._invalidate_catalog_cache()

        versions = get_dataset_versions(symbol=SYMBOL, timeframe=TF)
        kinds = {v["source"] for v in versions}
        assert "restatement" in kinds
        current = [v for v in versions if str(v["id"]).startswith("current-")]
        assert current and current[0]["checksum"]  # single-series query -> checksum
        restated = [v for v in versions if v["source"] == "restatement"]
        assert restated[0]["row_count"] == 1


# ---------------------------------------------------------------------------
# Phase 5: completeness-aware planning
# ---------------------------------------------------------------------------


class _StubCatalog:
    def __init__(self, rows):
        self._rows = rows

    def list_coverage(self):
        return self._rows


def _coverage_row(*, end_ts: str, start_ts: str, row_count: int, stream: str = "candles"):
    return {
        "source": "binance",
        "market": "perp",
        "symbol": SYMBOL,
        "timeframe": TF,
        "stream": stream,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "row_count": row_count,
    }


class TestCompletenessPlanning:
    NOW = pd.Timestamp("2026-06-10T00:30:00Z").to_pydatetime()

    def _plan(self, rows):
        from forven.dataeng.catchup import CatchUpPlanner

        return CatchUpPlanner(_StubCatalog(rows)).plan(now=self.NOW)

    def test_gappy_current_series_gets_gaps_task(self):
        # Current at the tail (end = latest closed bar) but only half the bars.
        tasks = self._plan([
            _coverage_row(start_ts="2026-06-01T00:00:00Z", end_ts="2026-06-09T23:00:00Z", row_count=108),
        ])
        assert len(tasks) == 1
        assert tasks[0].reason == "gaps"

    def test_complete_current_series_not_planned(self):
        # 2026-06-01T00 .. 2026-06-09T23 = 216 hourly bars, all present.
        tasks = self._plan([
            _coverage_row(start_ts="2026-06-01T00:00:00Z", end_ts="2026-06-09T23:00:00Z", row_count=216),
        ])
        assert tasks == []

    def test_stale_series_planned_as_stale(self):
        tasks = self._plan([
            _coverage_row(start_ts="2026-06-01T00:00:00Z", end_ts="2026-06-05T00:00:00Z", row_count=97),
        ])
        assert len(tasks) == 1
        assert tasks[0].reason == "stale"

    def test_non_candle_streams_ignored(self):
        tasks = self._plan([
            _coverage_row(start_ts="2026-06-01T00:00:00Z", end_ts="2026-06-05T00:00:00Z", row_count=1, stream="trades"),
        ])
        assert tasks == []


# ---------------------------------------------------------------------------
# Phase 3: scaffolding really gone
# ---------------------------------------------------------------------------


def test_scaffolding_modules_deleted():
    for name in ("validation", "microstructure", "onchain", "derivatives", "registry"):
        with pytest.raises(ImportError):
            __import__(f"forven.dataeng.{name}", fromlist=["_"])


def test_hub_micro_readers_removed():
    from forven.dataeng.hub import DataHub

    assert not hasattr(DataHub, "trades")
    assert not hasattr(DataHub, "orderbook")


def test_ccxt_source_capabilities_match_fetch():
    from forven.dataeng.ccxt_source import CcxtSource
    from forven.dataeng.source import Stream

    assert CcxtSource().capabilities == {Stream.CANDLES, Stream.FUNDING, Stream.OI}


# ---------------------------------------------------------------------------
# Sim coverage gate
# ---------------------------------------------------------------------------


class TestSimCoverageGate:
    def _frame(self, count: int) -> pd.DataFrame:
        idx = pd.date_range("2026-06-01", periods=count, freq="1h", tz="UTC")
        return pd.DataFrame(
            {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx
        )

    @pytest.fixture()
    def _sim(self, monkeypatch):
        monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: True)
        monkeypatch.setattr(
            "forven.sim.clock.get_now", lambda: datetime(2026, 6, 10, tzinfo=timezone.utc)
        )

    def test_short_cache_falls_through_to_full_fetch(self, _sim, monkeypatch):
        import forven.scanner as scanner

        monkeypatch.setattr(
            "forven.sim.data_pump.get_cached_candles", lambda *a, **k: self._frame(10)
        )
        monkeypatch.setattr(scanner, "fetch_hyperliquid_candles", lambda *a, **k: self._frame(20))
        out = scanner.fetch_candles("BTC", bars=20, interval="1h")
        assert len(out) == 20

    def test_covering_cache_served_without_fetch(self, _sim, monkeypatch):
        import forven.scanner as scanner

        monkeypatch.setattr(
            "forven.sim.data_pump.get_cached_candles", lambda *a, **k: self._frame(20)
        )

        def _no_fetch(*a, **k):
            raise AssertionError("must not fetch when the cache covers")

        monkeypatch.setattr(scanner, "fetch_hyperliquid_candles", _no_fetch)
        out = scanner.fetch_candles("BTC", bars=20, interval="1h")
        assert len(out) == 20

    def test_venue_no_better_serves_cache(self, _sim, monkeypatch):
        import forven.scanner as scanner

        monkeypatch.setattr(
            "forven.sim.data_pump.get_cached_candles", lambda *a, **k: self._frame(10)
        )
        monkeypatch.setattr(scanner, "fetch_hyperliquid_candles", lambda *a, **k: self._frame(8))
        out = scanner.fetch_candles("BTC", bars=20, interval="1h")
        assert len(out) == 10  # cache is the best available
