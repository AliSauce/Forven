"""Daily learning job. Summarizes yesterday's closed trades and writes to LESSONS.md"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from forven.db import get_db, init_db
from forven.ai import call_ai
from forven.workspace import append_workspace
from forven.reporter import post_daily_summary

log = logging.getLogger("forven.jobs.daily_learning")

async def run_daily_learning():
    init_db()
    
    now = datetime.now(timezone.utc)
    lookback_start = (now - timedelta(days=1)).isoformat()
    
    with get_db() as conn:
        closed_trades = conn.execute(
            "SELECT strategy_id, asset, pnl_pct, pnl_usd, signal_data FROM trades WHERE status = 'CLOSED' AND closed_at >= ?",
            (lookback_start,)
        ).fetchall()
        
        degradations = conn.execute(
            "SELECT strategy_id, status_before, status_after, reason FROM strategy_decay_audit WHERE triggered_at >= ?",
            (lookback_start,)
        ).fetchall()

    if not closed_trades and not degradations:
        log.info("No data in the last 24h to learn from. Skipping daily learning.")
        return

    prompt = (
        "Analyze the following trading day (last 24h) and provide a concise, "
        "bullet-point markdown summary of lessons learned for a quant trading system.\n\n"
    )
    
    if closed_trades:
        prompt += f"CLOSED TRADES ({len(closed_trades)} total):\n"
        for t in closed_trades:
            pnl_pct = t["pnl_pct"] or 0
            prompt += f"- {t['strategy_id']} on {t['asset']}: PnL {pnl_pct*100:.2f}%\n"
            
    if degradations:
        prompt += f"\nSTRATEGY DEGRADATIONS ({len(degradations)} total):\n"
        for d in degradations:
            prompt += f"- {d['strategy_id']}: {d['status_before']} -> {d['status_after']} (Reason: {d['reason']})\n"
            
    prompt += "\nFormat the response strictly as markdown with a list of 'Lessons' and 'Pattern Observations'. Avoid fluff."

    try:
        # Route via the operator's configured primary provider/model, NEVER a
        # hardcoded provider, and with fallback=False so this unattended daily
        # job can never walk a chain onto a model the operator did not select.
        from forven.model_routing import get_primary_provider_model
        from forven.model_selection import UnconfiguredRouteError

        provider, model = get_primary_provider_model()
        log.info("Calling AI to synthesize daily learning (%s/%s)...", provider, model)
        try:
            summary = await call_ai(
                provider=provider,
                model=model,
                prompt=prompt,
                fallback=False,
            )
        except UnconfiguredRouteError as exc:
            log.warning("Skipping daily learning: no connected & selected LLM configured (%s)", exc)
            return

        lesson_block = f"\n\n## Daily Review - {now.strftime('%Y-%m-%d')}\n{summary}\n"
        append_workspace("LESSONS.md", lesson_block)

        # Post to Discord
        await post_daily_summary(summary)
        log.info("Daily learning synthesis complete and persisted to LESSONS.md.")

    except Exception as exc:
        log.error("Failed to execute daily learning job: %s", exc)

def execute_daily_learning_sync():
    """Sync wrapper for the scheduler"""
    asyncio.run(run_daily_learning())

if __name__ == "__main__":
    execute_daily_learning_sync()
