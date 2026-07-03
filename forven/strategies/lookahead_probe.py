"""Registration-time lookahead / data-leak probe.

An AI-generated strategy that uses a future bar in its vectorized
``generate_signals`` (e.g. ``.shift(-1)``) gets 1-bar lookahead and produces
impossible metrics (Sharpe pegged at the +/-10 clamp, profit factor 12-15, win
rate ~79%, thousands-of-percent returns). The promotion gates struggle to catch
this because a uniform leak makes BOTH the IS and OOS slices amazing (so the
IS/OOS-gap overfit detector sees gap ~0) and keeps profit factor high (so the
win-rate trap, which needs PF < 1.2, never fires).

This module catches the bug at the source via a **truncation-invariance probe**:
a genuinely causal signal at bar ``t`` must be identical whether or not bars
*after* ``t`` exist in the frame. If withholding future bars changes the signal
at an interior bar, the strategy reads the future. This is high-precision
(near-zero false positives) -- a correctly written causal strategy is invariant
under right-truncation by construction.

The probe NEVER raises: any error (the strategy throwing, an un-normalizable
payload) returns ``None`` so a probe failure can't block legitimate
registration. The bug is the leak, not the probe.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# Interior bars (counted from the end) at which to compare full-frame vs
# truncated-frame signals. All are well away from the warm-up region at the
# start of the frame so rolling-window NaNs don't cause spurious diffs.
_PROBE_OFFSETS = (60, 40, 20, 5)
_SYNTHETIC_ROWS = 300


def _build_synthetic_ohlcv(rows: int = _SYNTHETIC_ROWS) -> pd.DataFrame:
    """Deterministic synthetic OHLCV frame with the optional order-flow columns.

    Seeded RNG (no global/Date.now randomness) so the probe is reproducible and
    a flaky strategy can't pass by luck on one run and fail on another.
    """
    rng = np.random.default_rng(7)
    n = int(rows)

    index = pd.date_range("2023-01-01", periods=n, freq="1h")

    # Geometric random walk for close (positive, realistically noisy).
    log_returns = rng.normal(loc=0.0, scale=0.01, size=n)
    close = 30_000.0 * np.exp(np.cumsum(log_returns))

    # Derive a sane OHLC envelope around the close path.
    prev_close = np.empty(n, dtype=float)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    open_ = prev_close
    span = np.abs(rng.normal(loc=0.0, scale=0.004, size=n)) * close
    high = np.maximum(open_, close) + span
    low = np.minimum(open_, close) - span
    low = np.maximum(low, 1.0)  # keep strictly positive
    volume = rng.uniform(low=100.0, high=1_000.0, size=n)

    df = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        },
        index=index,
    )

    # Optional enrichment columns the order-flow strategies consume. Plausible
    # non-zero values so a strategy that reads them doesn't divide-by-zero or
    # early-out to an all-False signal (which would hide a leak in those cols).
    df["funding_rate"] = rng.normal(loc=0.0001, scale=0.0002, size=n)
    df["open_interest"] = rng.uniform(low=1e6, high=5e6, size=n)
    df["taker_buy_sell_ratio"] = rng.normal(loc=1.0, scale=0.15, size=n).clip(0.1, 5.0)
    df["ls_ratio"] = rng.normal(loc=1.0, scale=0.15, size=n).clip(0.1, 5.0)
    df["long_liq_usd"] = rng.uniform(low=0.0, high=5e5, size=n)
    df["short_liq_usd"] = rng.uniform(low=0.0, high=5e5, size=n)
    df["liq_imbalance"] = rng.uniform(low=-1.0, high=1.0, size=n)

    return df


def _normalize_to_bool_arrays(payload: object, index: pd.Index) -> dict[str, np.ndarray] | None:
    """Normalize a generate_signals payload to {side: bool ndarray} aligned to index.

    Mirrors how ``backtest._normalize_directional_signal_payload`` /
    ``_resolve_strategy_vectorized_signals`` interpret the payload -- the 2-tuple
    ``(entry, exit)`` (treated as long), the 4-tuple
    ``(long_entries, long_exits, short_entries, short_exits)``, and a
    ``DirectionalSignals`` object. Returns ``None`` if the payload can't be
    interpreted (probe degrades gracefully).
    """
    from forven.strategies.base import DirectionalSignals

    def _coerce(series: object) -> np.ndarray:
        s = pd.Series(series)
        # Align to the frame index when the series carries a comparable index,
        # then fill gaps with False and cast to bool (matches _coerce_bool_series
        # semantics closely enough for a flip-comparison).
        try:
            if isinstance(s.index, pd.DatetimeIndex) or s.index.equals(index):
                s = s.reindex(index)
        except Exception:
            pass
        return s.fillna(False).to_numpy(dtype=bool, na_value=False)

    if isinstance(payload, DirectionalSignals):
        return {
            "long_entries": _coerce(payload.long_entries),
            "long_exits": _coerce(payload.long_exits),
            "short_entries": _coerce(payload.short_entries),
            "short_exits": _coerce(payload.short_exits),
        }
    if isinstance(payload, (tuple, list)) and len(payload) == 4:
        return {
            "long_entries": _coerce(payload[0]),
            "long_exits": _coerce(payload[1]),
            "short_entries": _coerce(payload[2]),
            "short_exits": _coerce(payload[3]),
        }
    if isinstance(payload, (tuple, list)) and len(payload) == 2:
        # 2-tuple is (entries, exits) treated as the long side (mirrors the
        # long_only default in _normalize_directional_signal_payload).
        return {
            "long_entries": _coerce(payload[0]),
            "long_exits": _coerce(payload[1]),
        }
    return None


def detect_lookahead(strategy_obj) -> str | None:
    """Return a rejection reason if ``strategy_obj`` reads future bars, else None.

    Runs a truncation-invariance probe: computes vectorized signals on a full
    synthetic frame, then recomputes on right-truncated frames ``df.iloc[:t+1]``
    for several interior bars ``t`` and checks the signal AT bar ``t`` is
    unchanged. Any flip means the bar-``t`` signal depended on bars after ``t``
    (a lookahead leak, e.g. ``.shift(-1)``).

    Graceful: returns ``None`` (never raises) if the strategy lacks
    ``generate_signals``, throws, or produces an un-normalizable payload -- a
    probe error must not block registration.
    """
    if strategy_obj is None or not hasattr(strategy_obj, "generate_signals"):
        # Nothing to probe vectorized; the per-bar path is checked elsewhere.
        return None

    try:
        df = _build_synthetic_ohlcv()
        index = df.index

        full_payload = strategy_obj.generate_signals(df)
        if full_payload is None:
            return None
        full = _normalize_to_bool_arrays(full_payload, index)
        if full is None:
            return None

        n = len(df)
        for offset in _PROBE_OFFSETS:
            t = n - offset
            if t <= 1 or t >= n:
                continue
            truncated = df.iloc[: t + 1]
            trunc_payload = strategy_obj.generate_signals(truncated)
            if trunc_payload is None:
                continue
            trunc = _normalize_to_bool_arrays(trunc_payload, truncated.index)
            if trunc is None:
                continue

            for side, full_arr in full.items():
                trunc_arr = trunc.get(side)
                if trunc_arr is None:
                    continue
                if t >= len(full_arr) or t >= len(trunc_arr):
                    continue
                if bool(full_arr[t]) != bool(trunc_arr[t]):
                    return (
                        f"Lookahead detected: vectorized signal at bar t=-{offset} "
                        f"changes when future bars are withheld ({side}) -- strategy "
                        f"reads future data (e.g. a .shift(-1)); rejected"
                    )
        return None
    except Exception as exc:  # never block registration on a probe error
        log.warning("Lookahead probe error (treated as inconclusive): %s", exc)
        return None


# Exception types that, when raised from a strategy's OWN module on clean
# synthetic data, are unambiguous authoring bugs rather than a data/engine
# quirk. The canonical case: a per-bar ``generate_signal`` reads ``self.position``
# (or ``self._position`` / ``self.entry_price``), which the engine never injects
# because it owns position state -- so the read raises AttributeError on the
# first fall-through bar and kills the whole backtest with a cryptic "Indicator
# execution failed" three gates later. A correctly written stateless strategy
# NEVER raises these on a valid frame, so blocking on them is near-zero false
# positive. Other exception types (ZeroDivisionError, KeyError, ...) can have
# benign synthetic-data causes, so they stay inconclusive (logged, not blocked).
_CRASH_BLOCK_EXC_TYPES = (AttributeError, NameError)

# Bars (from the end of the synthetic frame) at which to invoke the per-bar
# ``generate_signal`` so both the entry and the fall-through/exit branches run.
_EXEC_PROBE_STEPS = 16


def _strategy_source_file(strategy_obj) -> str | None:
    """Resolved path to the strategy class's source file, or None."""
    import inspect
    from pathlib import Path

    try:
        src = inspect.getsourcefile(type(strategy_obj)) or inspect.getfile(type(strategy_obj))
    except (TypeError, OSError):
        return None
    if not src:
        return None
    try:
        return str(Path(src).resolve())
    except OSError:
        return src


def _raised_in_strategy_module(exc: BaseException, strategy_file: str | None) -> bool:
    """True if ``exc``'s traceback terminates inside the strategy's own source file.

    This distinguishes an authoring bug (the strategy's code raised) from an
    engine/probe fault (which must never block a legitimate registration).
    """
    import traceback
    from pathlib import Path

    if not strategy_file:
        return False
    tb = exc.__traceback__
    last_file = None
    for frame, _lineno in traceback.walk_tb(tb):
        last_file = frame.f_code.co_filename
    if not last_file:
        return False
    try:
        return Path(last_file).resolve() == Path(strategy_file).resolve()
    except OSError:
        return last_file == strategy_file


def detect_execution_crash(strategy_obj) -> str | None:
    """Return a rejection reason if ``strategy_obj`` crashes on a clean run, else None.

    Exercises the per-bar ``generate_signal`` path (which ``detect_lookahead``
    does NOT touch -- it only probes vectorized ``generate_signals``) plus a
    single vectorized call, over a deterministic synthetic frame carrying every
    enrichment column. If the strategy raises an :data:`_CRASH_BLOCK_EXC_TYPES`
    error FROM ITS OWN MODULE, the run is a guaranteed crash on every real
    backtest too, so we return a precise, actionable reason. The most common
    trigger is a stateful read (``self.position``) the engine never provides.

    Graceful by design: a probe-infrastructure fault, or any exception NOT
    originating in the strategy's own file, returns ``None`` (inconclusive) so
    the probe can never block a legitimate registration on its own bug.
    """
    if strategy_obj is None:
        return None

    strategy_file = _strategy_source_file(strategy_obj)

    try:
        df = _build_synthetic_ohlcv()
    except Exception as exc:  # synthetic build should never fail; stay inconclusive
        log.warning("Execution smoke probe setup error (treated as inconclusive): %s", exc)
        return None

    # 1) Vectorized path, if implemented. A crash here fails the shared kernel too.
    if hasattr(strategy_obj, "generate_signals"):
        try:
            strategy_obj.generate_signals(df)
        except _CRASH_BLOCK_EXC_TYPES as exc:
            if _raised_in_strategy_module(exc, strategy_file):
                return _format_crash_reason("generate_signals", exc)
        except Exception:
            pass  # non-targeted exception type: inconclusive, don't block

    # 2) Per-bar path -- what the deterministic slow-path walk actually calls.
    #    Step across the frame so both entry and fall-through/exit branches run.
    if hasattr(strategy_obj, "generate_signal"):
        n = len(df)
        start = min(40, max(2, n // 4))
        step = max(1, (n - start) // _EXEC_PROBE_STEPS)
        for end in range(start, n + 1, step):
            try:
                strategy_obj.generate_signal(df.iloc[:end])
            except _CRASH_BLOCK_EXC_TYPES as exc:
                if _raised_in_strategy_module(exc, strategy_file):
                    return _format_crash_reason("generate_signal", exc)
            except Exception:
                # Non-targeted exception (e.g. a benign synthetic-data edge):
                # inconclusive. Keep walking -- a later bar may hit the real bug.
                continue

    return None


def _format_crash_reason(entry_point: str, exc: BaseException) -> str:
    msg = str(exc)
    hint = ""
    if isinstance(exc, AttributeError) and "has no attribute" in msg:
        # e.g. "'X' object has no attribute 'position'"
        hint = (
            " -- generate_signal must be STATELESS: the engine owns position "
            "state and does NOT inject self.position/self.entry_price. Gate "
            "exits on indicator conditions, not on a tracked position."
        )
    return (
        f"Execution smoke test failed: {type(exc).__name__} in {entry_point} "
        f"on synthetic data ({msg}){hint}; rejected"
    )


__all__ = ["detect_lookahead", "detect_execution_crash"]
