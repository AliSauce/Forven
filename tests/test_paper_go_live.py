"""Finding-3 fix: the kernel's trade-recording cutoff is anchored to the paper book's
GO-LIVE (``stage_changed_at``, or the last reset) instead of a sliding 6-bar window.

So ANY downtime backfills every trade missed since go-live — a complete round-trip that
opened AND closed during a long outage is no longer dropped — while a fresh/reset book
still never replays its pre-go-live history. The reset script stamps a KV timestamp so a
reset restarts the window (else a still-old ``stage_changed_at`` would replay everything).
"""

from __future__ import annotations


import forven.scanner as sc
from forven.db import kv_set
from forven.sim.clock import get_now
from forven.strategies.execution_kernel import KernelResult
from forven.strategies.paper_reconcile import reconcile


# ── _resolve_paper_go_live ────────────────────────────────────────────────────────────

def test_go_live_uses_stage_changed_at(forven_db):
    kv_set(sc.PAPER_BOOK_RESET_KV_KEY, None)  # no reset stamped
    go = sc._resolve_paper_go_live({"stage_changed_at": "2026-06-01T00:00:00+00:00"})
    assert go is not None and go.isoformat() == "2026-06-01T00:00:00+00:00"


def test_go_live_prefers_later_reset(forven_db):
    kv_set(sc.PAPER_BOOK_RESET_KV_KEY, "2026-06-20T00:00:00+00:00")
    go = sc._resolve_paper_go_live({"stage_changed_at": "2026-06-01T00:00:00+00:00"})
    assert go.isoformat() == "2026-06-20T00:00:00+00:00"  # reset later than stage change → wins


def test_go_live_reset_used_even_without_stage(forven_db):
    kv_set(sc.PAPER_BOOK_RESET_KV_KEY, "2026-06-20T00:00:00+00:00")
    go = sc._resolve_paper_go_live({})  # e.g. a legacy row with no stage_changed_at
    assert go.isoformat() == "2026-06-20T00:00:00+00:00"


def test_go_live_capped_at_now(forven_db):
    kv_set(sc.PAPER_BOOK_RESET_KV_KEY, None)
    go = sc._resolve_paper_go_live({"stage_changed_at": "2999-01-01T00:00:00+00:00"})
    assert go <= get_now()  # a skewed future stamp can't freeze recording


def test_go_live_none_when_no_anchor(forven_db):
    kv_set(sc.PAPER_BOOK_RESET_KV_KEY, None)
    assert sc._resolve_paper_go_live({}) is None  # caller falls back to the bar window


def test_go_live_none_on_unparseable(forven_db):
    kv_set(sc.PAPER_BOOK_RESET_KV_KEY, "not-a-timestamp")
    assert sc._resolve_paper_go_live({"stage_changed_at": "garbage"}) is None


# ── reconcile cutoff behaviour ────────────────────────────────────────────────────────

def test_go_live_cutoff_backfills_offline_roundtrip():
    """A trade that opened AND closed during a long outage (entry far older than 6 bars) is
    backfilled under the go-live cutoff but DROPPED under the old sliding window — the fix."""
    closed = {"direction": "long", "entry_time": "2026-06-10 00:00:00+00:00",
              "exit_time": "2026-06-10 08:00:00+00:00", "entry_price": 100.0, "exit_price": 103.0,
              "pnl_pct": 0.03, "exit_reason": "take_profit", "size_fraction": 0.5, "entry_bar": 50}
    res = KernelResult(closed_trades=[closed])
    # book live since June 1 → the June-10 round-trip is RECORDED no matter the gap length.
    golive = reconcile(res, [], recent_cutoff="2026-06-01T00:00:00+00:00")
    assert [a.kind for a in golive] == ["backfill"]
    # old sliding window (cutoff a recent bar, June 20) → the June-10 trade is DROPPED.
    window = reconcile(res, [], recent_cutoff="2026-06-20T00:00:00+00:00")
    assert [a.kind for a in window] == []


def test_pre_go_live_roundtrip_still_suppressed():
    """A trade entered BEFORE go-live is still pre-tracking (chart trigger only), so a fresh
    book never floods with the strategy's whole would-be history."""
    closed = {"direction": "long", "entry_time": "2026-05-15 00:00:00+00:00",
              "exit_time": "2026-05-15 08:00:00+00:00", "entry_price": 100.0, "exit_price": 103.0,
              "pnl_pct": 0.03, "exit_reason": "take_profit", "size_fraction": 0.5, "entry_bar": 5}
    res = KernelResult(closed_trades=[closed])
    acts = reconcile(res, [], recent_cutoff="2026-06-01T00:00:00+00:00")  # entry predates go-live
    assert [a.kind for a in acts] == []


def test_recent_cutoff_tolerates_format_drift():
    """The cutoff and the kernel entry are the SAME instant but different string formats
    (space vs 'T'). The old raw string compare put ' '(0x20) < 'T'(0x54) → wrongly 'before'
    the cutoff → suppressed a legitimate at-go-live entry. The tolerant parse fixes it."""
    pos = {"entry_time": "2026-06-01 12:00:00+00:00", "entry_price": 100.0, "size_fraction": 0.5,
           "stop_price": 95.0, "entry_bar": 5, "regime": "trend"}
    res = KernelResult(open_positions={"long": pos})
    acts = reconcile(res, [], recent_cutoff="2026-06-01T12:00:00+00:00",
                     window_start="2026-01-01T00:00:00+00:00")
    opens = [a for a in acts if a.kind == "open"]
    assert len(opens) == 1 and not opens[0].late_entry  # same instant ⇒ recent ⇒ faithful open
