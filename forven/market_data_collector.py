"""Market data collector — fetches and stores funding rates, OI, and other
derivatives metrics from HyperLiquid for historical backtesting.

Supports:
- Live collection (scheduled every 15 minutes)
- Historical backfill via fundingHistory endpoint pagination
- Generic time-series storage in market_data_history table
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from forven.db import get_db, kv_get, kv_set, log_activity
from forven.market_data import post_hyperliquid_info

log = logging.getLogger("forven.market_data_collector")

# Assets to collect data for
COLLECT_ASSETS = ["BTC", "ETH", "SOL"]

# Self-healing backfill state — per-asset record of the last backfill attempt
# and the oldest timestamp the exchange could provide, so gap checks don't
# re-fetch history that simply doesn't exist (e.g. recently listed assets).
_FUNDING_BACKFILL_STATE_KEY = "market-data:funding-backfill-state"

# Re-attempt a still-gapped backfill at most this often. Keeps a backtest loop
# from hammering the exchange when the requested window predates available data.
_FUNDING_BACKFILL_COOLDOWN_HOURS = 6.0
# A funding series counts as "covered" only if it ALSO reaches near ~now and is
# dense enough across the window. An earliest-only check let stale series (XRP's
# latest record was 2023-08) and sparse series (BNB: ~1500 points over 3 years,
# full of internal gaps) masquerade as covered — so backtest enrichment saw
# mostly-NaN funding and every strategy on that asset was funding-incomplete and
# held out of live over a phantom "complete" dataset.
_FUNDING_RECENCY_HOURS = 48.0  # latest stored record must be within this of now
_FUNDING_DENSITY_FLOOR = 0.75  # require >= this fraction of expected points
_FUNDING_EXPECTED_INTERVAL_HOURS = 8.0  # conservative venue funding interval (Binance 8h)

# How far back the scheduled reconciliation keeps funding history filled.
DEFAULT_FUNDING_TARGET_DAYS = 730

# Metric types
METRIC_FUNDING_RATE = "funding_rate"
METRIC_OPEN_INTEREST = "open_interest"
METRIC_MARK_PRICE = "mark_price"
METRIC_PREMIUM = "premium"

_ALL_METRICS = {METRIC_FUNDING_RATE, METRIC_OPEN_INTEREST, METRIC_MARK_PRICE, METRIC_PREMIUM}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store_data_point(
    asset: str,
    metric_type: str,
    value: float,
    timestamp_ms: int,
    source: str = "hyperliquid",
    extra: dict | None = None,
) -> bool:
    """Store a single data point, ignoring duplicates.

    Returns True if the row was inserted, False on duplicate or failure.
    The UNIQUE(asset, metric_type, timestamp_ms) index makes IGNORE
    semantically safe: a duplicate is the same point in time for the same
    asset/metric, so we prefer the first write. H-D2: return value exposes
    duplicate skips so callers can surface telemetry.
    """
    ts_iso = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()
    try:
        with get_db() as conn:
            before = int(conn.total_changes)
            conn.execute(
                """INSERT OR IGNORE INTO market_data_history
                   (asset, metric_type, value, timestamp, timestamp_ms, source, extra)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    asset,
                    metric_type,
                    float(value),
                    ts_iso,
                    int(timestamp_ms),
                    source,
                    json.dumps(extra, default=str) if extra else None,
                ),
            )
            inserted = int(conn.total_changes) > before
            if not inserted:
                log.debug(
                    "market_data_history duplicate skipped: asset=%s metric=%s ts_ms=%s",
                    asset, metric_type, timestamp_ms,
                )
            return inserted
    except Exception as exc:
        log.debug("Failed to store %s/%s: %s", asset, metric_type, exc)
        return False


def _store_batch(rows: list[tuple]) -> int:
    """Store multiple data points efficiently.

    Returns the number of rows *actually inserted* (not len(rows)). Any row
    with a pre-existing (asset, metric_type, timestamp_ms) triple is skipped
    by the UNIQUE constraint; the drop is logged for observability.
    """
    if not rows:
        return 0
    try:
        with get_db() as conn:
            before = int(conn.total_changes)
            conn.executemany(
                """INSERT OR IGNORE INTO market_data_history
                   (asset, metric_type, value, timestamp, timestamp_ms, source, extra)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            inserted = int(conn.total_changes) - before
        duplicates = max(0, len(rows) - inserted)
        if duplicates:
            log.info(
                "market_data_history batch: %d inserted, %d duplicates ignored",
                inserted, duplicates,
            )
        return inserted
    except Exception as exc:
        log.warning("Batch store failed: %s", exc)
        return 0


# ── Live Collection ─────────────────────────────────────────────────────────


def collect_current_snapshot(assets: list[str] | None = None) -> dict:
    """Fetch current funding rates + OI for all tracked assets and store.

    Called by the scheduler every 15 minutes.
    """
    assets = assets or COLLECT_ASSETS
    now_ms = _now_ms()
    stored = 0
    errors = []

    try:
        resp = post_hyperliquid_info({"type": "metaAndAssetCtxs"})
        if not isinstance(resp, list) or len(resp) < 2:
            return {"error": "Invalid metaAndAssetCtxs response", "stored": 0}

        meta, ctxs = resp[0], resp[1]
        universe = list((meta or {}).get("universe") or [])

        for idx, asset_info in enumerate(universe):
            asset_name = str((asset_info or {}).get("name") or "").upper()
            if asset_name not in assets:
                continue
            if idx >= len(ctxs):
                continue

            ctx = ctxs[idx] if isinstance(ctxs[idx], dict) else {}

            # Funding rate
            funding = ctx.get("funding")
            if funding is not None and _store_data_point(asset_name, METRIC_FUNDING_RATE, float(funding), now_ms):
                stored += 1

            # Open interest
            oi = ctx.get("openInterest")
            if oi is not None and _store_data_point(asset_name, METRIC_OPEN_INTEREST, float(oi), now_ms):
                stored += 1

            # Mark price
            mark = ctx.get("markPx")
            if mark is not None and _store_data_point(asset_name, METRIC_MARK_PRICE, float(mark), now_ms):
                stored += 1

            # Premium (mark vs oracle)
            premium = ctx.get("premium")
            if premium is not None and _store_data_point(asset_name, METRIC_PREMIUM, float(premium), now_ms):
                stored += 1

    except Exception as exc:
        errors.append(str(exc))
        log.warning("Snapshot collection failed: %s", exc)

    result = {
        "collected_at": _now_iso(),
        "assets": assets,
        "stored": stored,
        "errors": errors,
    }

    if stored > 0:
        log.info("Market data snapshot: stored %d data points for %s", stored, assets)

    return result


# ── Historical Backfill ─────────────────────────────────────────────────────


def backfill_funding_history(
    asset: str,
    days_back: int = 365,
    batch_size: int = 500,
) -> dict:
    """Backfill historical funding rates from HyperLiquid's fundingHistory endpoint.

    Paginates backwards from now, 500 records at a time (API limit).
    Funding settles every 8 hours, so 500 records ≈ 166 days.
    """
    normalized_asset = asset.strip().upper()
    now = datetime.now(timezone.utc)
    cutoff_ms = int((now - timedelta(days=days_back)).timestamp() * 1000)
    end_time_ms = _now_ms()

    total_stored = 0
    total_fetched = 0
    pages = 0
    oldest_ts = None

    log.info("Backfilling %s funding history: %d days back (cutoff=%s)",
             normalized_asset, days_back,
             datetime.fromtimestamp(cutoff_ms / 1000, tz=timezone.utc).isoformat())

    # HyperLiquid's fundingHistory returns records ASCENDING from startTime,
    # capped at 500/page. Walk FORWARD by advancing startTime past the newest
    # record each page. (The previous loop walked endTime backward with a fixed
    # startTime, so it fetched only the OLDEST 500 records and stopped — leaving
    # every off-scan asset permanently sparse, which then failed the funding
    # completeness gate for every strategy on that asset.)
    start_cursor = cutoff_ms
    while start_cursor < end_time_ms:
        try:
            resp = post_hyperliquid_info({
                "type": "fundingHistory",
                "coin": normalized_asset,
                "startTime": start_cursor,
                "endTime": end_time_ms,
            }, timeout=30)

            if not isinstance(resp, list) or len(resp) == 0:
                log.info("Backfill %s: no more data (page %d)", normalized_asset, pages)
                break

            pages += 1
            total_fetched += len(resp)

            # Build batch rows
            rows = []
            page_newest = start_cursor
            for record in resp:
                funding = record.get("fundingRate")
                ts_ms = record.get("time")
                premium = record.get("premium")
                if funding is None or ts_ms is None:
                    continue

                ts_ms = int(ts_ms)
                ts_iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()

                rows.append((
                    normalized_asset,
                    METRIC_FUNDING_RATE,
                    float(funding),
                    ts_iso,
                    ts_ms,
                    "hyperliquid_history",
                    json.dumps({"premium": premium}) if premium is not None else None,
                ))

                if oldest_ts is None or ts_ms < oldest_ts:
                    oldest_ts = ts_ms
                if ts_ms > page_newest:
                    page_newest = ts_ms

            batch_stored = _store_batch(rows)
            total_stored += batch_stored

            # Paginate forward past the newest record fetched. No progress => done.
            if page_newest <= start_cursor:
                break
            start_cursor = page_newest + 1

            # Rate limit courtesy
            if pages % 5 == 0:
                time.sleep(0.5)

            log.info(
                "Backfill %s page %d: fetched %d, stored %d (through=%s)",
                normalized_asset, pages, len(resp), batch_stored,
                datetime.fromtimestamp(page_newest / 1000, tz=timezone.utc).isoformat(),
            )

            # A short page means we've caught up to the end of available history.
            if len(resp) < batch_size:
                break

        except Exception as exc:
            log.error("Backfill %s page %d failed: %s", normalized_asset, pages, exc)
            break

    result = {
        "asset": normalized_asset,
        "days_back": days_back,
        "pages": pages,
        "total_fetched": total_fetched,
        "total_stored": total_stored,
        "oldest_record": (
            datetime.fromtimestamp(oldest_ts / 1000, tz=timezone.utc).isoformat()
            if oldest_ts else None
        ),
        "completed_at": _now_iso(),
    }

    log.info(
        "Backfill %s complete: %d records stored across %d pages (oldest=%s)",
        normalized_asset, total_stored, pages, result["oldest_record"],
    )
    log_activity("info", "market-data-collector",
                 f"Backfill {normalized_asset}: {total_stored} funding records", result)

    return result


def backfill_all(days_back: int = 365) -> dict:
    """Backfill funding history for all tracked assets."""
    results = {}
    for asset in COLLECT_ASSETS:
        results[asset] = backfill_funding_history(asset, days_back=days_back)
    return results


# ── Self-Healing Backfill ───────────────────────────────────────────────────


def get_funding_coverage_bounds(asset: str) -> tuple[int | None, int | None]:
    """Return (earliest_ms, latest_ms) of stored funding records for an asset."""
    try:
        with get_db() as conn:
            row = conn.execute(
                """SELECT MIN(timestamp_ms) AS earliest, MAX(timestamp_ms) AS latest
                   FROM market_data_history
                   WHERE asset = ? AND metric_type = ?""",
                (asset.strip().upper(), METRIC_FUNDING_RATE),
            ).fetchone()
    except Exception as exc:
        log.debug("Funding coverage bounds query failed for %s: %s", asset, exc)
        return None, None
    if not row or row["earliest"] is None:
        return None, None
    return int(row["earliest"]), int(row["latest"])


def get_funding_record_count(asset: str, start_ms: int, end_ms: int) -> int:
    """Count stored funding records for an asset within [start_ms, end_ms]."""
    try:
        with get_db() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM market_data_history
                   WHERE asset = ? AND metric_type = ? AND timestamp_ms BETWEEN ? AND ?""",
                (asset.strip().upper(), METRIC_FUNDING_RATE, int(start_ms), int(end_ms)),
            ).fetchone()
        return int(row["n"]) if row and row["n"] is not None else 0
    except Exception:
        return 0


def _load_backfill_state() -> dict:
    state = kv_get(_FUNDING_BACKFILL_STATE_KEY, {})
    return state if isinstance(state, dict) else {}


def ensure_funding_history(
    asset: str,
    start_ms: int,
    *,
    cooldown_hours: float = _FUNDING_BACKFILL_COOLDOWN_HOURS,
) -> dict:
    """Ensure stored funding history reaches back to ``start_ms``; backfill gaps.

    This is the self-healing entry point: callers that are about to consume
    funding history for a time window (backtest enrichment, the scheduled
    reconciliation) call this first, and a fresh install converges to full
    coverage automatically — no operator CLI invocation required.

    Returns a dict with ``action`` of:
    - ``"covered"``    — history already reaches the requested start
    - ``"backfilled"`` — a backfill ran (see ``stored``/``oldest_record``)
    - ``"exhausted"``  — the exchange has no data older than what is stored
    - ``"cooldown"``   — a recent attempt already failed to extend coverage
    - ``"error"``      — the backfill attempt itself failed
    - ``"skipped_unknown_asset"`` — the asset exists nowhere in the lake or the
      symbol registry (a fabricated symbol like MULTI/BASKET leaked in from a
      strategy row); the venue is not asked at all
    """
    normalized_asset = str(asset or "").strip().upper()
    if not normalized_asset:
        return {"action": "error", "error": "empty asset"}

    # SYMBOL-VALID-1 companion: assets are enumerated from strategy symbols, so a
    # fabricated symbol (MULTI/USDT, BASKET/USDT) used to reach this backfill and
    # hammer the venue every cooldown lapse (observed: repeated 429/500 pages,
    # every 6h, forever). An asset with no lake dir and no registry row cannot
    # have funding history — skip without a network call. Fails open when the
    # system has no lake/registry evidence at all (fresh install).
    try:
        from forven.dataeng.coverage import known_base_asset

        if not known_base_asset(normalized_asset):
            log.info(
                "Funding backfill skipped for %s: unknown asset (no lake data, "
                "no registry row)", normalized_asset,
            )
            return {"action": "skipped_unknown_asset", "asset": normalized_asset}
    except Exception:
        pass
    start_ms = int(start_ms)
    now = datetime.now(timezone.utc)

    earliest_ms, latest_ms = get_funding_coverage_bounds(normalized_asset)
    now_ms = int(now.timestamp() * 1000)
    if earliest_ms is not None and earliest_ms <= start_ms and latest_ms is not None:
        # Beyond reaching back far enough, require freshness AND density. The old
        # earliest-only check let stale/sparse series pass as covered while the
        # backtest enrichment actually saw mostly-NaN funding.
        fresh = latest_ms >= now_ms - int(_FUNDING_RECENCY_HOURS * 3600 * 1000)
        span_ms = max(1, now_ms - start_ms)
        expected = span_ms / (_FUNDING_EXPECTED_INTERVAL_HOURS * 3600 * 1000)
        have = get_funding_record_count(normalized_asset, start_ms, now_ms)
        dense = have >= _FUNDING_DENSITY_FLOOR * expected
        if fresh and dense:
            return {
                "action": "covered",
                "asset": normalized_asset,
                "earliest_ms": earliest_ms,
                "latest_ms": latest_ms,
                "records": have,
            }

    state = _load_backfill_state()
    entry = state.get(normalized_asset) if isinstance(state.get(normalized_asset), dict) else {}

    # If a previous backfill established the exchange's oldest reachable record
    # and our store already starts there, asking again cannot help.
    oldest_reachable = entry.get("oldest_reachable_ms")
    if (
        oldest_reachable is not None
        and earliest_ms is not None
        and int(oldest_reachable) >= start_ms
        and earliest_ms <= int(oldest_reachable)
    ):
        return {
            "action": "exhausted",
            "asset": normalized_asset,
            "earliest_ms": earliest_ms,
            "oldest_reachable_ms": int(oldest_reachable),
        }

    attempted_at = entry.get("attempted_at")
    if attempted_at:
        try:
            attempted_dt = datetime.fromisoformat(str(attempted_at))
            if attempted_dt.tzinfo is None:
                attempted_dt = attempted_dt.replace(tzinfo=timezone.utc)
            age_hours = (now - attempted_dt).total_seconds() / 3600
            # Cooldown only applies when the previous attempt did not extend
            # coverage far enough — i.e. we'd be retrying the same gap.
            last_target = entry.get("target_start_ms")
            if age_hours < cooldown_hours and last_target is not None and int(last_target) <= start_ms:
                return {"action": "cooldown", "asset": normalized_asset, "age_hours": round(age_hours, 2)}
        except Exception:
            pass

    days_back = max(1, int((now.timestamp() * 1000 - start_ms) / 86400000) + 1)
    try:
        result = backfill_funding_history(normalized_asset, days_back=days_back)
    except Exception as exc:
        log.warning("Self-healing funding backfill failed for %s: %s", normalized_asset, exc)
        return {"action": "error", "asset": normalized_asset, "error": str(exc)}

    new_earliest_ms, _ = get_funding_coverage_bounds(normalized_asset)
    entry = {
        "attempted_at": now.isoformat(),
        "target_start_ms": start_ms,
    }
    # If the backfill ran to completion but still doesn't reach the requested
    # start, the exchange has nothing older — record that bound.
    if new_earliest_ms is not None and new_earliest_ms > start_ms:
        entry["oldest_reachable_ms"] = new_earliest_ms
    state[normalized_asset] = entry
    try:
        kv_set(_FUNDING_BACKFILL_STATE_KEY, state)
    except Exception:
        pass

    return {
        "action": "backfilled",
        "asset": normalized_asset,
        "days_back": days_back,
        "stored": result.get("total_stored"),
        "oldest_record": result.get("oldest_record"),
        "earliest_ms": new_earliest_ms,
    }


def _pipeline_scan_assets() -> list[str]:
    """Assets the autopilot scans (normalized), falling back to COLLECT_ASSETS."""
    assets: list[str] = []
    try:
        payload = kv_get("forven:pipeline:settings", {}) or {}
        symbols = payload.get("autopilot_scan_symbols") if isinstance(payload, dict) else None
        for symbol in symbols or []:
            normalized = str(symbol or "").split("/", 1)[0].strip().upper()
            if normalized and normalized not in assets:
                assets.append(normalized)
    except Exception:
        pass
    for asset in COLLECT_ASSETS:
        if asset not in assets:
            assets.append(asset)
    return assets


def _strategy_universe_assets() -> list[str]:
    """Base assets that strategies are actually configured to trade.

    The funding reconcile must cover these, not just the scan set — otherwise any
    strategy on an off-scan asset (BNB, XRP, AVAX, ...) backtests funding-blind
    and gets held out of live by the funding-completeness gate. Normalizes
    BTC/USDT, BTC-USDT, BTCUSDT -> BTC and drops placeholder/quote junk.
    """
    assets: list[str] = []
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM strategies "
                "WHERE symbol IS NOT NULL AND TRIM(symbol) <> ''"
            ).fetchall()
        for row in rows:
            base = str(row[0] or "").split("/", 1)[0].split("-", 1)[0].strip().upper()
            for quote in ("USDT", "USDC", "USD"):
                if base.endswith(quote) and len(base) > len(quote):
                    base = base[: -len(quote)]
                    break
            if base and base not in {"GENERIC", "CRYPTO", "USDT", "USDC", "USD"} and base not in assets:
                assets.append(base)
    except Exception:
        log.debug("funding reconcile: could not enumerate strategy assets", exc_info=True)
    return assets


def _funding_reconcile_assets() -> list[str]:
    """Union of the scan set and every asset strategies actually trade."""
    assets = _pipeline_scan_assets()
    for asset in _strategy_universe_assets():
        if asset not in assets:
            assets.append(asset)
    return assets


def reconcile_funding_history(
    target_days: int = DEFAULT_FUNDING_TARGET_DAYS,
    assets: list[str] | None = None,
) -> dict:
    """Keep historical funding coverage filled for all traded assets.

    Runs on a schedule so fresh installs (and factory resets) converge to
    ``target_days`` of funding history without any operator action. Covers the
    scan set AND every asset strategies are configured to trade.
    """
    resolved_assets = assets if assets is not None else _funding_reconcile_assets()
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=int(target_days))).timestamp() * 1000)
    results: dict[str, dict] = {}
    backfilled = 0
    for asset in resolved_assets:
        outcome = ensure_funding_history(asset, start_ms)
        results[asset] = outcome
        if outcome.get("action") == "backfilled":
            backfilled += 1
    summary = {
        "target_days": int(target_days),
        "assets": resolved_assets,
        "backfilled": backfilled,
        "results": results,
        "completed_at": _now_iso(),
    }
    if backfilled:
        log_activity(
            "info",
            "market-data-collector",
            f"Funding history reconciliation backfilled {backfilled} asset(s)",
            summary,
        )
    return summary


# ── Query Functions ─────────────────────────────────────────────────────────


def get_metric_history(
    asset: str,
    metric_type: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = 10000,
) -> list[dict]:
    """Query historical metric data for backtesting or analysis."""
    conditions = ["asset = ?", "metric_type = ?"]
    params: list = [asset.upper(), metric_type]

    if start_ms is not None:
        conditions.append("timestamp_ms >= ?")
        params.append(int(start_ms))
    if end_ms is not None:
        conditions.append("timestamp_ms <= ?")
        params.append(int(end_ms))

    where = " AND ".join(conditions)

    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT asset, metric_type, value, timestamp, timestamp_ms, source, extra
                FROM market_data_history
                WHERE {where}
                ORDER BY timestamp_ms ASC
                LIMIT ?""",
            params + [limit],
        ).fetchall()

    return [dict(r) for r in rows]


def get_funding_rate_series(
    asset: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[tuple[int, float]]:
    """Return funding rate as (timestamp_ms, value) pairs for DataFrame joining."""
    rows = get_metric_history(asset, METRIC_FUNDING_RATE, start_ms, end_ms)
    return [(r["timestamp_ms"], r["value"]) for r in rows]


def get_open_interest_series(
    asset: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[tuple[int, float]]:
    """Return OI as (timestamp_ms, value) pairs for DataFrame joining."""
    rows = get_metric_history(asset, METRIC_OPEN_INTEREST, start_ms, end_ms)
    return [(r["timestamp_ms"], r["value"]) for r in rows]


def get_data_coverage(asset: str | None = None) -> dict:
    """Report data coverage — how many records per metric type per asset."""
    try:
        with get_db() as conn:
            conditions = []
            params: list = []
            if asset:
                conditions.append("asset = ?")
                params.append(asset.upper())
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

            rows = conn.execute(
                f"""SELECT asset, metric_type, COUNT(*) as count,
                           MIN(timestamp) as earliest, MAX(timestamp) as latest
                    FROM market_data_history
                    {where}
                    GROUP BY asset, metric_type
                    ORDER BY asset, metric_type""",
                params,
            ).fetchall()
        return {"coverage": [dict(r) for r in rows]}
    except Exception:
        return {"coverage": []}
