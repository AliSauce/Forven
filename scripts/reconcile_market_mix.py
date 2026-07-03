"""Reconcile spot/futures market mixing in the OHLCV lake (Phase 1).

Historically a crypto series could hold Binance Vision USD-M FUTURES history
(deep backfill) under a Binance SPOT tail (REST keep-alive) — one file, two
venues, a basis discontinuity at the splice. The fetch path is now
perp-canonical going forward; this tool reports the damage per series and,
with --apply, rebuilds each series as a pure USD-M perp history:

    Binance Vision futures monthly archives  ->  full perp history
    + binanceusdm REST tail                  ->  up to the latest closed bar

The old series (cold + tail merged) is preserved next to the file as
``{tf}.parquet.spotmix.bak`` before the swap.

Usage:
    python scripts/reconcile_market_mix.py                # dry-run report, all crypto series
    python scripts/reconcile_market_mix.py --symbol BTC-USDT
    python scripts/reconcile_market_mix.py --apply        # rebuild everything reported
    python scripts/reconcile_market_mix.py --apply --symbol BTC-USDT --timeframe 1h

Notes:
- Run --apply with the backend STOPPED if possible: the dataset lock is
  per-process, so a concurrent keep-alive append can be lost in the swap
  (harmless — it is refetched on the next keep-alive, but avoid the noise).
- Perp history starts at the venue's listing (BTC: 2019-09). Rebuilding a
  major with pre-2019 spot history SHORTENS the series to the perp span —
  that is the point: bars must carry the venue semantics we trade.
- Rebuilding changed bars invalidates backtest baselines: re-baseline after.
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from forven.binance_vision import bv_client  # noqa: E402
from forven.data import (  # noqa: E402
    DATA_DIR,
    _cached_markets,
    _drop_unclosed_bars,
    _fetch_range,
    _get_dataset_lock,
    _normalize_ohlcv_frame,
    _timeframe_to_ms,
    classify_dataset_asset_class,
    get_dataset_market,
    get_dataset_source,
    get_exchange,
    load_parquet,
    reconcile_close_prices,
    save_parquet,
    tail_path,
)

DIVERGENCE_PROBE_BARS = 200
# Above this mean close divergence on the probe window the series is
# unambiguously serving the wrong venue's prices.
DIVERGENCE_FLAG_PCT = 0.05


def _perp_symbol(fs_symbol: str) -> str | None:
    parts = fs_symbol.split("-")
    if len(parts) != 2 or parts[1] not in ("USDT", "USDC"):
        return None
    return f"{parts[0]}/{parts[1]}:{parts[1]}"


def _iter_series(symbol: str | None, timeframe: str | None):
    root = Path(DATA_DIR)
    if not root.exists():
        return
    for sym_dir in sorted(root.iterdir()):
        if not sym_dir.is_dir() or sym_dir.name.startswith("."):
            continue
        if symbol and sym_dir.name != symbol:
            continue
        for pq_file in sorted(sym_dir.glob("*.parquet")):
            tf = pq_file.stem
            if timeframe and tf != timeframe:
                continue
            yield sym_dir.name, tf


def _probe_divergence(fs_symbol: str, tf: str, perp_symbol: str) -> dict:
    """Compare the stored tail window against live perp klines."""
    stored = load_parquet(fs_symbol, tf)
    if stored is None or stored.empty:
        return {"overlap_bars": 0, "max_divergence_pct": 0.0, "mean_divergence_pct": 0.0}
    window = stored.tail(DIVERGENCE_PROBE_BARS)
    start_ms = int(window["timestamp"].iloc[0].timestamp() * 1000)
    end_ms = int(window["timestamp"].iloc[-1].timestamp() * 1000)
    exchange = get_exchange("binanceusdm")
    perp = _fetch_range(exchange, perp_symbol, tf, start_ms, end_ms)
    return reconcile_close_prices(window, perp)


def _rebuild_perp_series(fs_symbol: str, tf: str, perp_symbol: str) -> dict:
    """Full perp rebuild: BV futures archives + binanceusdm REST tail."""
    # Temp in-memory store for the BV backfill (save_fn/load_fn contract).
    store: dict[str, pd.DataFrame] = {}
    lock = threading.Lock()

    def _save(df, _sym, _tf, _source="binance-vision"):
        store["frame"] = _normalize_ohlcv_frame(df)

    def _load(_sym, _tf):
        return store.get("frame")

    def _lock(_sym, _tf):
        return lock

    bv_rows = bv_client.backfill_ohlcv(fs_symbol, tf, None, save_fn=_save, load_fn=_load, lock_fn=_lock)
    frame = store.get("frame")

    # REST tail from the BV end (or from perp inception when BV had nothing).
    tf_ms = _timeframe_to_ms(tf)
    now_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    since_ms = 0
    if frame is not None and not frame.empty:
        since_ms = int(frame["timestamp"].iloc[-1].timestamp() * 1000) + tf_ms
    exchange = get_exchange("binanceusdm")
    tail = _fetch_range(exchange, perp_symbol, tf, since_ms, now_ms)
    if frame is None or frame.empty:
        frame = tail
    elif tail is not None and not tail.empty:
        frame = _normalize_ohlcv_frame(pd.concat([frame, tail], ignore_index=True))
    frame = _drop_unclosed_bars(frame, tf_ms, now_ms)

    if frame is None or frame.empty:
        raise RuntimeError(f"perp rebuild produced no data for {fs_symbol} {tf}")
    return {"frame": frame, "bv_rows": int(bv_rows), "rest_rows": int(len(tail) if tail is not None else 0)}


def _apply_rebuild(fs_symbol: str, tf: str, new_frame: pd.DataFrame) -> dict:
    from forven.data import parquet_path

    cold = parquet_path(fs_symbol, tf)
    backup = Path(str(cold) + ".spotmix.bak")
    with _get_dataset_lock(fs_symbol, tf):
        old = load_parquet(fs_symbol, tf)
        if old is not None and not old.empty:
            # Preserve the full old series (cold + tail merged) before the swap.
            import pyarrow as pa
            import pyarrow.parquet as pq

            pq.write_table(pa.Table.from_pandas(old, preserve_index=False), backup, compression="zstd")
        # save_parquet clears the tail sidecar and stamps source/market.
        save_parquet(new_frame, fs_symbol, tf, source="binanceusdm")
        stray_tail = tail_path(fs_symbol, tf)
        if stray_tail.exists():
            stray_tail.unlink()
    return {
        "old_rows": int(len(old)) if old is not None else 0,
        "new_rows": int(len(new_frame)),
        "backup": str(backup),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", help="filesystem symbol (e.g. BTC-USDT); default: all")
    parser.add_argument("--timeframe", help="timeframe (e.g. 1h); default: all")
    parser.add_argument("--apply", action="store_true", help="rebuild flagged series (default: dry-run report)")
    args = parser.parse_args()

    try:
        usdm_markets = _cached_markets("binanceusdm")
    except Exception as exc:
        print(f"FATAL: cannot load Binance USD-M markets: {exc}")
        return 2

    flagged: list[tuple[str, str, str]] = []
    print(f"{'series':<24} {'source':<16} {'market':<8} {'rows':>9} {'probe mean%':>12}  verdict")
    print("-" * 88)
    for fs_symbol, tf in _iter_series(args.symbol, args.timeframe):
        if classify_dataset_asset_class(fs_symbol) != "crypto":
            print(f"{fs_symbol + ' ' + tf:<24} {'-':<16} {'-':<8} {'-':>9} {'-':>12}  skip (non-crypto)")
            continue
        perp_symbol = _perp_symbol(fs_symbol)
        if perp_symbol is None or perp_symbol not in usdm_markets:
            print(f"{fs_symbol + ' ' + tf:<24} {'-':<16} {'-':<8} {'-':>9} {'-':>12}  skip (no USD-M perp)")
            continue

        source = get_dataset_source(fs_symbol, tf) or "?"
        market = get_dataset_market(fs_symbol, tf) or "unstamped"
        stored = load_parquet(fs_symbol, tf)
        rows = len(stored) if stored is not None else 0
        try:
            probe = _probe_divergence(fs_symbol, tf, perp_symbol)
            mean_div = float(probe.get("mean_divergence_pct") or 0.0)
        except Exception as exc:
            print(f"{fs_symbol + ' ' + tf:<24} {source:<16} {market:<8} {rows:>9} {'?':>12}  probe failed: {exc}")
            continue

        # A series is flagged when its recent window diverges from the perp
        # series (spot-served) OR its provenance is mixed/unstamped legacy.
        mixed = market in ("unstamped", "unknown") or market == "spot" or mean_div > DIVERGENCE_FLAG_PCT
        verdict = "REBUILD" if mixed else "ok (perp)"
        print(f"{fs_symbol + ' ' + tf:<24} {source:<16} {market:<8} {rows:>9} {mean_div:>11.4f}%  {verdict}")
        if mixed:
            flagged.append((fs_symbol, tf, perp_symbol))

    if not flagged:
        print("\nNothing to rebuild.")
        return 0
    if not args.apply:
        print(f"\n{len(flagged)} series flagged. Re-run with --apply to rebuild them "
              f"(old series kept as *.spotmix.bak). Re-baseline backtests afterwards.")
        return 0

    print(f"\nRebuilding {len(flagged)} series as pure USD-M perp history...")
    failures = 0
    for fs_symbol, tf, perp_symbol in flagged:
        try:
            built = _rebuild_perp_series(fs_symbol, tf, perp_symbol)
            result = _apply_rebuild(fs_symbol, tf, built["frame"])
            print(
                f"  {fs_symbol} {tf}: {result['old_rows']:,} -> {result['new_rows']:,} rows "
                f"(BV {built['bv_rows']:,} + REST {built['rest_rows']:,}); backup {result['backup']}"
            )
        except Exception as exc:
            failures += 1
            print(f"  {fs_symbol} {tf}: FAILED — {exc}")
    print(f"\nDone: {len(flagged) - failures} rebuilt, {failures} failed.")
    print("REMINDER: rebuilt bars invalidate existing backtest baselines — re-baseline.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
