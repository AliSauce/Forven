"""Re-score paper-stage strategies under the new parity engine (Phase 3).

The shared execution kernel now sizes (each strategy's execution_profile, else 1%),
nets fees+slippage, runs the unified single-source signals, and applies real
intrabar stop/TP/trailing/time-stops — so the PERSISTED gauntlet metrics (computed
on the old full-notional / gross / drifted-signal engine) are STALE. This tool
re-runs each paper-stage strategy's confirmation backtest under the new engine and
shows how its metrics change, flagging strategies that no longer look viable.

Backtest data stays on the Binance lake (operator decision); paper trades on
HyperLiquid — the two track closely for major perps. Run source-reconciliation to
watch the divergence.

SAFE BY DEFAULT — a read-only report (no DB writes, no demotions):

    python scripts/rescore_paper_strategies.py            # report only
    python scripts/rescore_paper_strategies.py --apply    # also refresh stored metrics
    python scripts/rescore_paper_strategies.py --json     # machine-readable

Demotion is intentionally NOT automated here: review the report, then demote the
losers through the normal UI flow (which closes their open paper positions correctly).
"""

from __future__ import annotations

import argparse
import json
import sys


# Key metrics we compare old→new. (total_return as a fraction, sharpe, win rate,
# max drawdown as a fraction, trade count.)
_METRIC_KEYS = ("total_return", "sharpe_ratio", "win_rate", "max_drawdown", "num_trades")


def _coerce(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _extract_metrics(result_or_metrics: dict) -> dict:
    """Pull the comparison metrics from a backtest result (handles top-level or a
    nested ``metrics`` dict, and a few key aliases)."""
    if not isinstance(result_or_metrics, dict):
        return {}
    m = result_or_metrics.get("metrics") if isinstance(result_or_metrics.get("metrics"), dict) else result_or_metrics
    aliases = {
        "total_return": ("total_return", "total_return_pct", "return", "net_return"),
        "sharpe_ratio": ("sharpe_ratio", "sharpe"),
        "win_rate": ("win_rate", "winrate"),
        "max_drawdown": ("max_drawdown", "max_dd", "maximum_drawdown"),
        "num_trades": ("total_trades", "num_trades", "trade_count", "n_trades"),
    }
    out: dict = {}
    for key, names in aliases.items():
        for n in names:
            if n in m and m[n] is not None:
                out[key] = _coerce(m[n])
                break
    return out


def classify(new: dict) -> tuple[str, str]:
    """Viability flag for the re-scored metrics (a triage heuristic, NOT the full
    promotion gate). DEAD = no trades; UNPROFITABLE = non-positive return; THIN =
    too few trades to trust; OK otherwise. Review before demoting."""
    n_trades = new.get("num_trades")
    ret = new.get("total_return")
    if n_trades is not None and n_trades <= 0:
        return "DEAD", "produces zero trades under the new engine"
    if ret is not None and ret <= 0:
        return "UNPROFITABLE", f"net return {ret:.2%} <= 0 under realistic sizing+costs"
    if n_trades is not None and n_trades < 10:
        return "THIN", f"only {int(n_trades)} trades — low confidence"
    return "OK", ""


def _load_paper_strategies() -> list[dict]:
    from forven.db import get_db
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, type, runtime_type, symbol, timeframe, params, metrics, stage "
            "FROM strategies WHERE LOWER(COALESCE(stage, status)) IN ('paper', 'paper_trading')"
        ).fetchall()
    return [dict(r) for r in rows]


def _rescore_one(strat: dict) -> dict:
    """Re-run one strategy's confirmation backtest under the new engine. Read-only:
    persist_legacy_run=False, sync_strategy_state=False so nothing is written."""
    from forven.api_core import stage_backtest_duration_days
    from forven.db import _strategy_asset_token
    from forven.strategies.backtest import backtest_strategy

    params = strat.get("params")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = {}
    params = params or {}
    old_metrics = strat.get("metrics")
    if isinstance(old_metrics, str):
        try:
            old_metrics = json.loads(old_metrics)
        except Exception:
            old_metrics = {}

    asset = _strategy_asset_token(strat.get("symbol")) or str(strat.get("symbol") or "")
    timeframe = str(strat.get("timeframe") or "1h").strip().lower() or "1h"
    duration_days = stage_backtest_duration_days("confirmation")
    minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}.get(timeframe, 60)
    bars = max(int(duration_days) * 24 * 60 // minutes, 200)

    result = backtest_strategy(
        strat["id"], asset, str(strat.get("runtime_type") or strat.get("type") or ""),
        params, bars=bars, timeframe=timeframe,
        regime_gate=False, execution_controls=None,  # None → auto-derives the profile (Phase 3 threading)
        persist_legacy_run=False, sync_strategy_state=False,
    )
    new_metrics = _extract_metrics(result)
    verdict, reason = classify(new_metrics)
    return {
        "id": strat["id"], "name": strat.get("name"), "type": strat.get("type"),
        "symbol": strat.get("symbol"), "timeframe": timeframe,
        "old": _extract_metrics({"metrics": old_metrics or {}}),
        "new": new_metrics, "verdict": verdict, "reason": reason,
        "_full_metrics": result.get("metrics") if isinstance(result, dict) else None,
    }


def _apply_metrics(strategy_id: str, full_metrics: dict) -> None:
    from forven.db import get_db
    from forven.sim.clock import get_now
    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET metrics = ?, updated_at = ? WHERE id = ?",
            (json.dumps(full_metrics), get_now().isoformat(), strategy_id),
        )


def _fmt(v, pct=False):
    if v is None:
        return "  —"
    return f"{v:.2%}" if pct else (f"{v:.2f}" if isinstance(v, float) else str(v))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Re-score paper-stage strategies under the new parity engine.")
    ap.add_argument("--apply", action="store_true", help="Refresh the stored metrics with the re-scored values (no demotions).")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of a table.")
    args = ap.parse_args(argv)

    strategies = _load_paper_strategies()
    if not strategies:
        print("No paper-stage strategies found.")
        return 0

    results = []
    for strat in strategies:
        try:
            results.append(_rescore_one(strat))
        except Exception as exc:  # one bad strategy must not abort the sweep
            results.append({"id": strat.get("id"), "name": strat.get("name"), "verdict": "ERROR", "reason": str(exc),
                            "old": {}, "new": {}, "_full_metrics": None})

    if args.apply:
        applied = 0
        for r in results:
            if r.get("_full_metrics") and r["verdict"] != "ERROR":
                _apply_metrics(r["id"], r["_full_metrics"])
                applied += 1
        print(f"Refreshed stored metrics for {applied} strategies.\n")

    if args.json:
        print(json.dumps([{k: v for k, v in r.items() if k != "_full_metrics"} for r in results], indent=2, default=str))
        return 0

    flagged = [r for r in results if r["verdict"] not in ("OK",)]
    print(f"Re-scored {len(results)} paper strategies under the new engine "
          f"({'metrics REFRESHED' if args.apply else 'read-only report'}).\n")
    header = f"{'verdict':<13} {'id':<14} {'type':<18} {'tf':<4} {'ret old→new':<22} {'trades old→new':<16}"
    print(header); print("-" * len(header))
    for r in sorted(results, key=lambda r: r["verdict"] != "OK", reverse=True):
        ret = f"{_fmt(r['old'].get('total_return'), pct=True)} → {_fmt(r['new'].get('total_return'), pct=True)}"
        trades = f"{_fmt(r['old'].get('num_trades'))} → {_fmt(r['new'].get('num_trades'))}"
        print(f"{r['verdict']:<13} {str(r['id'])[:14]:<14} {str(r.get('type'))[:18]:<18} "
              f"{str(r.get('timeframe'))[:4]:<4} {ret:<22} {trades:<16}")
        if r.get("reason"):
            print(f"              ↳ {r['reason']}")
    print(f"\n{len(flagged)} strategy(ies) flagged (DEAD/UNPROFITABLE/THIN/ERROR). "
          f"Review and demote the losers via the UI (it closes their open positions).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
