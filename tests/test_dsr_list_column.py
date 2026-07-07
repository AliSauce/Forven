"""Forge-list DSR column contract (2026-07-07).

The Deflated Sharpe is expensive to compute (per-trade returns + optimizer
trials per strategy), so the list NEVER computes it. compute_strategy_dsr
write-throughs its result onto the strategies row; the list endpoint just
reads the snapshot. Rows never computed carry NULL and render as '-'.
"""

from __future__ import annotations

from forven.db import get_db


def _insert_strategy(sid: str, *, dsr: float | None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, display_id, name, type, status, stage, owner, "
            "symbol, timeframe, deflated_sharpe) "
            "VALUES (?, ?, ?, 'keltner_coil_x', 'gauntlet', 'gauntlet', 'brain', 'BTC', '1h', ?)",
            (sid, sid, sid, dsr),
        )


def test_migration_adds_snapshot_columns(forven_db):
    with get_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(strategies)").fetchall()}
    assert "deflated_sharpe" in cols
    assert "deflated_sharpe_at" in cols


def test_list_payload_carries_dsr_snapshot(forven_db):
    from forven.strategy_lifecycle import read_strategies

    _insert_strategy("s-with-dsr", dsr=0.9321)
    _insert_strategy("s-without-dsr", dsr=None)

    rows = {r["id"]: r for r in read_strategies(status="gauntlet")}
    assert rows["s-with-dsr"]["deflated_sharpe"] == 0.9321
    # never-computed stays null — the UI renders '-' rather than a fake 0
    assert rows["s-without-dsr"]["deflated_sharpe"] is None
