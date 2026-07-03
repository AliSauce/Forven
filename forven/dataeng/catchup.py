"""Startup catch-up planning for desktop-only data collection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from forven.dataeng.catalog import Catalog


@dataclass(frozen=True)
class CatchUpTask:
    source: str
    market: str
    symbol: str
    timeframe: str
    stream: str
    start_ts: str
    end_ts: str
    permanent: bool = False
    # Why the task was planned: "stale" (series behind the latest closed bar)
    # or "gaps" (current, but interior bars are missing).
    reason: str = "stale"


# A series counts as gap-complete when it holds at least this fraction of the
# bars its [start_ts, end_ts] span implies. Below it, the executor's
# backfill_ohlcv_gaps pass is scheduled even though the series is current —
# previously only END-staleness was planned, so an old-but-gappy series was
# never repaired ("coverage" measured calendar span, not bars present).
COMPLETENESS_THRESHOLD = 0.98


class CatchUpPlanner:
    def __init__(self, catalog: Catalog | None = None) -> None:
        self.catalog = catalog or Catalog()

    def plan(self, *, now: datetime | None = None) -> list[CatchUpTask]:
        now_ts = _as_utc(now or datetime.now(timezone.utc))
        tasks: list[CatchUpTask] = []
        for row in self.catalog.list_coverage():
            stream = str(row.get("stream") or "")
            end_raw = row.get("end_ts")
            timeframe = str(row.get("timeframe") or "")
            if stream != "candles" or not end_raw or not timeframe:
                continue
            end_ts = _as_utc(end_raw)
            tf_delta = _timeframe_delta(timeframe)
            start_ts = end_ts + tf_delta
            # Only closed bars are catch-up candidates.
            latest_closed_start = _floor_to_timeframe(now_ts, tf_delta) - tf_delta
            if start_ts <= latest_closed_start:
                tasks.append(_task_from_row(row, start_ts, latest_closed_start, reason="stale"))
                continue
            # Current at the tail — but is it gap-complete inside its span?
            if _completeness(row, tf_delta) < COMPLETENESS_THRESHOLD:
                tasks.append(_task_from_row(row, _as_utc(row.get("start_ts") or end_ts), end_ts, reason="gaps"))
        return tasks


def _completeness(row: dict[str, object], tf_delta: pd.Timedelta) -> float:
    """rows-present / rows-expected over the series' recorded span. Returns 1.0
    when the bounds are unusable (never flag on bad metadata)."""
    try:
        start_raw = row.get("start_ts")
        end_raw = row.get("end_ts")
        rows = int(row.get("row_count") or 0)
        if not start_raw or not end_raw or rows <= 0:
            return 1.0
        span = _as_utc(end_raw) - _as_utc(start_raw)
        expected = int(span / tf_delta) + 1
        if expected <= 1:
            return 1.0
        return min(1.0, rows / expected)
    except Exception:
        return 1.0


def _task_from_row(row: dict[str, object], start_ts: pd.Timestamp, end_ts: pd.Timestamp, *, reason: str) -> CatchUpTask:
    return CatchUpTask(
        source=str(row.get("source") or ""),
        market=str(row.get("market") or ""),
        symbol=str(row.get("symbol") or ""),
        timeframe=str(row.get("timeframe") or ""),
        stream=str(row.get("stream") or ""),
        start_ts=_to_iso(start_ts),
        end_ts=_to_iso(end_ts),
        permanent=False,
        reason=reason,
    )


def _timeframe_delta(timeframe: str) -> pd.Timedelta:
    from forven.data import _timeframe_to_ms

    return pd.Timedelta(milliseconds=_timeframe_to_ms(timeframe))


def _floor_to_timeframe(value: pd.Timestamp, delta: pd.Timedelta) -> pd.Timestamp:
    ts = _as_utc(value)
    seconds = delta.total_seconds()
    if seconds <= 0:
        return ts.floor("s")
    if seconds % 86400 == 0:
        return ts.floor(f"{int(seconds // 86400)}D")
    if seconds % 3600 == 0:
        return ts.floor(f"{int(seconds // 3600)}h")
    if seconds % 60 == 0:
        return ts.floor(f"{int(seconds // 60)}min")
    return ts.floor(f"{int(seconds)}s")


def _as_utc(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _to_iso(value: pd.Timestamp) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")
