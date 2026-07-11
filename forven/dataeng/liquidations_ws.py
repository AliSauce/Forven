"""OKX all-market liquidation WS capture → 1h liquidation buckets.

Forward-only: Binance's historical liquidation REST API is dead
(``allForceOrders`` is auth-gated/deprecated) and — verified empirically from
this network — Binance's futures WS handshakes and acks subscriptions but
never delivers market-data frames (policy-blocked data plane; spot WS and
futures REST both work). OKX's public ``liquidation-orders`` channel covers
EVERY swap instrument in one subscription, states the liquidated position
side explicitly (``posSide``), and flows from here — so liquidation capture
is sourced from OKX as a labeled cross-venue proxy. Liquidation cascades are
venue-correlated; magnitudes are OKX's, not Binance's. The lake schema is
unchanged from the retired Binance REST collector
(``derivatives/{sym}/liquidations_1h.parquet``: timestamp = bucket START,
long_liq_usd, short_liq_usd, liq_count, liq_imbalance).

Events aggregate into per-symbol 1h buckets in memory; a bucket is flushed
only after its hour CLOSES. Partial (still-open) buckets never touch parquet:
the enrichment join re-stamps liquidations to bucket close, and
``_combine_and_save`` keeps the FIRST row on timestamp collisions, so a
premature partial row would permanently shadow the complete one.

Closed hours with NO events are persisted as explicit ZERO rows (from the
per-pair coverage start forward), not skipped: the enrichment ASOF join matches
backward, so an event-less hour without a row would silently read the previous
bucket's stale aggregates (phantom liquidation values indistinguishable from
real ones). A zero row is a value AT its own bucket close, so it stays causal.

The in-progress partial is CHECKPOINTED to a sidecar (``.liq_ws_checkpoint.json``,
atomic write) every ~30s and restored on start, so an unexpected crash/kill
resumes the current hour instead of dropping it. If the hour already closed
while the process was down, the restored partial flushes as a completed bucket
and the zero-fill cursor makes the intervening empty hours honest zeros.

Runs in either of two mutually exclusive modes, arbitrated by a file lock so
capture is single-instance across processes:

- inside the backend daemon via :func:`run_capture_supervised` (default ON;
  set ``FORVEN_ENABLE_LIQUIDATIONS=0`` to opt out). While another process
  holds the lock the daemon retries every few minutes, so it takes over
  automatically when a standalone capture stops.
- standalone: ``python -m forven.dataeng.liquidations_ws`` — used to start
  capture immediately without a backend restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
SUBSCRIBE_MSG = json.dumps(
    {"op": "subscribe", "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]}
)
INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"

BUCKET_MS = 3_600_000
# A bucket flushes once its hour has closed plus this grace, covering event
# timestamps that trail the wall clock slightly.
FLUSH_GRACE_MS = 5_000
FLUSH_CHECK_SECONDS = 60.0
# OKX closes sockets idle >30s; send an app-level "ping" when recv is quiet.
PING_IDLE_SECONDS = 20.0
# How long the daemon waits before re-trying the capture lock when a
# standalone capture process owns it.
LOCK_RETRY_SECONDS = 300.0
# Throttle instrument-table refreshes triggered by unknown instIds.
CTVAL_REFRESH_MIN_SECONDS = 3600.0
RECONNECT_BACKOFF_SECONDS = (1, 5, 15, 60, 300)
# Fraction of ±jitter applied to each backoff step. Without it, many capture
# processes that dropped together (a venue-wide blip) all reconnect at the same
# ladder boundaries — a thundering herd that hammers the venue in lockstep and
# re-triggers the same drop. ±20% de-syncs them.
RECONNECT_JITTER_FRAC = 0.20
# How often the in-memory partial buckets are checkpointed to disk so an
# unexpected crash/kill (not a graceful shutdown) doesn't lose the current hour.
CHECKPOINT_INTERVAL_SECONDS = 30.0


def _backoff_delay(backoff_idx: int) -> float:
    """Backoff step for ``backoff_idx`` with ±RECONNECT_JITTER_FRAC jitter."""
    base = RECONNECT_BACKOFF_SECONDS[min(backoff_idx, len(RECONNECT_BACKOFF_SECONDS) - 1)]
    jitter = base * RECONNECT_JITTER_FRAC
    return max(0.0, base + random.uniform(-jitter, jitter))


def _derivatives_dir() -> Path:
    from forven.data_manager import DERIVATIVES_DIR

    return DERIVATIVES_DIR


def _lock_path() -> Path:
    return _derivatives_dir() / ".liq_ws.capture.lock"


def _status_path() -> Path:
    return _derivatives_dir() / ".liq_ws_status.json"


def _checkpoint_path() -> Path:
    return _derivatives_dir() / ".liq_ws_checkpoint.json"


def fetch_contract_values() -> dict[str, float]:
    """instId → ctVal (contract size in base coin) for USDT-linear swaps."""
    import requests

    resp = requests.get(INSTRUMENTS_URL, timeout=30)
    resp.raise_for_status()
    rows = (resp.json() or {}).get("data") or []
    out: dict[str, float] = {}
    for row in rows:
        inst_id = str(row.get("instId", ""))
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        try:
            ct_val = float(row.get("ctVal") or 0)
        except (TypeError, ValueError):
            continue
        if ct_val > 0:
            out[inst_id] = ct_val
    return out


class LiquidationCapture:
    """Thread-safe in-memory aggregation of OKX liquidation events into 1h buckets."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        # (pair, bucket_start_ms) -> [long_liq_usd, short_liq_usd, liq_count]
        self._buckets: dict[tuple[str, int], list[float]] = {}
        # Per-pair "gap-honest" cursor: buckets are only persisted when their hour
        # HAD events, so the enrichment ASOF join carries a stale prior bucket
        # forward across empty hours (phantom liquidation values). To make the
        # stream gap-honest we ALSO write explicit zero rows for closed hours that
        # had no events. ``_coverage_start_ms`` is the first bucket this capture
        # ever saw for a pair (nothing before it is knowable — before capture the
        # value is genuinely unknown, not zero) and ``_zero_cursor_ms`` is the next
        # closed hour we still owe a row for (real or zero). Persisted to the
        # checkpoint sidecar so a restart resumes without re-emitting or skipping.
        self._coverage_start_ms: dict[str, int] = {}
        self._zero_cursor_ms: dict[str, int] = {}
        self._ct_vals: dict[str, float] = {}
        self._ct_vals_fetched_at = 0.0
        self.events_total = 0
        self.events_skipped = 0
        self.buckets_flushed = 0
        self.last_event_ms: int | None = None

    def refresh_contract_values(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._ct_vals_fetched_at < CTVAL_REFRESH_MIN_SECONDS:
            return
        try:
            table = fetch_contract_values()
        except Exception as exc:
            log.warning("OKX instrument table refresh failed: %s", exc)
            return
        with self._guard:
            self._ct_vals = table
            self._ct_vals_fetched_at = now
        log.info("OKX instrument table loaded: %d USDT swaps", len(table))

    def _skip(self) -> None:
        with self._guard:
            self.events_skipped += 1

    def ingest(self, message: dict) -> None:
        """Ingest one liquidation-orders push message (may carry many events)."""
        for item in message.get("data") or []:
            inst_id = str(item.get("instId", ""))
            if not inst_id.endswith("-USDT-SWAP"):
                self._skip()
                continue
            pair = inst_id[: -len("-SWAP")]
            with self._guard:
                ct_val = self._ct_vals.get(inst_id)
            if ct_val is None:
                # New listing since the last table load — refresh (throttled)
                # and drop this event rather than guess a multiplier.
                self._skip()
                self.refresh_contract_values()
                continue
            for detail in item.get("details") or []:
                try:
                    size = float(detail.get("sz") or 0)
                    price = float(detail.get("bkPx") or 0)
                    event_ms = int(detail.get("ts") or 0)
                except (TypeError, ValueError):
                    self._skip()
                    continue
                if size <= 0 or price <= 0 or event_ms <= 0:
                    self._skip()
                    continue
                usd = size * ct_val * price
                pos_side = str(detail.get("posSide", "")).lower()
                if pos_side not in ("long", "short"):
                    # Net-mode accounts omit posSide; a sell order closes a long.
                    pos_side = "long" if str(detail.get("side", "")).lower() == "sell" else "short"
                bucket_ms = event_ms - (event_ms % BUCKET_MS)
                with self._guard:
                    bucket = self._buckets.setdefault((pair, bucket_ms), [0.0, 0.0, 0])
                    if pos_side == "long":
                        bucket[0] += usd
                    else:
                        bucket[1] += usd
                    bucket[2] += 1
                    self.events_total += 1
                    self.last_event_ms = max(self.last_event_ms or 0, event_ms)
                    # First bucket seen for this pair anchors zero-fill coverage:
                    # closed hours from here forward that stay empty become zeros.
                    prior = self._coverage_start_ms.get(pair)
                    if prior is None or bucket_ms < prior:
                        self._coverage_start_ms[pair] = bucket_ms
                    if pair not in self._zero_cursor_ms:
                        self._zero_cursor_ms[pair] = bucket_ms

    @staticmethod
    def _row(bucket_ms: int, long_usd: float, short_usd: float, count: int) -> dict:
        total = long_usd + short_usd
        return {
            "timestamp": bucket_ms,
            "long_liq_usd": long_usd,
            "short_liq_usd": short_usd,
            "liq_count": int(count),
            "liq_imbalance": ((long_usd - short_usd) / total) if total > 0 else 0.0,
        }

    def _latest_closed_bucket_ms(self, now_ms: int) -> int:
        """Start-ms of the most recent hour that has fully CLOSED (incl. grace)."""
        cutoff = now_ms - FLUSH_GRACE_MS
        # A bucket B closes at B+BUCKET_MS; the newest closed bucket starts at the
        # last hour boundary at or before (cutoff - BUCKET_MS).
        closed_edge = cutoff - BUCKET_MS
        return closed_edge - (closed_edge % BUCKET_MS)

    def _pop_completed(self, now_ms: int) -> dict[str, list[dict]]:
        cutoff = now_ms - FLUSH_GRACE_MS
        latest_closed = self._latest_closed_bucket_ms(now_ms)
        by_pair: dict[str, list[dict]] = {}
        with self._guard:
            done_keys = [key for key in self._buckets if key[1] + BUCKET_MS <= cutoff]
            real_by_pair: dict[str, dict[int, list[float]]] = {}
            for key in done_keys:
                pair, bucket_ms = key
                real_by_pair.setdefault(pair, {})[bucket_ms] = self._buckets.pop(key)

            # Every pair we have a zero-fill cursor for owes a continuous row per
            # closed hour up to ``latest_closed`` — real where events landed, an
            # explicit zero otherwise. This is what stops the ASOF join from
            # carrying a stale prior bucket across an event-less hour. Zero rows
            # are values AT their own bucket close, so they never move a value
            # earlier in time (causal).
            for pair, cursor in list(self._zero_cursor_ms.items()):
                if cursor > latest_closed:
                    continue
                real = real_by_pair.get(pair, {})
                rows = by_pair.setdefault(pair, [])
                bucket_ms = cursor
                while bucket_ms <= latest_closed:
                    if bucket_ms in real:
                        long_usd, short_usd, count = real[bucket_ms]
                        rows.append(self._row(bucket_ms, long_usd, short_usd, int(count)))
                    else:
                        rows.append(self._row(bucket_ms, 0.0, 0.0, 0))
                    bucket_ms += BUCKET_MS
                self._zero_cursor_ms[pair] = bucket_ms

            # A pair may have completed real buckets with no cursor yet (e.g. a
            # restored-from-checkpoint partial whose coverage anchor was lost) —
            # flush those directly so no data is dropped.
            for pair, real in real_by_pair.items():
                if pair in self._zero_cursor_ms:
                    continue
                rows = by_pair.setdefault(pair, [])
                for bucket_ms in sorted(real):
                    long_usd, short_usd, count = real[bucket_ms]
                    rows.append(self._row(bucket_ms, long_usd, short_usd, int(count)))
        return by_pair

    def flush_completed(self, now_ms: int | None = None) -> int:
        """Write all completed buckets to the lake. Sync — call off the event loop."""
        import pandas as pd

        from forven.data_manager import (
            DERIVATIVES_DIR,
            _combine_and_save,
            _get_stream_lock,
            _load_stream_parquet,
            _validate_stream_df,
        )

        if now_ms is None:
            now_ms = int(time.time() * 1000)
        by_pair = self._pop_completed(now_ms)
        flushed = 0
        for pair, rows in by_pair.items():
            try:
                path = DERIVATIVES_DIR / pair / "liquidations_1h.parquet"
                df = pd.DataFrame(rows)
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                df, _ = _validate_stream_df(
                    df, "liquidations", non_negative=["long_liq_usd", "short_liq_usd", "liq_count"]
                )
                if df is None or df.empty:
                    continue
                with _get_stream_lock(f"liq::{pair}"):
                    existing = _load_stream_parquet(path)
                    if existing is not None and not existing.empty:
                        seen = set(pd.to_datetime(existing["timestamp"], utc=True))
                        df = df[~df["timestamp"].isin(seen)]
                        if df.empty:
                            continue
                    flushed += _combine_and_save(
                        existing, df, path, stream="liquidations", symbol=pair
                    )
            except Exception as exc:
                # One bad symbol must not stall the rest of the flush; the
                # bucket is already popped, so log loudly — that hour is lost.
                log.error("Liquidation flush failed for %s (bucket lost): %s", pair, exc)
        with self._guard:
            self.buckets_flushed += flushed
        return flushed

    def write_status(self, connected: bool) -> None:
        """Best-effort heartbeat so ops can tell silence from failure."""
        try:
            with self._guard:
                payload = {
                    "source": "okx",
                    "connected": connected,
                    "pid": os.getpid(),
                    "events_total": self.events_total,
                    "events_skipped": self.events_skipped,
                    "buckets_flushed": self.buckets_flushed,
                    "last_event_ms": self.last_event_ms,
                    "pending_buckets": len(self._buckets),
                    "known_instruments": len(self._ct_vals),
                    "updated_ms": int(time.time() * 1000),
                }
            path = _status_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(str(path) + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            log.debug("Liquidation status write failed", exc_info=True)

    def save_checkpoint(self) -> None:
        """Persist the in-progress partial buckets + zero-fill cursors atomically.

        In-memory partials are otherwise lost on an unexpected crash/kill (only a
        graceful shutdown flushes). Round-tripping them means a restart resumes
        the current hour instead of dropping it, and the zero-fill cursors survive
        so no closed hour is re-emitted or skipped. Best-effort — a failed write
        just means the crash-recovery window reverts to the pre-fix behaviour."""
        try:
            with self._guard:
                payload = {
                    "buckets": [
                        {"pair": pair, "bucket_ms": bucket_ms,
                         "long_usd": vals[0], "short_usd": vals[1], "count": int(vals[2])}
                        for (pair, bucket_ms), vals in self._buckets.items()
                    ],
                    "coverage_start_ms": dict(self._coverage_start_ms),
                    "zero_cursor_ms": dict(self._zero_cursor_ms),
                    "saved_ms": int(time.time() * 1000),
                }
            path = _checkpoint_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(str(path) + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            log.debug("Liquidation checkpoint write failed", exc_info=True)

    def restore_checkpoint(self) -> int:
        """Restore partial buckets + cursors from the sidecar. Returns the number
        of partial buckets restored.

        Buckets whose hour already CLOSED while the process was down stay in the
        in-memory map — the next flush picks them up as completed (real events),
        and the zero-fill cursor makes the intervening empty hours honest zeros.
        So a restart never leaves a silent gap that ASOF would paper over."""
        path = _checkpoint_path()
        if not path.exists():
            return 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.debug("Liquidation checkpoint read failed", exc_info=True)
            return 0
        restored = 0
        with self._guard:
            for row in payload.get("buckets") or []:
                try:
                    pair = str(row["pair"])
                    bucket_ms = int(row["bucket_ms"])
                    vals = [float(row.get("long_usd") or 0.0),
                            float(row.get("short_usd") or 0.0),
                            int(row.get("count") or 0)]
                except (KeyError, TypeError, ValueError):
                    continue
                self._buckets[(pair, bucket_ms)] = vals
                restored += 1
            for pair, ms in (payload.get("coverage_start_ms") or {}).items():
                try:
                    self._coverage_start_ms[str(pair)] = int(ms)
                except (TypeError, ValueError):
                    continue
            for pair, ms in (payload.get("zero_cursor_ms") or {}).items():
                try:
                    self._zero_cursor_ms[str(pair)] = int(ms)
                except (TypeError, ValueError):
                    continue
        if restored:
            log.info("Restored %d partial liquidation bucket(s) from checkpoint", restored)
        return restored

    def clear_checkpoint(self) -> None:
        """Remove the sidecar after a clean flush so a stale partial can't be
        restored on the next start."""
        try:
            _checkpoint_path().unlink(missing_ok=True)
        except Exception:
            log.debug("Liquidation checkpoint clear failed", exc_info=True)


async def _listen_once(capture: LiquidationCapture, shutdown: asyncio.Event) -> None:
    """One WS connection lifetime: subscribe, ingest until close or shutdown."""
    import websockets

    await asyncio.to_thread(capture.refresh_contract_values, True)
    last_flush = time.time()
    last_checkpoint = time.time()
    async with websockets.connect(WS_URL, max_queue=4096) as ws:
        await ws.send(SUBSCRIBE_MSG)
        log.info("Liquidation WS connected (OKX liquidation-orders, all swaps)")
        await asyncio.to_thread(capture.write_status, True)
        while not shutdown.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=PING_IDLE_SECONDS)
            except asyncio.TimeoutError:
                await ws.send("ping")  # OKX app-level keepalive
                raw = None
            if raw is not None and raw != "pong":
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    msg = None
                if isinstance(msg, dict):
                    if msg.get("event") == "error":
                        raise RuntimeError(f"OKX subscription error: {msg}")
                    if "data" in msg:
                        capture.ingest(msg)
            now = time.time()
            if now - last_flush >= FLUSH_CHECK_SECONDS:
                last_flush = now
                await asyncio.to_thread(capture.flush_completed)
                await asyncio.to_thread(capture.write_status, True)
            # Checkpoint the in-progress hour so an unexpected kill doesn't lose
            # it (a graceful shutdown flushes; a crash between hours would not).
            if now - last_checkpoint >= CHECKPOINT_INTERVAL_SECONDS:
                last_checkpoint = now
                await asyncio.to_thread(capture.save_checkpoint)


async def run_capture(shutdown: asyncio.Event | None = None) -> bool:
    """Own the capture lock and listen until shutdown.

    Returns False immediately (without listening) when another process holds
    the capture lock; True when this process ran capture and was shut down.
    """
    from filelock import FileLock, Timeout

    if shutdown is None:
        shutdown = asyncio.Event()

    lock_file = _lock_path()
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_file))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return False

    capture = LiquidationCapture()
    # Resume any partial hour a previous run checkpointed before it died. Buckets
    # whose hour has since closed flush on the first pass; the zero-fill cursor
    # makes the down-time hours honest zeros rather than an ASOF-papered gap.
    capture.restore_checkpoint()
    backoff_idx = 0
    try:
        while not shutdown.is_set():
            try:
                await _listen_once(capture, shutdown)
                backoff_idx = 0  # clean close (server recycle)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = _backoff_delay(backoff_idx)
                backoff_idx += 1
                log.warning("Liquidation WS dropped (%s) — reconnecting in %.1fs", exc, delay)
                await asyncio.to_thread(capture.write_status, False)
                # Persist the partial so a crash during the backoff wait doesn't
                # lose it (the reconnect might never come back).
                await asyncio.to_thread(capture.save_checkpoint)
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        return True
    finally:
        # Flush completed buckets, then checkpoint whatever partial remains so an
        # exception in the flush path (or a still-open hour) survives to the next
        # start rather than being silently dropped.
        try:
            capture.flush_completed()
            capture.write_status(False)
            capture.save_checkpoint()
        except Exception:
            log.debug("Final liquidation flush failed", exc_info=True)
        lock.release()


async def run_capture_supervised(shutdown: asyncio.Event) -> None:
    """Daemon-facing wrapper: keep trying to own capture until shutdown.

    While a standalone capture process holds the lock this idles and retries,
    so ownership hands over automatically when that process exits.
    """
    while not shutdown.is_set():
        try:
            ran = await run_capture(shutdown)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("Liquidation capture crashed — retrying", exc_info=True)
            ran = False
        if ran or shutdown.is_set():
            return
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=LOCK_RETRY_SECONDS)
        except asyncio.TimeoutError:
            pass


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    shutdown = asyncio.Event()

    async def _run() -> None:
        ran = await run_capture(shutdown)
        if not ran:
            log.error(
                "Another process already owns liquidation capture (%s) — exiting",
                _lock_path(),
            )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
