"""Phase 2 regression tests for the paper+live engine audit (2026-06-28).

DB-1 / SCANAPPLY-2: the kernel close writes its net equity-fraction pnl +
net_pnl_pct + the pnl_is_equity_fraction parity flag in ONE atomic transaction
(via close_trade_record's pnl_override), so a crash can't leave a CLOSED row at
the wrong (margin) scale or an equity-fraction pnl unflagged.

(PROMOTION-GATE-PARITY-2/3 exclusion is covered in test_paper_gate_s00152.py.)
"""
import json


def test_close_trade_record_pnl_override_is_atomic_and_flagged(forven_db):
    from forven.scanner import _open_trade_db, _update_trade_fill
    from forven.trade_state import close_trade_record
    from forven.db import get_db

    tid = _open_trade_db("s-ov", "BTC", "long", 100.0, 1.0, 0.01, 1.0,
                         {"kernel_managed": True}, execution_type="paper")
    _update_trade_fill(trade_id=tid, fill_price=100.0, fill_kind="entry", signal_price=100.0)
    _update_trade_fill(trade_id=tid, fill_price=110.0, fill_kind="exit", signal_price=110.0)

    close_trade_record(
        tid, signal_exit_price=110.0, exit_price=110.0, close_reason="signal",
        close_price_source="kernel",
        pnl_override={"pnl_pct": 0.0888, "net_pnl_pct": 0.0888, "pnl_usd": 888.0, "equity_fraction": True},
    )

    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT status, pnl_pct, net_pnl_pct, pnl, pnl_usd, signal_data FROM trades WHERE id=?",
            (tid,),
        ).fetchone())

    assert row["status"] == "CLOSED"
    # The net equity-fraction (NOT close_trade_record's gross margin return of +10%) is stored.
    assert abs(row["pnl_pct"] - 0.0888) < 1e-9
    assert abs(row["net_pnl_pct"] - 0.0888) < 1e-9
    assert abs(row["pnl_usd"] - 888.0) < 1e-9
    # ...and the parity flag is set in the SAME write (so the promotion gate counts it).
    assert json.loads(row["signal_data"]).get("pnl_is_equity_fraction") is True


def test_close_trade_record_without_override_keeps_margin_pnl(forven_db):
    # Non-override callers (legacy/manual closes) are UNCHANGED: gross margin pnl, no
    # net_pnl_pct written here, no equity-fraction flag (so the gate excludes them).
    from forven.scanner import _open_trade_db, _update_trade_fill
    from forven.trade_state import close_trade_record
    from forven.db import get_db

    tid = _open_trade_db("s-leg", "BTC", "long", 100.0, 1.0, 0.01, 2.0,
                         {"source": "manual"}, execution_type="paper")
    _update_trade_fill(trade_id=tid, fill_price=100.0, fill_kind="entry", signal_price=100.0)
    close_trade_record(tid, signal_exit_price=110.0, exit_price=110.0, close_reason="manual")

    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT pnl_pct, net_pnl_pct, signal_data FROM trades WHERE id=?", (tid,)
        ).fetchone())
    # +10% price move * 2x leverage = +20% margin return (gross), NOT an equity-fraction.
    assert abs(row["pnl_pct"] - 0.20) < 1e-6
    assert row["net_pnl_pct"] is None
    assert json.loads(row["signal_data"]).get("pnl_is_equity_fraction") is None
