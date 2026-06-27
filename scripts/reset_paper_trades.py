"""Reset paper trading — wipe the paper/testnet trade history so the system starts
fresh on the corrected engine.

The garbage trades made while the engine was broken (full-notional / drifted signals /
no stops / whipsaw fills) live in the `trades` table; paper sessions and equity are
DERIVED from it, so deleting the paper trades resets the rollup, the per-session
equity (back to the starting capital), and the chart's trade markers automatically.

Keeps: the strategies themselves, their backtest metrics, and REAL live trades.

SAFE: read-only report by default; --apply BACKS UP the DB first, then deletes.

    python scripts/reset_paper_trades.py                 # report what WOULD be deleted
    python scripts/reset_paper_trades.py --apply         # back up + delete paper trades
    python scripts/reset_paper_trades.py --apply --include-live   # also wipe live trades

Tip: hit Emergency Halt / pause the paper service before --apply so the scanner
doesn't open a fresh trade mid-reset.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time

PAPER_TYPES = ("paper", "paper_challenger", "simulation")


def _placeholders(items) -> str:
    return ",".join("?" * len(items))


def _counts(conn, types: list[str]) -> dict:
    ph = _placeholders(types)
    trades = conn.execute(
        f"SELECT COUNT(*) AS c FROM trades WHERE COALESCE(execution_type,'paper') IN ({ph})", types
    ).fetchone()["c"]
    closed = conn.execute(
        f"SELECT COUNT(*) AS c FROM trades WHERE COALESCE(execution_type,'paper') IN ({ph}) AND status='CLOSED'", types
    ).fetchone()["c"]
    open_ = conn.execute(
        f"SELECT COUNT(*) AS c FROM trades WHERE COALESCE(execution_type,'paper') IN ({ph}) AND status='OPEN'", types
    ).fetchone()["c"]
    pos = conn.execute(
        f"SELECT COUNT(*) AS c FROM portfolio_positions WHERE COALESCE(execution_type,'paper') IN ({ph})", types
    ).fetchone()["c"]
    live = conn.execute(
        "SELECT COUNT(*) AS c FROM trades WHERE COALESCE(execution_type,'paper')='live'"
    ).fetchone()["c"]
    signals = conn.execute("SELECT COUNT(*) AS c FROM scanner_signal_results").fetchone()["c"]
    return {"trades": trades, "closed": closed, "open": open_, "positions": pos, "live": live, "signals": signals}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reset paper trading history.")
    ap.add_argument("--apply", action="store_true", help="Back up the DB, then delete (default: dry-run report).")
    ap.add_argument("--include-live", action="store_true", help="Also delete REAL live trades (default: live kept).")
    args = ap.parse_args(argv)

    from forven.db import get_db
    import forven.config as cfg

    types = list(PAPER_TYPES) + (["live"] if args.include_live else [])

    with get_db() as conn:
        c = _counts(conn, types)

    print(f"Scope: {', '.join(types)}")
    print(f"  trades to delete : {c['trades']}  (closed {c['closed']}, open {c['open']})")
    print(f"  risk positions   : {c['positions']}")
    print(f"  signal markers   : {c['signals']} (all cleared)")
    print(f"  LIVE trades      : {c['live']} {'(INCLUDED — will be deleted)' if args.include_live else '(kept)'}")

    if not args.apply:
        print("\nDRY RUN — nothing deleted. Re-run with --apply to back up + delete.")
        return 0

    db_path = str(cfg.FORVEN_DB)
    backup = f"{db_path}.bak-{int(time.time())}"
    shutil.copy2(db_path, backup)
    print(f"\nBacked up DB -> {backup}")

    # Stamp the paper-book reset so the kernel's recording window restarts HERE — BEFORE any
    # destructive delete. Without it, a still-old stage_changed_at would make the next scan
    # replay the whole pre-reset history into the fresh book (it backfills every trade since
    # go-live by design). Stamping first means an interrupt can never leave an emptied-but-
    # unstamped book (which would re-flood); the slow ``forven.scanner`` import also happens
    # before the wipe, not between wipe and stamp.
    from forven.db import kv_set
    from forven.scanner import PAPER_BOOK_RESET_KV_KEY
    from forven.sim.clock import get_now
    kv_set(PAPER_BOOK_RESET_KV_KEY, get_now().isoformat())

    ph = _placeholders(types)
    with get_db() as conn:
        conn.execute(f"DELETE FROM portfolio_positions WHERE COALESCE(execution_type,'paper') IN ({ph})", types)
        deleted = conn.execute(f"DELETE FROM trades WHERE COALESCE(execution_type,'paper') IN ({ph})", types).rowcount
        # Orphan cleanup + stale caches that draw on the old runs.
        conn.execute("DELETE FROM portfolio_positions WHERE trade_id NOT IN (SELECT id FROM trades)")
        conn.execute("DELETE FROM trade_slippage_audit WHERE trade_id NOT IN (SELECT id FROM trades)")
        conn.execute("DELETE FROM scanner_signal_results")
        conn.execute("DELETE FROM kv WHERE key = 'pending_post_mortems'")

    print(f"Deleted {deleted} trades + cleared risk slots, signal markers, and stale post-mortems.")
    print("Stamped paper-book go-live = now (kernel records from here; no pre-reset replay).")
    print("Paper sessions/equity reset automatically (derived from trades).")
    print("Restart/refresh the app for a clean slate. Backup retained above if you need to roll back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
