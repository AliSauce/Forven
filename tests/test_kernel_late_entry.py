"""Late "hop-in": when the kernel still HOLDS a position whose entry predates the recording
window (a still-active signal the scanner missed while the system was off), the paper path
should TAKE that position now — at the current price/time, re-anchoring the stop/target —
instead of leaving it as a chart-only trigger.

Covers all four touch points:
  * reconcile emits a `late_entry` open for a stale-but-held position (only when enabled);
  * the scanner opens at the CURRENT price with a re-anchored stop and the historical
    kernel_entry_time (so later scans REFRESH, not duplicate);
  * the close computes PnL from the recorded (late) entry, not the kernel's historical one;
  * a refresh does not clobber the re-anchored stop with the kernel's historical level.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

import forven.scanner as sc
from forven.db import get_db
from forven.strategies.execution_kernel import KernelResult
from forven.strategies.paper_reconcile import ReconcileAction, reconcile

CUTOFF = "2026-06-26 16:00:00+00:00"
WINDOW = "2026-05-01 00:00:00+00:00"
STALE_ENTRY = "2026-06-01 04:00:00+00:00"   # ~25 days before the cutoff
STRAT = {"asset": "ETH", "params": {"execution_profile": {"risk_per_trade": 0.01}}}


def _kr(open_pos=None, closed=None):
    return KernelResult(closed_trades=closed or [], open_positions=open_pos or {}, closed_gross=[])


def _stale_short_pos():
    return {"entry_time": STALE_ENTRY, "entry_price": 2400.0, "size_fraction": 0.5,
            "stop_price": 2450.0, "entry_bar": 10, "regime": "trend"}


# stop distance preserved as a fraction of entry: (2450-2400)/2400 = 2.0833%
def _expected_short_stop(cur):
    return cur * (1.0 + (2450.0 - 2400.0) / 2400.0)


# ── reconcile ────────────────────────────────────────────────────────────────────────

def test_stale_open_suppressed_when_late_entry_disabled():
    acts = reconcile(_kr(open_pos={"short": _stale_short_pos()}), [], recent_cutoff=CUTOFF, window_start=WINDOW)
    assert [a for a in acts if a.kind == "open"] == []  # default: stale entry is NOT opened


def test_stale_open_emits_late_entry_when_enabled():
    acts = reconcile(_kr(open_pos={"short": _stale_short_pos()}), [],
                     recent_cutoff=CUTOFF, window_start=WINDOW, late_entry=True)
    opens = [a for a in acts if a.kind == "open"]
    assert len(opens) == 1
    assert opens[0].late_entry is True
    assert opens[0].direction == "short"
    assert opens[0].entry_time == STALE_ENTRY  # historical, so the next scan refreshes it


def test_recent_open_is_never_late_even_when_enabled():
    pos = dict(_stale_short_pos(), entry_time="2026-06-27 08:00:00+00:00")  # within the window
    acts = reconcile(_kr(open_pos={"short": pos}), [], recent_cutoff=CUTOFF, window_start=WINDOW, late_entry=True)
    opens = [a for a in acts if a.kind == "open"]
    assert len(opens) == 1 and opens[0].late_entry is False  # recent → faithful kernel open


# ── scanner open (hop in at current price) ─────────────────────────────────────────────

def _open_late(forven_db, current_price=1590.0, current_time="2026-06-27 12:00:00+00:00"):
    action = ReconcileAction("open", "short", STALE_ENTRY, position=_stale_short_pos(), late_entry=True)
    msg = sc._kernel_open_paper_trade("S-LATE", STRAT, action, sizing_equity=10000.0, leverage=1.0,
                                      current_price=current_price, current_time=current_time)
    assert msg and "late hop-in" in msg  # the applier returns a log message, not the id
    with get_db() as c:
        row = dict(c.execute(
            "SELECT * FROM trades WHERE COALESCE(strategy_id, strategy)='S-LATE' AND status='OPEN' "
            "ORDER BY rowid DESC LIMIT 1").fetchone())
    return row["id"], row


def test_late_entry_opens_at_current_price_with_reanchored_stop(forven_db):
    tid, row = _open_late(forven_db)
    sd = json.loads(row["signal_data"])
    assert row["entry_price"] == pytest.approx(1590.0)                 # CURRENT price, not 2400
    assert row["opened_at"] == "2026-06-27T12:00:00+00:00"            # current time
    assert sd["late_entry"] is True
    assert sd["kernel_entry_time"] == STALE_ENTRY                     # historical → reconcile key
    assert sd["stop_loss_price"] == pytest.approx(_expected_short_stop(1590.0), rel=1e-4)
    assert sd["stop_loss_price"] < 1700.0                             # re-anchored near 1623, not 2450


def test_late_entry_close_uses_recorded_entry_not_kernel_pnl(forven_db):
    tid, row = _open_late(forven_db)
    # The kernel's historical short (from 2400) would show a huge +30% at exit 1500; our late
    # short entered at 1590, so its real PnL is only (1590-1500)/1590 ≈ +5.66%.
    kernel_trade = {"exit_price": 1500.0, "pnl_pct": 0.30, "exit_reason": "signal",
                    "exit_time": "2026-06-27 16:00:00+00:00"}
    sc._kernel_close_recorded("S-LATE", STRAT, row, kernel_trade, "short")
    with get_db() as c:
        out = dict(c.execute("SELECT status, pnl_pct, closed_at FROM trades WHERE id=?", (tid,)).fetchone())
    assert out["status"] == "CLOSED"
    assert out["closed_at"] == "2026-06-27T16:00:00+00:00"           # kernel exit-bar time
    assert out["pnl_pct"] == pytest.approx((1590.0 - 1500.0) / 1590.0, rel=0.02)  # from OUR entry
    assert out["pnl_pct"] < 0.10                                      # NOT the kernel's 0.30


def test_refresh_does_not_clobber_late_entry_reanchored_stop(forven_db):
    tid, row = _open_late(forven_db)
    # The kernel pos still carries the HISTORICAL stop (2450); a refresh must leave the
    # re-anchored ~1623 in place.
    kpos = {"entry_time": STALE_ENTRY, "entry_price": 2400.0, "stop_price": 2450.0, "target_price": None}
    sc._kernel_refresh_paper_trade(
        ReconcileAction("refresh", "short", STALE_ENTRY, position=kpos, recorded={"_row": row})
    )
    with get_db() as c:
        sd = json.loads(dict(c.execute("SELECT signal_data FROM trades WHERE id=?", (tid,)).fetchone())["signal_data"])
    assert sd["stop_loss_price"] == pytest.approx(_expected_short_stop(1590.0), rel=1e-4)  # unchanged


# ── RE-ANCHORED STOP IS ENFORCED (the live-faithful exit) ─────────────────────────────
# A late hop-in must actually CLOSE at its re-anchored stop/target — a live resting order
# would — not ride to the kernel's far-away historical stop. These cover the monitor, the
# deferral of the kernel's historical price-stops, the orphan-skip, and the re-hop guard.

def _make_df(rows):
    """rows = [(iso_time, open, high, low, close), ...] → OHLC frame with a UTC index."""
    idx = pd.to_datetime([r[0] for r in rows], utc=True)
    return pd.DataFrame(
        {"open": [r[1] for r in rows], "high": [r[2] for r in rows],
         "low": [r[3] for r in rows], "close": [r[4] for r in rows]},
        index=idx,
    )


def _long_pos():
    # historical long @1500, stop 1470 (2%), target 1560 (4%); re-anchored to a 1590 hop-in:
    # stop = 1590*(1-0.02) = 1558.2, target = 1590*(1+0.04) = 1653.6.
    return {"entry_time": STALE_ENTRY, "entry_price": 1500.0, "size_fraction": 0.5,
            "stop_price": 1470.0, "target_price": 1560.0, "entry_bar": 10, "regime": "trend"}


def _open_late_long(forven_db, current_price=1590.0, current_time="2026-06-27 12:00:00+00:00"):
    action = ReconcileAction("open", "long", STALE_ENTRY, position=_long_pos(), late_entry=True)
    msg = sc._kernel_open_paper_trade("S-LATEL", STRAT, action, sizing_equity=10000.0, leverage=1.0,
                                      current_price=current_price, current_time=current_time)
    assert msg and "late hop-in" in msg
    with get_db() as c:
        row = dict(c.execute(
            "SELECT * FROM trades WHERE COALESCE(strategy_id, strategy)='S-LATEL' AND status='OPEN' "
            "ORDER BY rowid DESC LIMIT 1").fetchone())
    return row["id"], row


def test_monitor_closes_short_at_reanchored_stop(forven_db):
    tid, _ = _open_late(forven_db)  # short @1590, re-anchored stop ~1623.125 (ABOVE entry)
    df = _make_df([
        ("2026-06-27 12:00:00+00:00", 1590, 1595, 1585, 1592),  # entry bar — excluded
        ("2026-06-27 13:00:00+00:00", 1600, 1610, 1595, 1605),  # high 1610 < 1623 → no breach
        ("2026-06-27 14:00:00+00:00", 1620, 1630, 1615, 1628),  # high 1630 ≥ 1623 → STOP
        ("2026-06-27 15:00:00+00:00", 1628, 1640, 1620, 1635),
    ])
    msgs = sc._kernel_handle_late_entry_exits("S-LATE", STRAT, df)
    assert msgs  # closed
    with get_db() as c:
        out = dict(c.execute(
            "SELECT status, closed_at, pnl_pct, fill_exit_price FROM trades WHERE id=?", (tid,)).fetchone())
    assert out["status"] == "CLOSED"
    assert out["closed_at"] == "2026-06-27T14:00:00+00:00"        # the breach bar, not scan time
    assert out["fill_exit_price"] == pytest.approx(_expected_short_stop(1590.0), rel=1e-4)  # at the stop
    assert out["pnl_pct"] < 0                                      # short stopped out above entry → loss


def test_monitor_holds_when_reanchored_stop_not_touched(forven_db):
    tid, _ = _open_late(forven_db)  # short @1590, stop ~1623.125
    df = _make_df([
        ("2026-06-27 12:00:00+00:00", 1590, 1595, 1585, 1592),
        ("2026-06-27 13:00:00+00:00", 1580, 1600, 1560, 1575),  # high 1600 < 1623 → no breach
        ("2026-06-27 14:00:00+00:00", 1570, 1610, 1550, 1560),  # high 1610 < 1623 → no breach
    ])
    assert sc._kernel_handle_late_entry_exits("S-LATE", STRAT, df) == []
    with get_db() as c:
        assert dict(c.execute("SELECT status FROM trades WHERE id=?", (tid,)).fetchone())["status"] == "OPEN"


def test_monitor_closes_long_at_reanchored_stop(forven_db):
    tid, _ = _open_late_long(forven_db)  # long @1590, re-anchored stop 1558.2 (BELOW entry)
    df = _make_df([
        ("2026-06-27 12:00:00+00:00", 1590, 1595, 1585, 1592),
        ("2026-06-27 13:00:00+00:00", 1585, 1592, 1575, 1580),  # low 1575 > 1558.2 → no breach
        ("2026-06-27 14:00:00+00:00", 1565, 1570, 1550, 1555),  # low 1550 ≤ 1558.2 → STOP (open 1565 > stop)
    ])
    sc._kernel_handle_late_entry_exits("S-LATEL", STRAT, df)
    with get_db() as c:
        out = dict(c.execute(
            "SELECT status, closed_at, pnl_pct, fill_exit_price FROM trades WHERE id=?", (tid,)).fetchone())
    assert out["status"] == "CLOSED"
    assert out["closed_at"] == "2026-06-27T14:00:00+00:00"
    assert out["fill_exit_price"] == pytest.approx(1558.2, rel=1e-4)  # at the re-anchored stop, not 1470
    assert out["pnl_pct"] < 0


def test_monitor_closes_long_at_reanchored_target(forven_db):
    tid, _ = _open_late_long(forven_db)  # long @1590, re-anchored target 1653.6
    df = _make_df([
        ("2026-06-27 12:00:00+00:00", 1590, 1595, 1585, 1592),
        ("2026-06-27 13:00:00+00:00", 1600, 1650, 1595, 1640),  # high 1650 < 1653.6 → no breach
        ("2026-06-27 14:00:00+00:00", 1645, 1660, 1640, 1655),  # high 1660 ≥ 1653.6 → TAKE-PROFIT
    ])
    sc._kernel_handle_late_entry_exits("S-LATEL", STRAT, df)
    with get_db() as c:
        out = dict(c.execute(
            "SELECT status, closed_at, pnl_pct, fill_exit_price FROM trades WHERE id=?", (tid,)).fetchone())
    assert out["status"] == "CLOSED"
    assert out["closed_at"] == "2026-06-27T14:00:00+00:00"
    assert out["fill_exit_price"] == pytest.approx(1653.6, rel=1e-4)
    assert out["pnl_pct"] > 0                                      # long hit its target → gain


def test_kernel_close_defers_late_historical_price_stop(forven_db):
    """The kernel's HISTORICAL stop must NOT close a late hop-in — its re-anchored stop owns
    price exits. The reconciler 'close' for a price-stop reason is deferred (returns None)."""
    tid, row = _open_late(forven_db)
    action = ReconcileAction(
        "close", "short", STALE_ENTRY, recorded={"_row": row},
        trade={"exit_price": 2450.0, "pnl_pct": -0.30, "exit_reason": "stop_loss",
               "exit_time": "2026-06-27 16:00:00+00:00"},
    )
    assert sc._kernel_close_paper_trade("S-LATE", STRAT, action) is None  # deferred
    with get_db() as c:
        assert dict(c.execute("SELECT status FROM trades WHERE id=?", (tid,)).fetchone())["status"] == "OPEN"


def test_kernel_close_honors_late_signal_exit(forven_db):
    """A strategy SIGNAL exit DOES close a late hop-in (the strategy decided to get out)."""
    tid, row = _open_late(forven_db)
    action = ReconcileAction(
        "close", "short", STALE_ENTRY, recorded={"_row": row},
        trade={"exit_price": 1500.0, "pnl_pct": 0.30, "exit_reason": "signal",
               "exit_time": "2026-06-27 16:00:00+00:00"},
    )
    msg = sc._kernel_close_paper_trade("S-LATE", STRAT, action)
    assert msg and "late" in msg.lower()
    with get_db() as c:
        out = dict(c.execute("SELECT status, closed_at FROM trades WHERE id=?", (tid,)).fetchone())
    assert out["status"] == "CLOSED"
    assert out["closed_at"] == "2026-06-27T16:00:00+00:00"


def test_orphan_close_skips_late_entry(forven_db):
    """Converge-close must never flatten a late hop-in at the last bar — its re-anchored
    stop (enforced by the monitor) owns the exit, not an arbitrary current price."""
    tid, row = _open_late(forven_db)
    action = ReconcileAction("orphan_close", "short", STALE_ENTRY, recorded={"_row": row})
    assert sc._kernel_close_orphan(action, last_close=1500.0, last_time="2026-06-27 16:00:00+00:00") is None
    with get_db() as c:
        assert dict(c.execute("SELECT status FROM trades WHERE id=?", (tid,)).fetchone())["status"] == "OPEN"


def test_no_rehop_after_reanchored_stop_close():
    """Once a hop-in is recorded (even CLOSED at its re-anchored stop) the reconciler must
    NOT open a second one while the kernel still holds the wider historical position."""
    recorded = [{"direction": "short", "entry_time": STALE_ENTRY, "status": "closed"}]
    acts = reconcile(_kr(open_pos={"short": _stale_short_pos()}), recorded,
                     recent_cutoff=CUTOFF, window_start=WINDOW, late_entry=True)
    assert [a for a in acts if a.kind == "open"] == []  # no re-hop


# ── MANUAL CLOSE / FLIP IS NOT REVERTED (a CLOSED record for a still-held kernel position) ──
# The kernel keeps "holding" a position the user manually closed (or that a late hop-in
# finalized at its re-anchored stop). reconcile must NOT re-open it from the kernel's still-open
# view — that silently reverts the user's action and double-counts PnL.

def test_closed_record_for_held_kernel_position_is_not_reopened():
    """RECENT (within-window) entry: a manual close leaves a CLOSED record for the exact
    (direction, entry_time) the kernel still holds → must NOT re-open."""
    pos = dict(_stale_short_pos(), entry_time="2026-06-27 08:00:00+00:00")  # recent → _recent True
    recorded = [{"direction": "short", "entry_time": "2026-06-27 08:00:00+00:00", "status": "closed"}]
    acts = reconcile(_kr(open_pos={"short": pos}), recorded, recent_cutoff=CUTOFF, window_start=WINDOW)
    assert [a for a in acts if a.kind == "open"] == []  # suppressed, not reverted


def test_new_signal_still_opens_despite_prior_closed_record():
    """Suppression is keyed to the EXACT kernel position: a prior closed record must not block
    a genuinely NEW signal (a different entry_time / bar)."""
    pos = dict(_stale_short_pos(), entry_time="2026-06-27 12:00:00+00:00")  # NEW bar
    recorded = [{"direction": "short", "entry_time": "2026-06-27 08:00:00+00:00", "status": "closed"}]
    acts = reconcile(_kr(open_pos={"short": pos}), recorded, recent_cutoff=CUTOFF, window_start=WINDOW)
    opens = [a for a in acts if a.kind == "open"]
    assert len(opens) == 1 and opens[0].entry_time == "2026-06-27 12:00:00+00:00"


# ── BACKFILL DEDUP (a busy book must not re-record an already-booked kernel round-trip) ──────

def _count_trades(strat_id):
    with get_db() as c:
        return c.execute(
            "SELECT COUNT(*) FROM trades WHERE COALESCE(strategy_id, strategy)=?", (strat_id,)
        ).fetchone()[0]


def test_kernel_trade_exists_detects_recorded_entry(forven_db):
    _open_late(forven_db)  # records a SHORT carrying kernel_entry_time = STALE_ENTRY
    assert sc._kernel_trade_exists("S-LATE", "short", STALE_ENTRY) is True
    assert sc._kernel_trade_exists("S-LATE", "long", STALE_ENTRY) is False         # wrong direction
    assert sc._kernel_trade_exists("S-LATE", "short", "2099-01-01 00:00:00+00:00") is False
    assert sc._kernel_trade_exists("OTHER", "short", STALE_ENTRY) is False          # wrong strategy


def test_backfill_skipped_when_already_recorded(forven_db):
    """A kernel round-trip already booked under its kernel_entry_time must NOT be re-backfilled
    (which would duplicate the CLOSED row and inflate paper equity every scan)."""
    _open_late(forven_db)  # OPEN trade with kernel_entry_time = STALE_ENTRY
    before = _count_trades("S-LATE")
    action = ReconcileAction(
        "backfill", "short", STALE_ENTRY,
        trade={"exit_price": 1500.0, "pnl_pct": 0.05, "exit_reason": "take_profit",
               "exit_time": "2026-06-27 16:00:00+00:00", "entry_price": 1590.0,
               "size_fraction": 0.5, "entry_bar": 10},
    )
    assert sc._kernel_close_paper_trade("S-LATE", STRAT, action) is None  # deduped
    assert _count_trades("S-LATE") == before  # no duplicate row created


# ── TRAILING-ONLY late hop-in (no re-anchored stop) must be CLOSED by the kernel exit ───────

def _open_late_nostop(forven_db, current_price=1590.0, current_time="2026-06-27 12:00:00+00:00"):
    pos = {"entry_time": STALE_ENTRY, "entry_price": 2400.0, "size_fraction": 0.5,
           "stop_price": None, "target_price": None, "entry_bar": 10, "regime": "trend"}
    action = ReconcileAction("open", "short", STALE_ENTRY, position=pos, late_entry=True)
    msg = sc._kernel_open_paper_trade("S-LATE-NS", STRAT, action, sizing_equity=10000.0, leverage=1.0,
                                      current_price=current_price, current_time=current_time)
    assert msg
    with get_db() as c:
        row = dict(c.execute(
            "SELECT * FROM trades WHERE COALESCE(strategy_id, strategy)='S-LATE-NS' AND status='OPEN' "
            "ORDER BY rowid DESC LIMIT 1").fetchone())
    return row["id"], row


def test_trailing_only_late_hopin_price_exit_is_not_deferred(forven_db):
    """A late hop-in with NO re-anchored stop/target (e.g. a trailing-only kernel position) must
    be CLOSED by the kernel's price exit — NOT deferred (the monitor can't protect it, so
    deferring would strand it unprotected, riding to the kernel's far historical stop)."""
    tid, row = _open_late_nostop(forven_db)
    sd = json.loads(row["signal_data"])
    assert sd["late_entry"] is True and sd["stop_loss_price"] is None and sd["take_profit_price"] is None
    action = ReconcileAction(
        "close", "short", STALE_ENTRY, recorded={"_row": row},
        trade={"exit_price": 2450.0, "pnl_pct": -0.02, "exit_reason": "trailing_stop",
               "exit_time": "2026-06-27 16:00:00+00:00"},
    )
    assert sc._kernel_close_paper_trade("S-LATE-NS", STRAT, action) is not None  # closed, NOT deferred
    with get_db() as c:
        assert dict(c.execute("SELECT status FROM trades WHERE id=?", (tid,)).fetchone())["status"] == "CLOSED"


def test_late_hopin_exit_at_or_before_entry_is_deferred(forven_db):
    """A kernel signal/time exit that fills AT/BEFORE the hop-in entry (closed_at <= opened_at)
    would record a backwards, negative-duration trade — defer it (a later bar's exit or the
    re-anchored monitor closes it instead)."""
    tid, row = _open_late(forven_db, current_time="2026-06-27 12:00:00+00:00")
    action = ReconcileAction(
        "close", "short", STALE_ENTRY, recorded={"_row": row},
        trade={"exit_price": 1580.0, "pnl_pct": 0.10, "exit_reason": "signal",
               "exit_time": "2026-06-27 11:00:00+00:00"},  # BEFORE opened_at
    )
    assert sc._kernel_close_paper_trade("S-LATE", STRAT, action) is None  # deferred
    with get_db() as c:
        assert dict(c.execute("SELECT status FROM trades WHERE id=?", (tid,)).fetchone())["status"] == "OPEN"
