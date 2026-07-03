"""CORR-1: measured-correlation effective exposure for the live portfolio budget.

The static group cap treats BTC/ETH/SOL longs as one 'crypto_major' bucket with
a fixed ceiling — useful, but blind three ways: correlations DRIFT (majors
decouple in rotations, couple in crashes), CROSS-group pairs correlate, and an
opposing-direction position OFFSETS risk that nominal group math double-counts.

This layer measures rolling log-return correlations from the STORED candle lake
(no network on the admission path) and admits a new live open only while the
directional, correlation-weighted exposure stays under its cap:

    effective = | candidate_signed + Σ_i corr(candidate, asset_i) * signed_i |

Two perfectly correlated longs add like one doubled bet; an uncorrelated pair
contributes nothing to each other; a correlated short against a long offsets.
Same-asset positions use corr=1 by definition. A pair whose correlation cannot
be measured (missing series, too little overlap) falls back to a CONSERVATIVE
default (1.0 — assume they move together), so missing data leans toward
blocking, never toward waving an open through.

Settings (all operator-editable):
  live_correlation_budget_enabled     default True
  live_max_effective_exposure_pct     default 200.0  (% of equity; scale-matched
                                      to the static group cap)
  live_correlation_window_bars        default 720    (1h bars ≈ 30 days)
  live_correlation_missing_default    default 1.0
"""

from __future__ import annotations

import logging
import math
import time

log = logging.getLogger("forven.portfolio_correlation")

CORRELATION_TIMEFRAME = "1h"
DEFAULT_WINDOW_BARS = 720
DEFAULT_MISSING_CORRELATION = 1.0
DEFAULT_MAX_EFFECTIVE_EXPOSURE_PCT = 200.0
# Correlations move over days, not minutes — recompute at most hourly, and keep
# the admission path OFF the parquet lake between refreshes.
_CACHE_TTL_SECONDS = 3600.0
_MIN_OVERLAP_BARS = 48  # below this, a measured correlation is noise — fall back

_returns_cache: dict[tuple[str, int], tuple[float, object]] = {}
_corr_cache: dict[tuple[str, str, int], tuple[float, float | None]] = {}


def clear_correlation_caches() -> None:
    _returns_cache.clear()
    _corr_cache.clear()


def _load_close_series(asset: str):
    """Close series for ``asset`` from the stored lake (symbol-form fallbacks)."""
    from forven.data import load_parquet

    asset_u = str(asset or "").strip().upper()
    for symbol in (f"{asset_u}/USDT", asset_u, f"{asset_u}/USD"):
        try:
            df = load_parquet(symbol, CORRELATION_TIMEFRAME)
        except Exception:
            continue
        if df is None or getattr(df, "empty", True):
            continue
        if "close" in getattr(df, "columns", []):
            return df["close"]
    return None


def _asset_returns(asset: str, window_bars: int):
    """Log returns over the trailing window, TTL-cached. None when unavailable."""
    key = (str(asset).upper(), int(window_bars))
    now = time.time()
    cached = _returns_cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    returns = None
    try:
        closes = _load_close_series(asset)
        if closes is not None and len(closes) >= _MIN_OVERLAP_BARS:
            import numpy as np

            tail = closes.astype(float).tail(int(window_bars) + 1)
            tail = tail[tail > 0].dropna()
            if len(tail) >= _MIN_OVERLAP_BARS:
                # log returns, timestamp index preserved for pair overlap joins
                returns = np.log(tail / tail.shift(1)).dropna()
    except Exception:
        log.debug("correlation: could not load returns for %s", asset, exc_info=True)
        returns = None
    _returns_cache[key] = (now, returns)
    return returns


def pair_correlation(asset_a: str, asset_b: str, window_bars: int = DEFAULT_WINDOW_BARS) -> float | None:
    """Rolling log-return correlation on the pair's overlapping bars.

    None when either series is missing or the overlap is too short to trust —
    the CALLER decides the conservative fallback (this function never guesses)."""
    a = str(asset_a or "").strip().upper()
    b = str(asset_b or "").strip().upper()
    if not a or not b:
        return None
    if a == b:
        return 1.0
    key = (min(a, b), max(a, b), int(window_bars))
    now = time.time()
    cached = _corr_cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    corr: float | None = None
    ra = _asset_returns(a, window_bars)
    rb = _asset_returns(b, window_bars)
    if ra is not None and rb is not None:
        try:
            joined = ra.to_frame("a").join(rb.to_frame("b"), how="inner").dropna()
            if len(joined) >= _MIN_OVERLAP_BARS:
                value = float(joined["a"].corr(joined["b"]))
                if not math.isnan(value):
                    corr = max(-1.0, min(1.0, value))
        except Exception:
            log.debug("correlation: pair %s/%s failed", a, b, exc_info=True)
    _corr_cache[key] = (now, corr)
    return corr


def _float_setting(settings: dict, key: str, default: float) -> float:
    try:
        raw = settings.get(key)
        return float(raw) if raw is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def check_effective_correlated_exposure(
    asset: str,
    direction: str,
    add_notional_usd: float,
    positions: list[dict],
    equity: float,
    settings: dict,
) -> tuple[bool, str]:
    """(allowed, reason) for admitting a new live open under the effective cap."""
    enabled = settings.get("live_correlation_budget_enabled", True)
    if str(enabled).strip().lower() in {"0", "false", "no", "off"}:
        return True, "correlation budget disabled"
    if not equity or equity <= 0:
        return False, "correlation budget: equity unavailable — refusing the live open (fail closed)"

    asset_u = str(asset or "").strip().upper()
    sign = -1.0 if str(direction or "long").strip().lower() == "short" else 1.0
    candidate = sign * max(float(add_notional_usd or 0.0), 0.0)

    window = int(_float_setting(settings, "live_correlation_window_bars", DEFAULT_WINDOW_BARS))
    missing_default = max(-1.0, min(1.0, _float_setting(
        settings, "live_correlation_missing_default", DEFAULT_MISSING_CORRELATION
    )))
    cap_pct = max(_float_setting(
        settings, "live_max_effective_exposure_pct", DEFAULT_MAX_EFFECTIVE_EXPOSURE_PCT
    ), 0.0)
    cap_usd = cap_pct / 100.0 * float(equity)

    effective = candidate
    contributions: list[tuple[str, float, float]] = []  # (asset, corr, weighted usd)
    for pos in positions or []:
        pos_asset = str(pos.get("asset") or "").strip().upper()
        notional = float(pos.get("notional_usd") or 0.0)
        pos_sign = -1.0 if str(pos.get("direction") or "long").strip().lower() == "short" else 1.0
        if not pos_asset or notional <= 0:
            continue
        corr = pair_correlation(asset_u, pos_asset, window)
        if corr is None:
            corr = missing_default
        weighted = corr * pos_sign * notional
        effective += weighted
        contributions.append((pos_asset, corr, weighted))

    if abs(effective) <= cap_usd:
        return True, (
            f"correlation budget OK: effective exposure ${abs(effective):,.0f}/${cap_usd:,.0f}"
        )

    top = sorted(contributions, key=lambda c: abs(c[2]), reverse=True)[:3]
    detail = ", ".join(f"{a} (corr {c:+.2f}, ${w:,.0f})" for a, c, w in top) or "no open positions"
    return False, (
        f"correlation budget: opening {asset_u} {direction} ${abs(candidate):,.0f} would push "
        f"correlation-weighted effective exposure to ${abs(effective):,.0f}, above "
        f"{cap_pct:g}% of equity (${cap_usd:,.0f}). Largest correlated holdings: {detail}. "
        "Several positions moving together are one bet at combined size — this open waits"
    )


def held_pair_correlations(positions: list[dict], settings: dict | None = None) -> dict[str, float | None]:
    """Pairwise correlations across currently held assets, for the risk snapshot."""
    settings = settings or {}
    window = int(_float_setting(settings, "live_correlation_window_bars", DEFAULT_WINDOW_BARS))
    assets = sorted({str(p.get("asset") or "").strip().upper() for p in positions or [] if p.get("asset")})
    out: dict[str, float | None] = {}
    for i, a in enumerate(assets):
        for b in assets[i + 1:]:
            corr = pair_correlation(a, b, window)
            out[f"{a}|{b}"] = round(corr, 4) if corr is not None else None
    return out
