"""Trade-ledger blotter backend: filtering, whitelisted sort, and aggregate stats.

Covers get_all_trades / count_trades / get_trades_stats — the data layer behind the
overhauled /all-trades page (stat bar + filterable, sortable blotter).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from forven.db import count_trades, get_all_trades, get_db, get_trades_stats

_BASE = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _insert_trade(
    trade_id: str,
    *,
    asset: str = "BTC",
    direction: str = "long",
    status: str = "CLOSED",
    execution_type: str = "paper",
    strategy: str = "S0001",
    entry_price: float = 100.0,
    size: float = 1.0,
    leverage: float = 1.0,
    pnl_usd: float | None = None,
    minutes: int = 0,
):
    opened = (_BASE + timedelta(minutes=minutes)).isoformat()
    closed = (_BASE + timedelta(minutes=minutes + 30)).isoformat() if status == "CLOSED" else None
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (id, strategy, strategy_id, asset, symbol, direction, entry_price, fill_entry_price,
             size, leverage, status, execution_type, pnl, pnl_usd, opened_at, closed_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id, strategy, strategy, asset, f"{asset}-USD", direction,
                entry_price, entry_price, size, leverage, status, execution_type,
                pnl_usd, pnl_usd, opened, closed, opened,
            ),
        )


def _seed_known_book():
    # 2 wins, 2 losses (closed), 1 open (notional 1000), 1 failed.
    _insert_trade("W1", pnl_usd=100.0, minutes=1)
    _insert_trade("W2", pnl_usd=50.0, minutes=2)
    _insert_trade("L1", pnl_usd=-30.0, minutes=3)
    _insert_trade("L2", pnl_usd=-90.0, minutes=4)
    _insert_trade("O1", status="OPEN", size=10.0, entry_price=100.0, minutes=5)
    _insert_trade("F1", status="FAILED", minutes=6)


def test_stats_math_over_filtered_set(forven_db):
    _seed_known_book()
    s = get_trades_stats()

    assert s["total"] == 6
    assert s["closed_count"] == 4
    assert s["open_count"] == 1
    assert s["failed_count"] == 1
    assert s["wins"] == 2 and s["losses"] == 2 and s["decided"] == 4
    assert s["win_rate"] == 0.5
    assert s["net_pnl"] == 30.0           # 100 + 50 - 30 - 90
    assert s["gross_profit"] == 150.0
    assert s["gross_loss"] == -120.0
    assert s["profit_factor"] == 150.0 / 120.0
    assert s["avg_win"] == 75.0
    assert s["avg_loss"] == -60.0
    assert s["expectancy"] == 7.5         # 30 / 4
    assert s["best"] == 100.0
    assert s["worst"] == -90.0
    assert s["open_exposure"] == 1000.0   # 10 units * $100


def test_stats_undefined_metrics_are_none_not_zero(forven_db):
    # Only winners -> profit_factor undefined (no losses); no closed -> rates None.
    _insert_trade("W1", pnl_usd=100.0, minutes=1)
    s = get_trades_stats()
    assert s["losses"] == 0
    assert s["profit_factor"] is None     # would-be inf -> None for the UI
    assert s["win_rate"] == 1.0
    assert s["avg_loss"] is None


def test_filters_asset_direction_exec_type(forven_db):
    _insert_trade("A", asset="BTC", direction="long", execution_type="paper", pnl_usd=10.0, minutes=1)
    _insert_trade("B", asset="SOL", direction="short", execution_type="live", pnl_usd=20.0, minutes=2)
    _insert_trade("C", asset="BTC", direction="short", execution_type="live", pnl_usd=30.0, minutes=3)

    assert {t["id"] for t in get_all_trades(asset="BTC")} == {"A", "C"}
    assert {t["id"] for t in get_all_trades(direction="short")} == {"B", "C"}
    assert {t["id"] for t in get_all_trades(execution_type="live")} == {"B", "C"}
    assert {t["id"] for t in get_all_trades(asset="BTC", execution_type="live")} == {"C"}
    # count_trades applies the IDENTICAL filter set as the list.
    assert count_trades(asset="BTC") == 2
    assert count_trades(direction="short", execution_type="live") == 2


def test_paper_filter_includes_paper_challenger(forven_db):
    # `paper` and `paper_challenger` are one simulated stage on two engines; the
    # blotter renders both as "Paper", so the "Paper" filter must match either.
    _insert_trade("K", execution_type="paper", pnl_usd=1.0, minutes=1)
    _insert_trade("L", execution_type="paper_challenger", pnl_usd=2.0, minutes=2)
    _insert_trade("M", execution_type="live", pnl_usd=3.0, minutes=3)

    assert {t["id"] for t in get_all_trades(execution_type="paper")} == {"K", "L"}
    assert count_trades(execution_type="paper") == 2
    # An explicit paper_challenger filter still narrows to just that engine.
    assert {t["id"] for t in get_all_trades(execution_type="paper_challenger")} == {"L"}


def test_search_matches_id_and_strategy(forven_db):
    _insert_trade("ZZTOP", strategy="momentum_x", pnl_usd=1.0, minutes=1)
    _insert_trade("OTHER", strategy="reversal_y", pnl_usd=1.0, minutes=2)
    assert {t["id"] for t in get_all_trades(search="zztop")} == {"ZZTOP"}
    assert {t["id"] for t in get_all_trades(search="reversal")} == {"OTHER"}


def test_sort_by_pnl_and_whitelist_fallback(forven_db):
    _insert_trade("LOW", pnl_usd=-50.0, minutes=1)
    _insert_trade("HIGH", pnl_usd=200.0, minutes=2)
    _insert_trade("MID", pnl_usd=10.0, minutes=3)

    desc = [t["id"] for t in get_all_trades(sort="pnl_usd", sort_dir="desc")]
    assert desc[:3] == ["HIGH", "MID", "LOW"]
    asc = [t["id"] for t in get_all_trades(sort="pnl_usd", sort_dir="asc")]
    assert asc[:3] == ["LOW", "MID", "HIGH"]

    # A non-whitelisted sort key must NOT error or inject — it falls back to opened_at.
    rows = get_all_trades(sort="pnl_usd; DROP TABLE trades", sort_dir="desc")
    assert len(rows) == 3
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"] == 3


def test_pagination_and_total(forven_db):
    for i in range(5):
        _insert_trade(f"T{i}", pnl_usd=float(i), minutes=i)
    page = get_all_trades(limit=2, offset=0, sort="pnl_usd", sort_dir="asc")
    assert [t["id"] for t in page] == ["T0", "T1"]
    page2 = get_all_trades(limit=2, offset=2, sort="pnl_usd", sort_dir="asc")
    assert [t["id"] for t in page2] == ["T2", "T3"]
    assert count_trades() == 5
