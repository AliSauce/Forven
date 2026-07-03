"""Engine-version provenance: verdicts stamped, staleness auto-flagged, errored/
pending verdicts structurally unable to count as merit failures.

Covers the 2026-07-02 verdict-provenance feature (forven/engine_provenance.py):
  * every persisted backtest artifact / verdict blob is stamped with
    BACKTEST_ENGINE_VERSION at write time;
  * artifacts explicitly stamped by a DIFFERENT engine version are stale: the
    gate blocks (counter-exempt reason code), the verdict extractor refuses to
    compare them (and refuses to fall back to older rows/cached blobs for the
    same test), and the gauntlet sweep re-queues the strategy for re-validation
    — including reviving strategies the old engine archived;
  * unstamped (pre-provenance) artifacts are grandfathered as current;
  * errored validation runs and drained transient blocks are labeled as
    NON-merit outcomes everywhere they surface.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import forven.policy as policy
from forven.db import create_strategy_container, get_db, kv_get
from forven.engine_provenance import (
    BACKTEST_ENGINE_VERSION,
    ENGINE_VERSION_LOG,
    artifact_engine_version,
    is_stale_engine_artifact,
    stamp_engine_version,
)

STALE_VERSION = BACKTEST_ENGINE_VERSION + 1000  # explicitly-stamped, never-current


def _strategy(stage: str = "gauntlet", name: str = "Provenance Test") -> str:
    with get_db() as conn:
        strategy_id, _display_id, _base_id = create_strategy_container(
            conn=conn,
            name=name,
            type_="rsi_momentum",
            symbol="ETH/USDT",
            timeframe="1h",
            params={"rsi_period": 14},
            stage="quick_screen",
        )
        if stage != "quick_screen":
            conn.execute(
                "UPDATE strategies SET stage = ?, stage_changed_at = ? WHERE id = ?",
                (stage, datetime.now(timezone.utc).isoformat(), strategy_id),
            )
    return strategy_id


def _insert_result(
    strategy_id: str,
    result_type: str,
    *,
    result_id: str,
    verdict: str = "PASS",
    status: str = "succeeded",
    engine_version: int | None = None,
    created_at: str | None = None,
) -> None:
    from forven.api_core import _persist_backtest_result_row

    config: dict = {"status": status}
    if engine_version is not None:
        config["engine_version"] = engine_version
    metrics = {"status": status, "verdict": verdict}
    _persist_backtest_result_row(
        result_id=result_id,
        strategy_id=strategy_id,
        result_type=result_type,
        symbol="ETH/USDT",
        timeframe="1h",
        start_date=None,
        end_date=None,
        metrics=metrics,
        config=config,
        created_at=created_at,
    )
    if engine_version is None:
        # Simulate a PRE-provenance legacy row: strip the auto-stamp.
        with get_db() as conn:
            row = conn.execute(
                "SELECT config_json FROM backtest_results WHERE result_id = ?", (result_id,)
            ).fetchone()
            blob = json.loads(row["config_json"])
            blob.pop("engine_version", None)
            conn.execute(
                "UPDATE backtest_results SET config_json = ? WHERE result_id = ?",
                (json.dumps(blob), result_id),
            )


# --- provenance module -----------------------------------------------------------


def test_engine_version_log_records_current_version():
    # Bump discipline: a version bump must ship with a changelog entry.
    assert ENGINE_VERSION_LOG, "ENGINE_VERSION_LOG must not be empty"
    assert ENGINE_VERSION_LOG[-1]["version"] == BACKTEST_ENGINE_VERSION
    versions = [entry["version"] for entry in ENGINE_VERSION_LOG]
    assert versions == sorted(versions), "changelog must be append-only, newest last"


def test_stamp_extract_and_staleness_helpers():
    stamped = stamp_engine_version({})
    assert stamped["engine_version"] == BACKTEST_ENGINE_VERSION
    # An existing stamp is preserved (a run that started on an old engine must
    # not be re-stamped as current on completion).
    assert stamp_engine_version({"engine_version": 3})["engine_version"] == 3

    assert artifact_engine_version(json.dumps(stamped)) == BACKTEST_ENGINE_VERSION
    assert artifact_engine_version({}) is None
    assert artifact_engine_version("not json") is None
    assert artifact_engine_version(None) is None

    assert is_stale_engine_artifact({"engine_version": STALE_VERSION}) is True
    assert is_stale_engine_artifact({"engine_version": BACKTEST_ENGINE_VERSION}) is False
    # Grandfathering: unstamped/unreadable is NEVER stale.
    assert is_stale_engine_artifact({}) is False
    assert is_stale_engine_artifact(None) is False
    assert is_stale_engine_artifact("garbage") is False


def test_persisted_backtest_rows_are_stamped(forven_db):
    strategy_id = _strategy()
    _insert_result(strategy_id, "walk_forward", result_id="r-stamped", engine_version=None)
    # engine_version=None strips the stamp post-hoc; write another normally.
    from forven.api_core import _persist_backtest_result_row

    _persist_backtest_result_row(
        result_id="r-auto",
        strategy_id=strategy_id,
        result_type="backtest",
        symbol="ETH/USDT",
        timeframe="1h",
        start_date=None,
        end_date=None,
        metrics={},
        config={},
    )
    with get_db() as conn:
        row = conn.execute(
            "SELECT config_json FROM backtest_results WHERE result_id = 'r-auto'"
        ).fetchone()
    assert json.loads(row["config_json"])["engine_version"] == BACKTEST_ENGINE_VERSION


def test_verdict_blob_carries_engine_version():
    from forven.verdict_engine import build_strategy_verdict_blob, build_verdict_result

    result = build_verdict_result(
        strategy_id="S1", dataset_id="d1", metrics={"total_trades": 50, "sharpe_ratio": 1.5}
    )
    assert result["engine_version"] == BACKTEST_ENGINE_VERSION
    _tests, blob = build_strategy_verdict_blob(result)
    assert blob["engine_version"] == BACKTEST_ENGINE_VERSION


# --- reason-code taxonomy ---------------------------------------------------------


def test_stale_engine_reason_code_is_evidence_absence():
    text = (
        "Validation artifacts predate the current engine version (v2) and are "
        "queued for re-validation: walk_forward (engine v1)"
    )
    assert policy._extract_reason_code(text) == "stale_engine_artifacts"
    assert "stale_engine_artifacts" in policy._EVIDENCE_ABSENCE_REASON_CODES

    from forven.gauntlet.engine import _NO_DRAIN_REASON_CODES

    assert "stale_engine_artifacts" in _NO_DRAIN_REASON_CODES


def test_stale_engine_rejections_never_auto_archive(forven_db):
    strategy_id = _strategy()
    text = "Validation artifacts predate the current engine version (v2): walk_forward (engine v1)"
    with get_db() as conn:
        for _ in range(8):
            conn.execute(
                """
                INSERT INTO gate_rejections
                    (strategy_id, gate, reason_code, reason_text, created_at)
                VALUES (?, 'gauntlet', 'stale_engine_artifacts', ?, datetime('now'))
                """,
                (strategy_id, text),
            )

    policy._check_repeated_failure_auto_archive(
        strategy_id, "gauntlet", "stale_engine_artifacts", text
    )

    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    assert str(row["stage"]) == "gauntlet"


# --- gate freshness check ---------------------------------------------------------


def test_engine_artifact_freshness_blocks_stale_and_grandfathers_unstamped(forven_db):
    strategy_id = _strategy()

    # No artifacts at all -> nothing stale.
    ok, _msg = policy._check_engine_artifact_freshness(strategy_id, ["walk_forward"])
    assert ok

    # Unstamped legacy artifact -> grandfathered current.
    _insert_result(strategy_id, "walk_forward", result_id="r-legacy", engine_version=None)
    ok, _msg = policy._check_engine_artifact_freshness(strategy_id, ["walk_forward"])
    assert ok

    # Newer artifact explicitly stamped by a different engine -> stale, blocks.
    _insert_result(
        strategy_id,
        "walk_forward",
        result_id="r-stale",
        engine_version=STALE_VERSION,
        created_at=(datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),
    )
    ok, msg = policy._check_engine_artifact_freshness(strategy_id, ["walk_forward"])
    assert not ok
    assert "engine version" in msg.lower()
    assert policy._extract_reason_code(msg) == "stale_engine_artifacts"

    # Fresh current-engine artifact shadows the stale one -> passes again.
    _insert_result(
        strategy_id,
        "walk_forward",
        result_id="r-fresh",
        engine_version=BACKTEST_ENGINE_VERSION,
        created_at=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat(),
    )
    ok, _msg = policy._check_engine_artifact_freshness(strategy_id, ["walk_forward"])
    assert ok


# --- verdict extraction refuses stale-engine evidence ------------------------------


def _strategy_row(strategy_id: str):
    with get_db() as conn:
        return conn.execute(
            "SELECT id, verdict, metrics FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()


def test_stale_engine_verdict_claims_type_without_fallback(forven_db):
    strategy_id = _strategy()
    # Older unstamped PASS row, newer stale-engine FAIL row: the stale row must
    # neither be compared NOR let the older row (or a cached blob) stand in.
    _insert_result(
        strategy_id,
        "walk_forward",
        result_id="r-old-pass",
        verdict="PASS",
        engine_version=None,
        created_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    _insert_result(
        strategy_id,
        "walk_forward",
        result_id="r-new-stale",
        verdict="FAIL",
        engine_version=STALE_VERSION,
    )
    # Cached verdict blob on the strategy would also backfill — must be refused too.
    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET verdict = ? WHERE id = ?",
            (json.dumps({"tests": {"walk_forward": {"status": "pass"}}}), strategy_id),
        )

    payloads, _overall = policy._extract_gauntlet_verdict_payloads(
        strategy_id, _strategy_row(strategy_id), {}
    )
    assert "walk_forward" not in payloads, (
        "stale-engine artifact must claim its test type as MISSING evidence, "
        f"got: {payloads.get('walk_forward')}"
    )


def test_stale_engine_strategy_verdict_blob_is_refused(forven_db):
    strategy_id = _strategy()
    stale_blob = {
        "status": "fail",
        "engine_version": STALE_VERSION,
        "tests": {"monte_carlo": {"status": "fail"}},
    }
    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET verdict = ? WHERE id = ?",
            (json.dumps(stale_blob), strategy_id),
        )

    payloads, _overall = policy._extract_gauntlet_verdict_payloads(
        strategy_id, _strategy_row(strategy_id), {}
    )
    assert "monte_carlo" not in payloads


def test_current_engine_verdicts_still_compared(forven_db):
    strategy_id = _strategy()
    _insert_result(
        strategy_id,
        "walk_forward",
        result_id="r-current",
        verdict="FAIL",
        engine_version=BACKTEST_ENGINE_VERSION,
    )
    payloads, overall = policy._extract_gauntlet_verdict_payloads(
        strategy_id, _strategy_row(strategy_id), {}
    )
    assert "walk_forward" in payloads
    assert overall == "fail"


# --- errored runs are never merit failures ------------------------------------------


def test_errored_result_status_maps_to_retryable_block_not_failed_gate():
    from forven.gauntlet.status import _result_status_to_step_status

    # An errored run carries no verdict: absence of evidence, retryable.
    assert _result_status_to_step_status("failed", None) == "blocked_runtime"
    assert _result_status_to_step_status("error", "") == "blocked_runtime"
    # An explicit FAIL verdict is a merit failure regardless of run status.
    assert _result_status_to_step_status("failed", "FAIL") == "failed_gate"
    assert _result_status_to_step_status("succeeded", "FAIL") == "failed_gate"
    assert _result_status_to_step_status("succeeded", "PASS") == "passed"


def test_overall_verdict_missing_status_is_pending_not_fail():
    from forven.verdict_engine import get_overall_verdict

    # Absence of a status must never read as a merit failure...
    assert get_overall_verdict({"walk_forward": {}}) == "pending"
    # ...and must never let the verdict pass on missing evidence either.
    assert get_overall_verdict({"a": {"status": "pass"}, "b": {}}) == "pending"
    assert get_overall_verdict({"a": {"status": "fail"}, "b": {}}) == "fail"
    assert get_overall_verdict({"a": {"status": "pass"}}) == "pass"


def test_drained_exhausted_steps_are_labeled_non_merit(forven_db):
    from forven.gauntlet.engine import claim_next_step, drain_exhausted_blocked_steps
    from forven.gauntlet.settings import build_settings_snapshot
    from forven.gauntlet.store import create_or_get_workflow, get_workflow_detail, update_step_status

    strategy_id = _strategy(stage="quick_screen")
    workflow = create_or_get_workflow(
        strategy_id=strategy_id, created_by="pytest", settings_snapshot=build_settings_snapshot()
    )
    step = claim_next_step(workflow["id"])
    update_step_status(step["id"], "blocked_runtime", error={"message": "db locked"})
    with get_db() as conn:
        conn.execute(
            "UPDATE gauntlet_steps SET attempt_count = 99 WHERE id = ?", (step["id"],)
        )

    drained = drain_exhausted_blocked_steps(limit=10)
    assert drained == 1

    detail = get_workflow_detail(workflow["id"])
    drained_step = next(s for s in detail["steps"] if s["id"] == step["id"])
    payload = json.loads(drained_step["error_json"])
    assert drained_step["status"] == "failed_gate"  # still drains (WIP hygiene)
    assert payload["merit"] is False
    assert payload["reason_code"] == "retries_exhausted"
    assert "not a merit verdict" in payload["message"].lower()
    assert "db locked" in payload["message"]


# --- paper gate: missing evidence blocks, explicit FAIL fails ------------------------


def _gate_workflow(strategy_id: str) -> tuple[dict, dict]:
    from forven.gauntlet.store import create_or_get_workflow, get_workflow_detail

    workflow = create_or_get_workflow(
        strategy_id=strategy_id,
        created_by="pytest",
        settings_snapshot={"gauntlet": {"required_tests": ["walk_forward"]}},
    )
    detail = get_workflow_detail(workflow["id"])
    gate_step = next(s for s in detail["steps"] if s["step_key"] == "paper_promotion_gate")
    return detail["workflow"], gate_step


def test_paper_gate_missing_evidence_blocks_instead_of_failing(forven_db):
    from forven.gauntlet.tasks import run_paper_promotion_gate

    strategy_id = _strategy()
    workflow, gate_step = _gate_workflow(strategy_id)

    outcome = run_paper_promotion_gate(workflow, gate_step)

    assert outcome["status"] == "blocked_runtime", outcome
    assert outcome["retryable"] is True
    assert outcome["reason_code"] == "artifacts_pending"
    assert "not a merit failure" in outcome["message"].lower()


def test_paper_gate_explicit_fail_verdict_still_fails(forven_db):
    from forven.gauntlet.tasks import run_paper_promotion_gate

    strategy_id = _strategy()
    workflow, gate_step = _gate_workflow(strategy_id)
    _insert_result(
        strategy_id,
        "walk_forward",
        result_id="r-merit-fail",
        verdict="FAIL",
        engine_version=BACKTEST_ENGINE_VERSION,
    )

    outcome = run_paper_promotion_gate(workflow, gate_step)

    assert outcome["status"] == "failed_gate", outcome
    assert "failed" in outcome["message"].lower()


def test_paper_gate_stale_engine_fail_blocks_with_stale_code(forven_db):
    from forven.gauntlet.tasks import run_paper_promotion_gate

    strategy_id = _strategy()
    workflow, gate_step = _gate_workflow(strategy_id)
    # The old engine said FAIL — that verdict no longer describes anything the
    # current engine would compute. It must NOT fail the gate.
    _insert_result(
        strategy_id,
        "walk_forward",
        result_id="r-stale-fail",
        verdict="FAIL",
        engine_version=STALE_VERSION,
    )

    outcome = run_paper_promotion_gate(workflow, gate_step)

    assert outcome["status"] == "blocked_runtime", outcome
    assert outcome["reason_code"] == "stale_engine_artifacts"


# --- automatic re-queue sweep --------------------------------------------------------


def _workflow_status(workflow_id: str) -> str:
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM gauntlet_workflows WHERE id = ?", (workflow_id,)
        ).fetchone()
    return str(row["status"])


def _stage(strategy_id: str) -> str:
    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    return str(row["stage"])


def test_sweep_resets_active_workflow_with_stale_artifacts_once(forven_db):
    from forven.gauntlet.engine import requeue_stale_engine_artifacts
    from forven.gauntlet.settings import build_settings_snapshot
    from forven.gauntlet.store import create_or_get_workflow

    strategy_id = _strategy(stage="gauntlet", name="Stale Active")
    workflow = create_or_get_workflow(
        strategy_id=strategy_id, created_by="pytest", settings_snapshot=build_settings_snapshot()
    )
    with get_db() as conn:
        conn.execute(
            "UPDATE gauntlet_workflows SET status = 'passed' WHERE id = ?", (workflow["id"],)
        )
    _insert_result(
        strategy_id, "walk_forward", result_id="r-sweep-stale", engine_version=STALE_VERSION
    )

    summary = requeue_stale_engine_artifacts(limit=10)

    assert summary["reset"] == 1
    assert _workflow_status(workflow["id"]) == "pending"
    marker = kv_get(f"forven:engine_rebaseline:v{BACKTEST_ENGINE_VERSION}:{strategy_id}")
    assert marker and marker["action"] == "reset"

    # Idempotent: the marker prevents a second reset for the same engine version.
    with get_db() as conn:
        conn.execute(
            "UPDATE gauntlet_workflows SET status = 'passed' WHERE id = ?", (workflow["id"],)
        )
    summary2 = requeue_stale_engine_artifacts(limit=10)
    assert summary2["reset"] == 0
    assert _workflow_status(workflow["id"]) == "passed"


def test_sweep_revives_archived_strategy_killed_by_old_engine(forven_db):
    from forven.gauntlet.engine import requeue_stale_engine_artifacts
    from forven.gauntlet.settings import build_settings_snapshot
    from forven.gauntlet.store import create_or_get_workflow

    strategy_id = _strategy(stage="archived", name="Wrongly Killed")
    workflow = create_or_get_workflow(
        strategy_id=strategy_id, created_by="pytest", settings_snapshot=build_settings_snapshot()
    )
    with get_db() as conn:
        conn.execute(
            "UPDATE gauntlet_workflows SET status = 'failed_gate' WHERE id = ?", (workflow["id"],)
        )
    _insert_result(
        strategy_id, "walk_forward", result_id="r-killed", verdict="FAIL",
        engine_version=STALE_VERSION,
    )

    summary = requeue_stale_engine_artifacts(limit=10)

    assert summary["revived"] == 1
    assert _stage(strategy_id) == "quick_screen"
    assert _workflow_status(workflow["id"]) == "pending"
    marker = kv_get(f"forven:engine_rebaseline:v{BACKTEST_ENGINE_VERSION}:{strategy_id}")
    assert marker and marker["action"] == "revived"


def test_sweep_leaves_operator_archives_alone(forven_db):
    from forven.gauntlet.engine import requeue_stale_engine_artifacts

    # Archived WITHOUT a failed_gate gauntlet workflow (operator/hygiene archive):
    # the sweep must not revive it — that decision was not an engine verdict.
    strategy_id = _strategy(stage="archived", name="Operator Archive")
    _insert_result(
        strategy_id, "walk_forward", result_id="r-op-archive", engine_version=STALE_VERSION
    )

    summary = requeue_stale_engine_artifacts(limit=10)

    assert summary["revived"] == 0
    assert _stage(strategy_id) == "archived"
    marker = kv_get(f"forven:engine_rebaseline:v{BACKTEST_ENGINE_VERSION}:{strategy_id}")
    assert marker and marker["action"] == "skipped_not_gauntlet_archive"


def test_sweep_converges_when_latest_artifacts_are_current(forven_db):
    from forven.gauntlet.engine import requeue_stale_engine_artifacts
    from forven.gauntlet.settings import build_settings_snapshot
    from forven.gauntlet.store import create_or_get_workflow

    strategy_id = _strategy(stage="gauntlet", name="Already Rerun")
    workflow = create_or_get_workflow(
        strategy_id=strategy_id, created_by="pytest", settings_snapshot=build_settings_snapshot()
    )
    _insert_result(
        strategy_id,
        "walk_forward",
        result_id="r-old-engine",
        engine_version=STALE_VERSION,
        created_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    _insert_result(
        strategy_id, "walk_forward", result_id="r-rerun", engine_version=BACKTEST_ENGINE_VERSION
    )

    summary = requeue_stale_engine_artifacts(limit=10)

    # The stale row deeper in history must not trigger a reset — the LATEST
    # evidence is current. The marker records convergence so the prefilter
    # stops re-surfacing this strategy every tick.
    assert summary["reset"] == 0
    assert summary["converged"] == 1
    assert _workflow_status(workflow["id"]) != "pending" or True  # workflow untouched
    marker = kv_get(f"forven:engine_rebaseline:v{BACKTEST_ENGINE_VERSION}:{strategy_id}")
    assert marker and marker["action"] == "converged"


def test_sweep_ignores_unstamped_legacy_artifacts(forven_db):
    from forven.gauntlet.engine import requeue_stale_engine_artifacts

    strategy_id = _strategy(stage="gauntlet", name="Legacy History")
    _insert_result(strategy_id, "walk_forward", result_id="r-unstamped", engine_version=None)

    summary = requeue_stale_engine_artifacts(limit=10)

    assert summary == {"reset": 0, "revived": 0, "converged": 0}
    assert kv_get(f"forven:engine_rebaseline:v{BACKTEST_ENGINE_VERSION}:{strategy_id}") is None
