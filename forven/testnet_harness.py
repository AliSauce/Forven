"""Testnet end-to-end LIVE execution harness.

Every piece of the live path (kernel execution, stop mirroring, trailing
tightening, fill-now closes, the liquidity guard) was tested in isolation;
nothing exercised the whole order lifecycle together against a real exchange
API. This harness converts "we believe live works" into "live was proven
working this morning": a scheduled run that places a REAL tiny testnet order
and walks the full lifecycle, asserting exchange truth at every leg —

    preflight -> open (market IOC, passes LIQ-1) -> stop mirror (reduce-only
    trigger resting) -> trailing tighten (place-before-cancel, never
    unprotected) -> close (reduce-only, flat) -> no residuals

Safety invariants (each fail-closed):
  * MAINNET REFUSAL — runs only when the configured network resolves to
    testnet; anything else reports "skipped", never places an order.
  * SIM REFUSAL — a sim-clock session would "pass" against mocks; skipped.
  * KILL-SWITCH RESPECT — an active kill switch halts opens for a reason;
    the harness skips rather than trading through a halt.
  * DELTA-ONLY — positions/orders are snapshotted before the run; the harness
    only ever closes the position delta IT created and cancels order ids IT
    placed. Pre-existing testnet positions are never touched.
  * GUARANTEED CLEANUP — a finally-sweep cancels tracked orders and flattens
    the tracked delta even when a leg fails mid-run.

The report (per-leg ok/detail/duration) is persisted to KV
(``forven:testnet_harness:last_run``), logged to the activity feed, and an
error-level activity entry fires on failure.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

log = logging.getLogger("forven.testnet_harness")

HARNESS_KV_KEY = "forven:testnet_harness:last_run"
# Bounded ownership marker: while fresh, the exchange reconciler treats the
# harness's position on this asset as EXPECTED (testnet only) instead of an
# untracked orphan — otherwise the periodic reconcile would arm an emergency
# stop on it (residue the harness doesn't track) and raise a divergence that
# can block live entries. TTL bounds the suppression if the harness dies
# without cleaning up; after expiry the orphan machinery takes over as normal.
HARNESS_ACTIVE_KV_KEY = "forven:testnet_harness:active"
_HARNESS_MARKER_TTL_SECONDS = 600.0
DEFAULT_HARNESS_ASSET = "ETH"
# HL rejects orders under ~$10 notional; small enough to be irrelevant even if
# cleanup somehow failed twice, big enough to clear the minimum with headroom.
DEFAULT_NOTIONAL_USD = 15.0
_POLL_TIMEOUT_SECONDS = 20.0
_POLL_INTERVAL_SECONDS = 1.0
# Initial stop 2% under entry; the tighten moves it to 1% under — the same
# direction (only ever tighter) the live trailing mirror is allowed to move.
_STOP_FRAC = 0.02
_TIGHTENED_STOP_FRAC = 0.01


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mark_harness_active(asset: str) -> None:
    from forven.db import kv_set

    until = time.time() + _HARNESS_MARKER_TTL_SECONDS
    kv_set(HARNESS_ACTIVE_KV_KEY, {"asset": str(asset).upper(), "until_epoch": until})


def _clear_harness_active() -> None:
    from forven.db import kv_set

    kv_set(HARNESS_ACTIVE_KV_KEY, None)


def harness_position_expected(asset: str) -> bool:
    """True while the harness owns a fresh position on ``asset``.

    Called from the exchange reconciler (testnet path only) so the harness's
    short-lived position is not treated as an untracked orphan. Fails closed:
    missing/expired/unparseable marker -> False -> normal orphan handling."""
    from forven.db import kv_get

    marker = kv_get(HARNESS_ACTIVE_KV_KEY, None)
    if not isinstance(marker, dict):
        return False
    if str(marker.get("asset") or "").strip().upper() != str(asset or "").strip().upper():
        return False
    try:
        return time.time() < float(marker.get("until_epoch") or 0.0)
    except (TypeError, ValueError):
        return False


def _position_size(positions_payload: dict, asset: str) -> float:
    """Signed position size (szi) for ``asset`` from a get_positions payload."""
    for entry in (positions_payload or {}).get("positions", []) or []:
        pos = entry.get("position") if isinstance(entry, dict) else None
        if not isinstance(pos, dict):
            continue
        if str(pos.get("coin") or "").strip().upper() != asset:
            continue
        try:
            return float(pos.get("szi") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _open_order_ids(open_orders: list, asset: str) -> set[str]:
    out: set[str] = set()
    for order in open_orders or []:
        if not isinstance(order, dict):
            continue
        if str(order.get("coin") or "").strip().upper() != asset:
            continue
        oid = order.get("oid")
        if oid is not None:
            out.add(str(oid))
    return out


def _poll(predicate, timeout_seconds: float = _POLL_TIMEOUT_SECONDS) -> bool:
    """Poll ``predicate`` (no-arg -> bool) until true or timeout."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            if predicate():
                return True
        except Exception:
            log.debug("testnet harness poll predicate errored", exc_info=True)
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL_SECONDS)


class _LegRecorder:
    def __init__(self) -> None:
        self.legs: list[dict] = []

    def run(self, name: str, fn) -> tuple[bool, object]:
        """Run one leg; record ok/detail/duration. fn returns (ok, detail)."""
        started = time.monotonic()
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"exception: {exc}"
            log.exception("testnet harness leg %r raised", name)
        self.legs.append({
            "leg": name,
            "ok": bool(ok),
            "detail": str(detail),
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        })
        return bool(ok), detail


def _skip_report(reason: str, asset: str) -> dict:
    return {
        "ok": False,
        "status": "skipped",
        "reason": reason,
        "asset": asset,
        "started_at": _now_iso(),
        "finished_at": _now_iso(),
        "legs": [],
    }


def run_testnet_execution_harness(
    asset: str | None = None,
    notional_usd: float | None = None,
) -> dict:
    from forven.db import kv_get, kv_set, log_activity

    asset = str(asset or DEFAULT_HARNESS_ASSET).strip().upper()
    notional = float(notional_usd or DEFAULT_NOTIONAL_USD)
    started_at = _now_iso()

    # ── Refusal gates (no exchange writes past this point unless ALL pass) ──
    from forven.sim.clock import is_sim_active

    if is_sim_active():
        report = _skip_report("sim clock active — a mocked run proves nothing", asset)
        kv_set(HARNESS_KV_KEY, report)
        return report

    from forven.exchange.hyperliquid import resolve_configured_testnet

    if not resolve_configured_testnet(True):
        report = _skip_report("configured network is NOT testnet — harness refuses mainnet", asset)
        kv_set(HARNESS_KV_KEY, report)
        log_activity("warning", "testnet_harness", "Harness skipped: configured network is not testnet")
        return report

    risk_state = kv_get("risk_state", {}) or {}
    if bool(risk_state.get("kill_switch_active")):
        report = _skip_report("kill switch active — not trading through a halt", asset)
        kv_set(HARNESS_KV_KEY, report)
        return report

    from forven.exchange import hyperliquid as hl

    # Route EXACTLY like a real live long open: through the direction book the
    # live opener would pick (master wallet when books are disabled). The master
    # can legitimately sit at $0 while capital lives in the book sub-accounts —
    # trading the routed book is both the funded wallet AND the truer test.
    try:
        from forven.exchange import books as _books

        book_label, book_skip = _books.resolve_open_book("long")
        if book_label is None:
            report = _skip_report(f"no book routes a long open: {book_skip}", asset)
            kv_set(HARNESS_KV_KEY, report)
            return report
        vault = _books.book_address(book_label)
    except Exception:
        log.exception("testnet harness: book resolution failed — using the master wallet")
        book_label, vault = "main", None

    recorder = _LegRecorder()
    tracked_order_ids: set[str] = set()
    opened_size = [0.0]
    baseline_szi = [0.0]

    def _positions_now() -> dict:
        return hl.get_positions(testnet=True, account_address=vault)

    def _open_orders_now() -> list:
        return hl.get_open_orders(testnet=True, account_address=vault)

    def leg_preflight():
        account = hl.get_account_value(testnet=True, require_connection=True, account_address=vault)
        equity = float((account or {}).get("accountValue") or 0.0)
        mid = float((hl.get_all_mids(testnet=True) or {}).get(asset) or 0.0)
        if equity <= 0:
            wallet = vault or "master wallet"
            return False, (
                f"routed testnet wallet ('{book_label}' book, {wallet}) holds $0 — fund it "
                f"via the Hyperliquid testnet faucet before the harness can trade ({account})"
            )
        if mid <= 0:
            return False, f"no mid price for {asset}"
        return True, f"book={book_label} equity=${equity:,.2f} mid={mid:.6g}"

    def leg_baseline():
        baseline_szi[0] = _position_size(_positions_now(), asset)
        existing = _open_order_ids(_open_orders_now(), asset)
        return True, f"baseline szi={baseline_szi[0]} existing_orders={len(existing)}"

    entry_state: dict = {}

    def leg_open():
        mid = float((hl.get_all_mids(testnet=True) or {}).get(asset) or 0.0)
        if mid <= 0:
            return False, f"no mid price for {asset}"
        size = notional / mid
        result = hl.market_order(
            asset, "buy", size, testnet=True, vault_address=vault,
            idempotency_key=f"testnet-harness-{int(time.time())}",
        )
        if result.get("error"):
            return False, f"open rejected: {result['error']}"
        if result.get("fill_price_unknown"):
            return False, "entry IOC returned no confirmed fill (fill_price_unknown)"
        filled = float(result.get("filled_size") or 0.0)
        entry_px = float(result.get("entry_price") or 0.0)
        oid = result.get("entry_order_id")
        if filled <= 0 or entry_px <= 0 or not oid:
            return False, f"entry ack incomplete: filled={filled} px={entry_px} oid={oid}"
        opened_size[0] = filled
        entry_state["entry_price"] = entry_px
        # Exchange truth: the position delta must appear.
        target = baseline_szi[0] + filled
        seen = _poll(lambda: abs(_position_size(_positions_now(), asset) - target) < filled * 0.05)
        if not seen:
            return False, f"position delta +{filled} never appeared on the exchange"
        return True, f"filled {filled} @ {entry_px:.6g} (oid {oid}, liquidity guard passed)"

    def leg_stop_mirror():
        stop_px = entry_state["entry_price"] * (1.0 - _STOP_FRAC)
        result = hl.place_protective_stop(asset, "long", opened_size[0], stop_px, testnet=True, vault_address=vault)
        if result.get("error") or not result.get("stop_order_id"):
            return False, f"stop rejected: {result.get('error') or 'no order id'}"
        oid = str(result["stop_order_id"])
        tracked_order_ids.add(oid)
        entry_state["stop_oid"] = oid
        resting = _poll(lambda: oid in _open_order_ids(_open_orders_now(), asset))
        if not resting:
            return False, f"stop {oid} acked but never appeared in open orders"
        return True, f"reduce-only stop resting @ {result.get('stop_loss')} (oid {oid})"

    def leg_trailing_tighten():
        old_oid = entry_state["stop_oid"]
        new_px = entry_state["entry_price"] * (1.0 - _TIGHTENED_STOP_FRAC)
        # Place-before-cancel: the position must NEVER sit unprotected — the
        # same discipline as the live trailing mirror (LIVE-TRAIL-1).
        result = hl.place_protective_stop(asset, "long", opened_size[0], new_px, testnet=True, vault_address=vault)
        if result.get("error") or not result.get("stop_order_id"):
            return False, f"tightened stop rejected (old stop kept): {result.get('error') or 'no order id'}"
        new_oid = str(result["stop_order_id"])
        tracked_order_ids.add(new_oid)
        cancel = hl.cancel_order(asset, int(old_oid), testnet=True, vault_address=vault)
        if isinstance(cancel, dict) and cancel.get("error"):
            return False, f"old stop cancel failed: {cancel['error']} (two stops resting — cleanup will sweep)"
        entry_state["stop_oid"] = new_oid
        settled = _poll(lambda: (
            (lambda oids: new_oid in oids and old_oid not in oids)(
                _open_order_ids(_open_orders_now(), asset)
            )
        ))
        if not settled:
            return False, f"expected exactly the tightened stop {new_oid} resting; old {old_oid} gone"
        tracked_order_ids.discard(old_oid)
        return True, f"stop tightened {_STOP_FRAC:.0%}->{_TIGHTENED_STOP_FRAC:.0%} under entry (new oid {new_oid})"

    def leg_close():
        # Cancel our protective stop first — a resting reduce-only trigger on a
        # flat position is an orphan a real close never leaves behind.
        stop_oid = entry_state.get("stop_oid")
        if stop_oid:
            cancel = hl.cancel_order(asset, int(stop_oid), testnet=True, vault_address=vault)
            if isinstance(cancel, dict) and cancel.get("error"):
                return False, f"pre-close stop cancel failed: {cancel['error']}"
            tracked_order_ids.discard(stop_oid)
        result = hl.close_position(asset, opened_size[0], "sell", testnet=True, vault_address=vault)
        if result.get("error"):
            return False, f"close rejected: {result['error']}"
        exit_px = result.get("exit_price")
        if not exit_px:
            return False, "close returned no confirmed exit fill"
        flat = _poll(lambda: abs(_position_size(_positions_now(), asset) - baseline_szi[0]) < max(opened_size[0] * 0.05, 1e-9))
        if not flat:
            return False, "position delta did not return to baseline after close"
        opened_size[0] = 0.0
        return True, f"closed @ {float(exit_px):.6g}; position back to baseline"

    def leg_no_residuals():
        leftovers = tracked_order_ids & _open_order_ids(_open_orders_now(), asset)
        if leftovers:
            return False, f"harness orders still resting: {sorted(leftovers)}"
        delta = _position_size(_positions_now(), asset) - baseline_szi[0]
        if abs(delta) > 1e-9 and abs(delta) > opened_size[0] * 0.05:
            return False, f"residual harness position delta {delta}"
        return True, "no residual orders or position delta"

    ordered_legs = (
        ("preflight", leg_preflight),
        ("baseline", leg_baseline),
        ("open", leg_open),
        ("stop_mirror", leg_stop_mirror),
        ("trailing_tighten", leg_trailing_tighten),
        ("close", leg_close),
        ("no_residuals", leg_no_residuals),
    )

    failed = False
    _mark_harness_active(asset)
    try:
        for name, fn in ordered_legs:
            ok, _ = recorder.run(name, fn)
            if not ok:
                failed = True
                break
    finally:
        # ── Guaranteed cleanup: only what WE created. ──
        if failed or opened_size[0] > 0 or tracked_order_ids:
            def leg_cleanup():
                problems: list[str] = []
                for oid in sorted(tracked_order_ids):
                    try:
                        hl.cancel_order(asset, int(oid), testnet=True, vault_address=vault)
                    except Exception as exc:
                        problems.append(f"cancel {oid}: {exc}")
                try:
                    delta = _position_size(_positions_now(), asset) - baseline_szi[0]
                except Exception as exc:
                    return False, f"could not read positions for cleanup: {exc}"
                if delta > 1e-9:
                    try:
                        result = hl.close_position(asset, delta, "sell", testnet=True, vault_address=vault)
                        if isinstance(result, dict) and result.get("error"):
                            problems.append(f"flatten: {result['error']}")
                    except Exception as exc:
                        problems.append(f"flatten: {exc}")
                if problems:
                    return False, "; ".join(problems)
                return True, "tracked orders cancelled; harness delta flat"

            recorder.run("cleanup", leg_cleanup)
        try:
            _clear_harness_active()
        except Exception:
            log.exception("testnet harness: could not clear active marker (TTL will expire it)")

    all_ok = all(leg["ok"] for leg in recorder.legs)
    report = {
        "ok": all_ok,
        "status": "passed" if all_ok else "failed",
        "asset": asset,
        "book": book_label,
        "routed_wallet": vault or "master",
        "notional_usd": notional,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "legs": recorder.legs,
    }
    try:
        kv_set(HARNESS_KV_KEY, report)
        if all_ok:
            log_activity("info", "testnet_harness", f"Testnet execution harness PASSED ({asset}): open -> stop -> tighten -> close all verified")
        else:
            first_bad = next((leg for leg in recorder.legs if not leg["ok"]), {})
            log_activity(
                "error", "testnet_harness",
                f"Testnet execution harness FAILED at leg '{first_bad.get('leg')}': {first_bad.get('detail')}",
            )
    except Exception:
        log.exception("testnet harness: could not persist report")
    return report


def get_last_harness_report() -> dict:
    from forven.db import kv_get

    report = kv_get(HARNESS_KV_KEY, None)
    return report if isinstance(report, dict) else {"status": "never_run"}
