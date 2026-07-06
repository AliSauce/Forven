"""CRUX-1: value-ranked allocation of the crucible research budget.

Diagnosis (2026-07-06 crucible review): 1,512 crucibles produced 3,971
strategies for 6 survivors (0.15%) while consuming ~85% of weekly agent
compute (~250 develop tasks per survivor). Both dispatchers were economically
blind — crucible_planner iterated the active pool oldest-first and the
hypothesis-promotion loop's score collapsed to random for the (majority)
zero-children candidates. Every recent systemic pathology (585-task fruitless
dispatch loop, substrate-mismatch phantom class, dark-starved families) is a
symptom of dispatching without a value model.

This module is the shared brain for both dispatchers:

- crucible_value_score(): pure scoring function — true stage survival of the
  crucible's children dominates, family survival priors (90d,
  survivor-weighted) steer cold-start ranking, fruitless/failed develops and
  yield-free depth are penalized, staleness decays.
- develop budget: a hard daily cap on develop_candidate-family dispatches
  shared by BOTH loops (in-flight caps bound concurrency, not daily spend).
- trade-mode directive: a quota of daily develops carry an explicit
  short/both authoring requirement — the 2026-07-05 graveyard audit found
  shorts net-positive in EVERY regime bucket while generation ran 9:1 long.

Knobs live in research settings under hypothesis_discipline
(crucible_daily_develop_budget, crucible_short_mode_quota_pct).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from forven.db import get_db

log = logging.getLogger(__name__)

# Stages that count as a promoted descendant (mirrors forven.crucibles).
SURVIVOR_STAGES = ("paper", "paper_trading", "live_graduated", "deployed")

# Children past this with zero positive signal start dragging the score down —
# round-robin depth on a yield-free thesis is the disproof factory's engine.
_YIELD_FREE_DEPTH_GRACE = 6

_FAMILY_STATS_TTL_SECONDS = 600
_family_stats_cache: tuple[float, dict[str, dict[str, int]]] | None = None

SHORT_DIRECTIVE_TEXT = (
    "\n\nDIRECTION QUOTA (CRUX-1): author THIS candidate with trade_mode='short' "
    "or trade_mode='both' and bake trade_mode into default_params (it is lost "
    "unless explicitly set). Evidence: the 2026-07-05 graveyard audit found "
    "shorts net-positive in EVERY regime bucket while generation ran 9:1 long "
    "— the short side is the pipeline's most under-explored edge surface."
)


def _discipline() -> dict[str, Any]:
    from forven.research_contract import get_hypothesis_discipline_settings

    return get_hypothesis_discipline_settings()


# ── value model ──────────────────────────────────────────────────────────────

def crucible_value_score(
    *,
    status: str = "researching",
    survivor_children: int = 0,
    gauntlet_children: int = 0,
    positive_children: int = 0,
    scored_children: int = 0,
    fruitless_develops: int = 0,
    failed_develops: int = 0,
    days_since_activity: float = 0.0,
    family_survival_rate: float | None = None,
) -> float:
    """Expected-value score for one crucible. Pure and deterministic.

    True survival dominates (a paper/live descendant is the only ground truth
    the system has); verdict-eligible children and gauntlet reach are weaker
    positive evidence; the family prior does the cold-start steering when a
    crucible has no children yet. Depth without yield is penalized so the
    round-robin can't keep re-watering proven-dead theses.
    """
    score = (
        6.0 * max(0, int(survivor_children))
        + 1.5 * max(0, int(gauntlet_children))
        + 2.0 * max(0, int(positive_children))
        + 0.25 * max(0, int(scored_children))
    )

    if family_survival_rate is not None:
        # Smoothed rate arrives 0..1; weight so a hot family (~10%+) is worth
        # about one gauntlet child and a dead family adds nearly nothing.
        score += 12.0 * max(0.0, min(1.0, float(family_survival_rate)))

    score -= 2.0 * max(0, int(fruitless_develops))
    score -= 1.0 * max(0, int(failed_develops))
    score -= 0.05 * max(0.0, float(days_since_activity))

    depth = max(0, int(scored_children))
    if depth > _YIELD_FREE_DEPTH_GRACE and not (
        survivor_children or positive_children or gauntlet_children
    ):
        score -= min(3.0, 0.25 * (depth - _YIELD_FREE_DEPTH_GRACE))

    if str(status or "").strip().lower() == "proven":
        score *= 1.5
    return round(score, 4)


def smoothed_family_rate(family: str | None, stats: dict[str, dict[str, int]]) -> float:
    """Laplace-smoothed survivor rate for a family ((s+0.5)/(n+10)); the prior
    for families with no data lands near the global base rate instead of 0."""
    entry = stats.get(str(family or "other")) or {}
    survivors = max(0, int(entry.get("survivors") or 0))
    attempts = max(0, int(entry.get("attempts") or 0))
    return (survivors + 0.5) / (attempts + 10.0)


def cached_family_outcome_stats() -> dict[str, dict[str, int]]:
    """family_outcome_stats with a short TTL — both dispatch loops call per
    cycle and the underlying query scans the 90d strategy window."""
    global _family_stats_cache
    now = time.time()
    if _family_stats_cache and now - _family_stats_cache[0] < _FAMILY_STATS_TTL_SECONDS:
        return _family_stats_cache[1]
    try:
        from forven.strategy_diversity import family_outcome_stats

        stats = family_outcome_stats()
    except Exception as exc:
        log.debug("family outcome stats unavailable: %s", exc)
        stats = {}
    _family_stats_cache = (now, stats)
    return stats


def fetch_crucible_child_signals(crucible_ids: list[str]) -> dict[str, dict[str, Any]]:
    """One pass over strategies: per-crucible child stage/verdict aggregates.

    Linkage key is hypothesis_id OR (when that's empty) origin_crucible_id —
    mirroring the planner's _strategy_count semantics. Joining hypothesis_id
    alone blind-spotted legacy/orphaned survivors that only carry
    origin_crucible_id, which suppressed the exploit lane for exactly the
    proven-family crucibles CRUX-1 targets (2026-07-06 audit finding).
    """
    ids = [str(c) for c in crucible_ids if str(c or "").strip()]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    survivor_list = ",".join(f"'{s}'" for s in SURVIVOR_STAGES)
    link = "COALESCE(NULLIF(TRIM(COALESCE(hypothesis_id, '')), ''), origin_crucible_id)"
    try:
        with get_db() as conn:
            rows = conn.execute(
                f"""
                SELECT {link} AS crucible_key,
                       COUNT(*) AS children,
                       SUM(CASE WHEN stage IN ({survivor_list}) THEN 1 ELSE 0 END) AS survivor_children,
                       SUM(CASE WHEN stage = 'gauntlet' THEN 1 ELSE 0 END) AS gauntlet_children,
                       SUM(CASE WHEN verdict LIKE '%deploy_eligible%'
                                  OR verdict LIKE '%paper_eligible%' THEN 1 ELSE 0 END) AS positive_children,
                       MAX(created_at) AS last_child_created_at
                FROM strategies
                WHERE {link} IN ({placeholders})
                GROUP BY {link}
                """,
                ids,
            ).fetchall()
    except Exception as exc:
        log.debug("crucible child signal query failed: %s", exc)
        return {}
    return {str(r["crucible_key"]): dict(r) for r in rows}


# ── daily develop budget (shared by both dispatch loops) ─────────────────────

def develop_daily_budget() -> int:
    return int(_discipline()["crucible_daily_develop_budget"])


def develop_budget_used_today() -> int:
    """develop_candidate-family tasks created since UTC midnight, any status —
    a dispatched task is spent budget whether or not it later fails."""
    try:
        with get_db() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM agent_tasks
                   WHERE type = 'develop_candidate'
                     AND created_at >= strftime('%Y-%m-%dT00:00:00+00:00', 'now')"""
            ).fetchone()
        return int(row["n"] or 0)
    except Exception as exc:
        log.debug("develop budget count failed: %s", exc)
        return 0


def develop_budget_remaining() -> int:
    return max(0, develop_daily_budget() - develop_budget_used_today())


# ── short/both trade-mode directive quota ────────────────────────────────────

def _directive_counts_today() -> tuple[int, int]:
    """(develops_today, directive_carrying_develops_today)."""
    try:
        with get_db() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN json_extract(input_data, '$.trade_mode_directive')
                                   IS NOT NULL THEN 1 ELSE 0 END) AS directed
                   FROM agent_tasks
                   WHERE type = 'develop_candidate'
                     AND created_at >= strftime('%Y-%m-%dT00:00:00+00:00', 'now')"""
            ).fetchone()
        return int(row["total"] or 0), int(row["directed"] or 0)
    except Exception as exc:
        log.debug("directive count failed: %s", exc)
        return 0, 0


def next_trade_mode_directive() -> str | None:
    """'short_or_both' when today's directive share is under quota, else None.

    Callers stamp it into input_data (the counter's source of truth) and
    append SHORT_DIRECTIVE_TEXT to the task description.
    """
    quota_pct = float(_discipline()["crucible_short_mode_quota_pct"])
    if quota_pct <= 0:
        return None
    total, directed = _directive_counts_today()
    if total == 0:
        return "short_or_both"
    return "short_or_both" if (directed / total) * 100.0 < quota_pct else None
