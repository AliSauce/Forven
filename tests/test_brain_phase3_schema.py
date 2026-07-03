"""Phase 3 schema migration tests — quant_skills_history, skill_outcome_events.

Verifies the Phase 3 (P3-T01) DDL applies cleanly on a fresh DB, indexes are
present, CHECK constraints fire, and the migration is idempotent on re-run.
(brain_lessons was removed 2026-07-02 — the migration now drops those tables.)
"""
from __future__ import annotations

import sqlite3
import tempfile

import pytest

from forven import db as forven_db


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("FORVEN_HOME", td)
        if hasattr(forven_db, "_DB_PATH"):
            forven_db._DB_PATH = None  # type: ignore[attr-defined]
        if hasattr(forven_db, "_init_db_done"):
            forven_db._init_db_done = False  # type: ignore[attr-defined]
        forven_db.init_db()
        with forven_db.get_db() as conn:
            yield conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


# ── quant_skills_history ────────────────────────────────────────────────────


def test_quant_skills_history_table_present(fresh_db):
    assert _table_exists(fresh_db, "quant_skills_history")
    cols = {row["name"] for row in fresh_db.execute("PRAGMA table_info(quant_skills_history)")}
    assert {
        "skill_name",
        "version",
        "parent_version",
        "body_diff",
        "change_summary",
        "evidence_task_id",
        "created_by",
        "created_at",
    } <= cols


def test_quant_skills_history_indexes_present(fresh_db):
    assert _index_exists(fresh_db, "idx_quant_skills_history_skill_version")
    assert _index_exists(fresh_db, "idx_quant_skills_history_evidence_task")
    assert _index_exists(fresh_db, "idx_quant_skills_history_created_at")


def test_quant_skills_history_unique_skill_version(fresh_db):
    fresh_db.execute(
        "INSERT INTO quant_skills_history (skill_name, version, body_diff) "
        "VALUES ('foo', 1, '')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.execute(
            "INSERT INTO quant_skills_history (skill_name, version, body_diff) "
            "VALUES ('foo', 1, 'dup')"
        )


# ── skill_outcome_events ────────────────────────────────────────────────────


def test_skill_outcome_events_table_present(fresh_db):
    assert _table_exists(fresh_db, "skill_outcome_events")
    cols = {row["name"] for row in fresh_db.execute("PRAGMA table_info(skill_outcome_events)")}
    assert {
        "skill_name",
        "strategy_id",
        "outcome",
        "confidence_delta",
        "confidence_before",
        "confidence_after",
        "evidence_task_id",
        "triggered_by",
        "notes",
    } <= cols


def test_skill_outcome_events_outcome_check(fresh_db):
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.execute(
            "INSERT INTO skill_outcome_events "
            "(skill_name, strategy_id, outcome, confidence_delta, confidence_before, confidence_after, triggered_by) "
            "VALUES ('foo', 's-1', 'invalid', 0, 0.5, 0.5, 'test')"
        )


def test_skill_outcome_events_idempotent_unique(fresh_db):
    fresh_db.execute(
        "INSERT INTO skill_outcome_events "
        "(skill_name, strategy_id, outcome, confidence_delta, confidence_before, confidence_after, triggered_by) "
        "VALUES ('foo', 's-1', 'negative', -0.05, 0.5, 0.45, 'transition_stage:archived')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.execute(
            "INSERT INTO skill_outcome_events "
            "(skill_name, strategy_id, outcome, confidence_delta, confidence_before, confidence_after, triggered_by) "
            "VALUES ('foo', 's-1', 'negative', -0.05, 0.45, 0.40, 'transition_stage:archived')"
        )


# ── brain_lessons (removed) ─────────────────────────────────────────────────


def test_brain_lessons_tables_dropped(fresh_db):
    assert not _table_exists(fresh_db, "brain_lessons")
    assert not _table_exists(fresh_db, "brain_lessons_fts")


# ── meta ────────────────────────────────────────────────────────────────────


def test_schema_version_is_at_least_26(fresh_db):
    row = fresh_db.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] >= 26


def test_phase3_migration_idempotent(fresh_db):
    if hasattr(forven_db, "_init_db_done"):
        forven_db._init_db_done = False  # type: ignore[attr-defined]
    forven_db.init_db()
    assert _table_exists(fresh_db, "quant_skills_history")
    assert _table_exists(fresh_db, "skill_outcome_events")
