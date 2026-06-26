"""Assign an execution_profile to each paper-stage strategy by OPTIMIZING TOTAL
RETURN over the confirmation backtest (a sizing + stops search).

Why this exists — the gauntlet does NOT do this today (verified in
forven/gauntlet/tasks.py):
  1. its optimization submit leaves ``execution_parameter_ranges = None`` → the
     optimizer never even searches a profile;
  2. ``run_apply_optimized_defaults`` merges only ``best_params`` — the optimizer's
     ``best_execution_profile`` dies in ``backtest_results.config_json`` and is never
     written to ``strategies.params``;
  3. paper/live strategies are param-locked, so the apply step is skipped for them.
And the execution kernel reads sizing ONLY from ``params['execution_profile']``
(forven/strategies/sizing.py:extract_execution_profile) — with no profile a strategy
sizes at the flat 1%-risk default, which is why the re-score showed ~0.1-0.4% returns.

This tool closes that gap directly and transparently: for each strategy it sweeps a
grid of execution profiles through the SHARED KERNEL (backtest_strategy — the same
engine paper/live trade on), scores each by total return, and writes the winning
profile into ``params['execution_profile']`` — the one key the kernel honors.

The search is "sizing + stops" maximizing TOTAL RETURN (operator's choice). Because
total-return optimization always favors the most aggressive sizing in range, the grid
is BOUNDED (``--max-risk``) and near-ruin profiles are rejected (``--max-dd``). The
chosen return is IN-SAMPLE on the confirmation window — review the drawdowns; run a WFA
pass before trusting it with size.

SAFE: dry-run report by default; --apply BACKS UP the DB first, then writes profiles.

    python scripts/assign_execution_profiles.py                 # report only
    python scripts/assign_execution_profiles.py --apply         # back up + write profiles
    python scripts/assign_execution_profiles.py --only S02177   # single strategy (timing/test)
    python scripts/assign_execution_profiles.py --max-risk 0.03 # cap risk/trade (default 0.05)
    python scripts/assign_execution_profiles.py --max-dd 0.70   # reject profiles drawing >70%
    python scripts/assign_execution_profiles.py --json          # machine-readable
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time


def _coerce(v, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f


def _metric(metrics: dict, *names, default=None):
    if not isinstance(metrics, dict):
        return default
    for n in names:
        if n in metrics and metrics[n] is not None:
            v = _coerce(metrics[n])
            if v is not None:
                return v
    return default


def candidate_profiles(max_risk: float, with_tp: bool = True) -> list[dict]:
    """Grid of execution profiles to search (sizing + stops), maximizing total return.

    fraction = risk a % of equity spread over a hard % stop; atr = the same risk spread
    over an ATR-multiple stop. Both are risk-based so the deployed size scales with the
    stop distance. ``risk_per_trade`` is a fraction (0.05 = 5% of equity at risk);
    ``stop_loss_pct`` / ``take_profit_pct`` are percent; ``atr_stop_multiplier`` a
    multiple of ATR. These are exactly the HONORED execution-control fields.
    """
    risks = [r for r in (0.02, 0.03, 0.05) if r <= max_risk + 1e-9]
    if not risks:
        risks = [round(max_risk, 4)]
    tps = [None, 10.0] if with_tp else [None]
    out: list[dict] = []
    for risk in risks:
        for stop in (3.0, 5.0, 8.0):  # fraction: risk over a hard % stop
            for tp in tps:
                p = {"sizing_mode": "fraction", "risk_per_trade": risk, "stop_loss_pct": stop}
                if tp:
                    p["take_profit_pct"] = tp
                out.append(p)
        for mult in (2.0, 3.0):  # atr: risk over an ATR-multiple stop
            for tp in tps:
                p = {"sizing_mode": "atr", "risk_per_trade": risk, "atr_stop_multiplier": mult}
                if tp:
                    p["take_profit_pct"] = tp
                out.append(p)
    return out


def _profile_label(p: dict | None) -> str:
    if not p:
        return "default(1%)"
    mode = p.get("sizing_mode", "?")
    risk = p.get("risk_per_trade")
    bits = [f"{mode}", f"r{risk:.0%}" if isinstance(risk, (int, float)) else ""]
    if "stop_loss_pct" in p:
        bits.append(f"sl{p['stop_loss_pct']:g}%")
    if "atr_stop_multiplier" in p:
        bits.append(f"atr{p['atr_stop_multiplier']:g}x")
    if "take_profit_pct" in p:
        bits.append(f"tp{p['take_profit_pct']:g}%")
    return " ".join(b for b in bits if b)


def _load_paper_strategies(only: str | None) -> list[dict]:
    from forven.db import get_db
    with get_db() as conn:
        if only:
            rows = conn.execute(
                "SELECT id, name, type, runtime_type, symbol, timeframe, params, stage "
                "FROM strategies WHERE id = ?", (only,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, type, runtime_type, symbol, timeframe, params, stage "
                "FROM strategies WHERE LOWER(COALESCE(stage, status)) IN ('paper', 'paper_trading')"
            ).fetchall()
    return [dict(r) for r in rows]


def _bars_for(timeframe: str) -> int:
    from forven.api_core import stage_backtest_duration_days
    duration_days = stage_backtest_duration_days("confirmation")
    minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}.get(timeframe, 60)
    return max(int(duration_days) * 24 * 60 // minutes, 200)


def _run(strat: dict, profile: dict | None) -> dict | None:
    """One confirmation backtest under the shared kernel with the given profile."""
    from forven.db import _strategy_asset_token
    from forven.strategies.backtest import backtest_strategy

    params = strat["_params"]
    asset = _strategy_asset_token(strat.get("symbol")) or str(strat.get("symbol") or "")
    timeframe = strat["_tf"]
    result = backtest_strategy(
        strat["id"], asset, str(strat.get("runtime_type") or strat.get("type") or ""),
        params, bars=_bars_for(timeframe), timeframe=timeframe,
        regime_gate=False, execution_controls=profile,
        persist_legacy_run=False, sync_strategy_state=False,
    )
    if not isinstance(result, dict):
        return None
    m = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    return {
        "total_return": _metric(m, "total_return", "total_return_pct"),
        "max_drawdown": _metric(m, "max_drawdown", "max_dd", "maximum_drawdown"),
        "sharpe": _metric(m, "sharpe_ratio", "sharpe"),
        "trades": _metric(m, "total_trades", "num_trades", "trade_count", default=0),
    }


def optimize_one(strat: dict, *, max_risk: float, max_dd: float, min_trades: int) -> dict:
    """Sweep the grid, return the best-total-return profile passing the guards."""
    baseline = _run(strat, None)  # current behavior: no profile → flat 1%
    grid = candidate_profiles(max_risk)
    scored = []
    for prof in grid:
        r = _run(strat, prof)
        if r is None or r.get("total_return") is None:
            continue
        dd = r.get("max_drawdown")
        trades = r.get("trades") or 0
        ok = (trades >= min_trades) and (dd is None or dd <= max_dd)
        scored.append({"profile": prof, **r, "eligible": ok})

    eligible = [s for s in scored if s["eligible"]]
    pool = eligible or [s for s in scored if (s.get("trades") or 0) >= min_trades] or scored
    best = max(pool, key=lambda s: s["total_return"]) if pool else None

    return {
        "id": strat["id"], "name": strat.get("name"), "type": strat.get("type"),
        "timeframe": strat["_tf"], "stage": strat.get("stage"),
        "baseline": baseline,
        "best": best,
        "n_candidates": len(scored),
        "n_eligible": len(eligible),
    }


def _backup_db() -> str:
    import forven.config as cfg
    db_path = str(cfg.FORVEN_DB)
    backup = f"{db_path}.bak-{int(time.time())}"
    shutil.copy2(db_path, backup)
    return backup


def _write_profile(strategy_id: str, profile: dict) -> None:
    from forven.db import get_db
    from forven.strategies.sizing import normalize_execution_controls
    normalized = normalize_execution_controls(profile) or profile
    with get_db() as conn:
        row = conn.execute("SELECT params FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
        params = {}
        if row and row["params"]:
            try:
                params = json.loads(row["params"]) or {}
            except Exception:
                params = {}
        if not isinstance(params, dict):
            params = {}
        params["execution_profile"] = normalized
        conn.execute(
            "UPDATE strategies SET params = ?, updated_at = datetime('now') WHERE id = ?",
            (json.dumps(params, sort_keys=True), strategy_id),
        )


def _fmt_pct(v):
    return "   —" if v is None else f"{v:.2%}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Optimize + assign execution profiles (total return, sizing+stops).")
    ap.add_argument("--apply", action="store_true", help="Back up the DB, then write the winning profile into each strategy's params.")
    ap.add_argument("--only", help="Restrict to a single strategy id (timing/test).")
    ap.add_argument("--max-risk", type=float, default=0.05, help="Cap on risk_per_trade searched (fraction; default 0.05 = 5%%).")
    ap.add_argument("--max-dd", type=float, default=0.85, help="Reject profiles whose backtest max drawdown exceeds this (fraction; default 0.85).")
    ap.add_argument("--min-trades", type=int, default=5, help="Reject profiles with fewer trades than this (default 5).")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = ap.parse_args(argv)

    strategies = _load_paper_strategies(args.only)
    if not strategies:
        print("No matching strategies found.")
        return 0

    # Pre-parse params/timeframe once per strategy.
    for s in strategies:
        p = s.get("params")
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                p = {}
        s["_params"] = p if isinstance(p, dict) else {}
        s["_tf"] = (str(s.get("timeframe") or "1h").strip().lower() or "1h")

    print(f"Optimizing execution profiles for {len(strategies)} strateg(y/ies) "
          f"[objective=total_return, search=sizing+stops, max_risk={args.max_risk:.0%}, max_dd={args.max_dd:.0%}]")
    print(f"Grid: {len(candidate_profiles(args.max_risk))} profiles/strategy via the shared kernel. This takes a while.\n")

    results = []
    for i, strat in enumerate(strategies, 1):
        try:
            res = optimize_one(strat, max_risk=args.max_risk, max_dd=args.max_dd, min_trades=args.min_trades)
        except Exception as exc:
            res = {"id": strat.get("id"), "name": strat.get("name"), "error": str(exc), "baseline": None, "best": None}
        results.append(res)
        print(f"  [{i}/{len(strategies)}] {res.get('id')} done", flush=True)

    applied = 0
    backup = None
    if args.apply:
        to_write = [r for r in results if r.get("best") and r["best"].get("profile") and not r.get("error")]
        if to_write:
            backup = _backup_db()
            for r in to_write:
                _write_profile(r["id"], r["best"]["profile"])
                applied += 1

    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return 0

    print()
    if backup:
        print(f"Backed up DB -> {backup}")
        print(f"Wrote execution_profile to {applied} strateg(y/ies).\n")
    header = f"{'id':<10} {'tf':<4} {'ret base→opt':<22} {'maxDD':<8} {'trades':<7} chosen profile"
    print(header)
    print("-" * (len(header) + 12))
    for r in sorted(results, key=lambda r: -((r.get("best") or {}).get("total_return") or float("-inf"))):
        if r.get("error"):
            print(f"{str(r.get('id')):<10} ERROR: {r['error']}")
            continue
        base = (r.get("baseline") or {}).get("total_return")
        best = r.get("best") or {}
        bret = best.get("total_return")
        ret = f"{_fmt_pct(base)} → {_fmt_pct(bret)}"
        dd = best.get("max_drawdown")
        dds = "   —" if dd is None else f"{dd:.1%}"
        trades = best.get("trades")
        label = _profile_label(best.get("profile"))
        flag = "" if best.get("eligible", True) else "  (no eligible profile — best-effort)"
        print(f"{str(r['id']):<10} {str(r.get('timeframe'))[:4]:<4} {ret:<22} {dds:<8} "
              f"{str(int(trades)) if trades is not None else '—':<7} {label}{flag}")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to back up + write these profiles.")
    else:
        print("\nProfiles written. Re-score to confirm, then restart/refresh the app so paper sizes off them:")
        print("  python scripts/rescore_paper_strategies.py --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
