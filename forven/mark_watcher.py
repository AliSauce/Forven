"""Live-tick stop/take-profit watcher — paper price exits fill the moment the
level is touched, like a real exchange resting order.

Hosted by the DAEMON's per-price-message tick (``daemon._run_tick`` →
``check_mark_exits``): each tick compares the fresh marks against the armed
stop/TP levels of every OPEN local paper trade and closes breached ones through
the scanner's own recorded-close paths, stamped at the touch moment. Without
this, price exits were only detected on CLOSED bars — up to a full bar plus
scan lag after a real exchange would have filled (E0010: TP touched ~10:45Z,
executed 11:04Z, stamped 10:00Z).

The scanner's closed-bar monitors (kernel replay, ``_kernel_handle_late_entry_
exits``, ``_kernel_handle_manual_exits``) remain the catch-up backstop for
daemon downtime. ``close_trade_record``'s BEGIN-IMMEDIATE only-if-open
transaction makes the watcher-vs-scan race a harmless no-op, and the paper
reconciler already suppresses re-opening a kernel position whose recorded row
is CLOSED, so an early (intra-bar) close never gets resurrected by the next
scan's replay.

Fills are AT the level — the backtest kernel's own stop/TP convention (TP is a
resting limit; a stop's fee+slippage cost is charged by the close's PnL drag
term) — so parity with the backtest is price-exact; only the timing becomes
honest. Live-stage trades are excluded: their stops rest on the exchange.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("forven.mark_watcher")

# Armed levels are cheap to re-derive but not per-tick cheap: cache with a short
# TTL so level changes (a scan's trailing ratchet refresh, a manual SL edit)
# are picked up within seconds while ticks stay O(open trades) dict lookups.
_ARMED_TTL_SECONDS = 10.0
_armed_cache: dict = {"loaded_at": 0.0, "items": None}


def watcher_enabled() -> bool:
    from forven.scanner import _scanner_bool_setting

    return _scanner_bool_setting("paper_mark_watcher", True)


def invalidate_armed_cache() -> None:
    _armed_cache["loaded_at"] = 0.0
    _armed_cache["items"] = None


def _normalize_asset(value) -> str:
    raw = str(value or "").strip().upper()
    return raw.split("/", 1)[0] if "/" in raw else raw


def _load_armed_trades() -> list[dict]:
    """Every OPEN local paper trade with an enforceable stop/TP, with the level a
    real resting order would sit at."""
    from forven.db import get_db
    from forven.scanner import _coerce_positive_float
    from forven.trade_state import parse_trade_signal_data

    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'OPEN' "
            "AND COALESCE(execution_type, '') IN ('paper', 'paper_challenger')"
        ).fetchall()

    armed: list[dict] = []
    for row in rows:
        trade = dict(row)
        sd = parse_trade_signal_data(trade.get("signal_data"))
        if sd.get("manual_pause") or sd.get("pending_close_reconcile"):
            continue  # detached from auto-management / close already in flight
        kernel_managed = bool(sd.get("kernel_managed"))
        stop_is_manual = str(sd.get("stop_loss_source") or "").strip().lower() == "manual"
        fixed_stop = _coerce_positive_float(sd.get("stop_loss_price"))
        # Armed stop = what a real resting stop order would sit at:
        #  • operator-owned or late hop-in → the recorded (manual / re-anchored) level;
        #  • kernel-managed → the kernel's EFFECTIVE level (fixed ∨ ratcheted
        #    trailing), persisted by each scan's refresh; falls back to the fixed stop.
        stop = fixed_stop
        stop_kind = "stop_loss"
        if kernel_managed and not stop_is_manual and not sd.get("late_entry"):
            effective = _coerce_positive_float(sd.get("effective_stop_price"))
            if effective is not None:
                if fixed_stop is None or effective != fixed_stop:
                    stop_kind = "trailing_stop"
                stop = effective
        target = _coerce_positive_float(sd.get("take_profit_price"))
        if stop is None and target is None:
            continue
        armed.append(
            {
                "trade": trade,
                "asset": _normalize_asset(trade.get("asset")),
                "direction": "short"
                if str(trade.get("direction") or "long").strip().lower() == "short"
                else "long",
                "stop": stop,
                "stop_kind": stop_kind,
                "target": target,
                "kernel_managed": kernel_managed,
            }
        )
    return armed


def _armed_trades(now: float) -> list[dict]:
    items = _armed_cache["items"]
    if items is None or now - float(_armed_cache["loaded_at"]) > _ARMED_TTL_SECONDS:
        items = _load_armed_trades()
        _armed_cache["items"] = items
        _armed_cache["loaded_at"] = now
    return items


def _breach(direction: str, stop, target, mark: float):
    """(exit_reason, fill_price) when the mark touches a level, else None.

    Stop before target — the kernel's own intrabar precedence. Fills AT the
    level (resting-order semantics, identical to the backtest kernel's stop/TP
    fill convention; the fee+slippage drag is charged by the close's PnL
    recompute)."""
    if direction == "long":
        if stop is not None and mark <= stop:
            return ("stop", float(stop))
        if target is not None and mark >= target:
            return ("take_profit", float(target))
    else:
        if stop is not None and mark >= stop:
            return ("stop", float(stop))
        if target is not None and mark <= target:
            return ("take_profit", float(target))
    return None


def _close_at_mark(item: dict, reason: str, fill: float, mark: float) -> str | None:
    from forven.db import get_db
    from forven.sim.clock import get_now

    trade = item["trade"]
    direction = item["direction"]
    trade_id = str(trade.get("id"))
    # Freshness guard: the armed snapshot can be ~10s stale — a scan may have just
    # closed this trade. Re-read status so the fill writer never re-stamps a CLOSED
    # row's exit fill (close_trade_record itself is already only-if-open atomic).
    with get_db() as conn:
        cur = conn.execute("SELECT status FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if not cur or str(cur["status"] or "").strip().upper() != "OPEN":
        return None
    now_iso = get_now().isoformat()

    if item["kernel_managed"]:
        from forven.scanner import _kernel_close_recorded

        strat_id = str(trade.get("strategy_id") or trade.get("strategy") or "")
        strat = {"asset": trade.get("asset"), "timeframe": trade.get("timeframe")}
        synthetic = {
            "exit_price": fill,
            "expected_exit_price": fill,
            "pnl_pct": 0.0,
            "exit_reason": reason,
        }
        return _kernel_close_recorded(
            strat_id, strat, trade, synthetic, direction,
            current_price=float(mark), current_time=now_iso,
            timeframe=str(trade.get("timeframe") or "").strip().lower() or None,
            mark_fill=True,
        )

    # Manual position: same close semantics as _kernel_handle_manual_exits
    # (margin-gross PnL via _close_trade_db), filled at the touch instead of the
    # next closed bar.
    from forven.scanner import _close_trade_db, _coerce_positive_float, _update_trade_fill

    entry_price = _coerce_positive_float(
        trade.get("fill_entry_price") or trade.get("entry_price") or trade.get("signal_entry_price")
    ) or fill
    leverage = _coerce_positive_float(trade.get("leverage")) or 1.0
    size = _coerce_positive_float(trade.get("size")) or 0.0
    signed = 1.0 if direction != "short" else -1.0
    pnl_pct = ((fill - entry_price) / entry_price) * signed * leverage
    pnl_usd = (fill - entry_price) * size * signed  # units embed leverage
    _update_trade_fill(
        trade_id=trade_id, fill_price=fill, fill_kind="exit", signal_price=fill, mark_price=mark
    )
    _close_trade_db(trade_id, fill, pnl_pct, pnl_usd, close_reason=reason)
    return f"MARK-{reason.upper()} {trade.get('asset')} {direction} @ {fill:.6g}"


def check_mark_exits(prices: dict) -> list[str]:
    """Daemon tick entry point: fresh ``{COIN: mark}`` prices → close messages.

    Never raises — a watcher fault must not stall the price feed. Errors are
    throttle-logged; a single bad trade row skips, not aborts, the pass."""
    if not isinstance(prices, dict) or not prices:
        return []
    try:
        if not watcher_enabled():
            return []
        now = time.time()
        armed = _armed_trades(now)
    except Exception as exc:
        log.warning("mark watcher: armed-level load failed: %s", exc)
        return []
    if not armed:
        return []

    marks: dict[str, float] = {}
    for key, value in prices.items():
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price > 0:
            marks[_normalize_asset(key)] = price

    out: list[str] = []
    closed_any = False
    for item in armed:
        mark = marks.get(item["asset"])
        if mark is None:
            continue
        hit = _breach(item["direction"], item["stop"], item["target"], mark)
        if hit is None:
            continue
        reason, fill = hit
        if reason == "stop":
            reason = item.get("stop_kind") or "stop_loss"
        try:
            msg = _close_at_mark(item, reason, fill, mark)
        except Exception as exc:
            log.error(
                "mark watcher: close failed for trade %s (%s %s): %s",
                (item.get("trade") or {}).get("id"), item.get("asset"), reason, exc,
                exc_info=True,
            )
            continue
        closed_any = True
        if msg:
            out.append(msg)
    if closed_any:
        # Reload next tick so a just-closed trade can't re-trigger from the cache.
        invalidate_armed_cache()
    return out
