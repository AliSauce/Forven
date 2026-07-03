"""Phase 1 (P1-T08) — FTS5 trigger smoke tests + rebuild helper.

Three contentless FTS5 virtual tables sit alongside source tables:

* ``brain_decisions_fts`` mirrors ``brain_decisions``
* ``agent_tasks_fts``     mirrors ``agent_tasks``
* ``task_audit_log_fts``  mirrors ``task_audit_log``

INSERT/UPDATE/DELETE triggers keep them in sync. These tests assert that the
trigger wiring actually works for all three pairs, and that
``rebuild_fts5_indices`` is a working recovery knob if drift ever happens.
"""
from __future__ import annotations

from forven.db import FTS5_TABLES, get_db, rebuild_fts5_indices


# Use uppercase sentinel tokens that won't collide with anything else the
# schema or fixtures write. FTS5 default tokenizer is case-insensitive but
# we keep them uppercase here for visual grep-ability.
TOKEN_INSERT = "ZARFZIPZAR"
TOKEN_UPDATE = "QUUXBLATTERN"
TOKEN_OUTPUT = "FLIBBERTIPLAX"


def _insert_decision(situation: str, action: str = "noop") -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO brain_decisions (cycle_id, situation_summary, decision_json, action_taken) "
            "VALUES (?, ?, ?, ?)",
            ("c-test", situation, "{}", action),
        )
        return int(cur.lastrowid)


def _ensure_agent(agent_id: str = "quant-researcher") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role) VALUES (?, ?, ?)",
            (agent_id, agent_id.replace("-", " ").title(), agent_id),
        )


def _insert_agent_task(title: str, description: str, output: str | None = None) -> int:
    _ensure_agent()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO agent_tasks (agent_id, type, title, description, status, output_data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("quant-researcher", "research", title, description, "pending", output),
        )
        return int(cur.lastrowid)


def _insert_audit(task_id: str, tool_name: str, summary: str) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO task_audit_log (task_id, agent_id, tool_name, input_json, output_summary) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, "quant-researcher", tool_name, "{}", summary),
        )
        return int(cur.lastrowid)


# --- brain_decisions_fts -------------------------------------------------

def test_brain_decisions_insert_makes_row_searchable(forven_db):
    decision_id = _insert_decision(f"market is {TOKEN_INSERT} today")
    with get_db() as conn:
        row = conn.execute(
            "SELECT rowid FROM brain_decisions_fts WHERE brain_decisions_fts MATCH ?",
            (TOKEN_INSERT,),
        ).fetchone()
    assert row is not None
    assert int(row["rowid"]) == decision_id


def test_brain_decisions_update_reflects_new_body(forven_db):
    decision_id = _insert_decision(f"hello {TOKEN_INSERT}")
    with get_db() as conn:
        conn.execute(
            "UPDATE brain_decisions SET situation_summary = ? WHERE id = ?",
            (f"different {TOKEN_UPDATE} now", decision_id),
        )
        # Old token must NOT match anymore.
        old = conn.execute(
            "SELECT rowid FROM brain_decisions_fts WHERE brain_decisions_fts MATCH ?",
            (TOKEN_INSERT,),
        ).fetchone()
        # New token MUST match.
        new = conn.execute(
            "SELECT rowid FROM brain_decisions_fts WHERE brain_decisions_fts MATCH ?",
            (TOKEN_UPDATE,),
        ).fetchone()
    assert old is None
    assert new is not None
    assert int(new["rowid"]) == decision_id


def test_brain_decisions_delete_removes_from_index(forven_db):
    decision_id = _insert_decision(f"transient {TOKEN_INSERT}")
    with get_db() as conn:
        conn.execute("DELETE FROM brain_decisions WHERE id = ?", (decision_id,))
        row = conn.execute(
            "SELECT rowid FROM brain_decisions_fts WHERE brain_decisions_fts MATCH ?",
            (TOKEN_INSERT,),
        ).fetchone()
    assert row is None


# --- agent_tasks_fts -----------------------------------------------------

def test_agent_tasks_insert_makes_row_searchable(forven_db):
    task_id = _insert_agent_task(f"title {TOKEN_INSERT}", "plain description")
    with get_db() as conn:
        row = conn.execute(
            "SELECT rowid FROM agent_tasks_fts WHERE agent_tasks_fts MATCH ?",
            (TOKEN_INSERT,),
        ).fetchone()
    assert row is not None
    assert int(row["rowid"]) == task_id


def test_agent_tasks_update_description_and_output(forven_db):
    task_id = _insert_agent_task("plain title", f"first body {TOKEN_INSERT}")
    with get_db() as conn:
        conn.execute(
            "UPDATE agent_tasks SET description = ?, output_data = ? WHERE id = ?",
            (f"second body {TOKEN_UPDATE}", f"{{\"note\":\"{TOKEN_OUTPUT}\"}}", task_id),
        )
        old = conn.execute(
            "SELECT rowid FROM agent_tasks_fts WHERE agent_tasks_fts MATCH ?",
            (TOKEN_INSERT,),
        ).fetchone()
        new_desc = conn.execute(
            "SELECT rowid FROM agent_tasks_fts WHERE agent_tasks_fts MATCH ?",
            (TOKEN_UPDATE,),
        ).fetchone()
        new_out = conn.execute(
            "SELECT rowid FROM agent_tasks_fts WHERE agent_tasks_fts MATCH ?",
            (TOKEN_OUTPUT,),
        ).fetchone()
    assert old is None
    assert new_desc is not None and int(new_desc["rowid"]) == task_id
    assert new_out is not None and int(new_out["rowid"]) == task_id


def test_agent_tasks_delete_removes_from_index(forven_db):
    task_id = _insert_agent_task(f"ephemeral {TOKEN_INSERT}", "desc")
    with get_db() as conn:
        conn.execute("DELETE FROM agent_tasks WHERE id = ?", (task_id,))
        row = conn.execute(
            "SELECT rowid FROM agent_tasks_fts WHERE agent_tasks_fts MATCH ?",
            (TOKEN_INSERT,),
        ).fetchone()
    assert row is None


# --- task_audit_log_fts --------------------------------------------------

def test_task_audit_log_insert_makes_row_searchable(forven_db):
    audit_id = _insert_audit("t1", "exec_python", f"output had {TOKEN_INSERT} in it")
    with get_db() as conn:
        row = conn.execute(
            "SELECT rowid FROM task_audit_log_fts WHERE task_audit_log_fts MATCH ?",
            (TOKEN_INSERT,),
        ).fetchone()
    assert row is not None
    assert int(row["rowid"]) == audit_id


def test_task_audit_log_update_reflects_new_summary(forven_db):
    audit_id = _insert_audit("t2", "memory", f"first {TOKEN_INSERT}")
    with get_db() as conn:
        conn.execute(
            "UPDATE task_audit_log SET output_summary = ? WHERE id = ?",
            (f"second {TOKEN_UPDATE}", audit_id),
        )
        old = conn.execute(
            "SELECT rowid FROM task_audit_log_fts WHERE task_audit_log_fts MATCH ?",
            (TOKEN_INSERT,),
        ).fetchone()
        new = conn.execute(
            "SELECT rowid FROM task_audit_log_fts WHERE task_audit_log_fts MATCH ?",
            (TOKEN_UPDATE,),
        ).fetchone()
    assert old is None
    assert new is not None
    assert int(new["rowid"]) == audit_id


def test_task_audit_log_delete_removes_from_index(forven_db):
    audit_id = _insert_audit("t3", "exec_python", f"transient {TOKEN_INSERT}")
    with get_db() as conn:
        conn.execute("DELETE FROM task_audit_log WHERE id = ?", (audit_id,))
        row = conn.execute(
            "SELECT rowid FROM task_audit_log_fts WHERE task_audit_log_fts MATCH ?",
            (TOKEN_INSERT,),
        ).fetchone()
    assert row is None


# --- rebuild helper ------------------------------------------------------

def test_rebuild_fts5_indices_repairs_after_simulated_drift(forven_db):
    """Insert a row, manually wipe the FTS index, then rebuild — search must work again."""
    decision_id = _insert_decision(f"drifty {TOKEN_INSERT}")
    task_id = _insert_agent_task(f"drift task {TOKEN_INSERT}", "x")
    audit_id = _insert_audit("t-drift", "tool", f"audit {TOKEN_INSERT}")

    # Simulate trigger bypass: tell each FTS index to forget its contents
    # without touching the source rows. Rebuild should restore them.
    with get_db() as conn:
        for fts in FTS5_TABLES:
            conn.execute(f"INSERT INTO {fts}({fts}) VALUES('delete-all')")
        # Sanity: nothing matches now.
        for fts in FTS5_TABLES:
            row = conn.execute(
                f"SELECT rowid FROM {fts} WHERE {fts} MATCH ?",
                (TOKEN_INSERT,),
            ).fetchone()
            assert row is None, f"{fts} should be empty after delete-all"

    counts = rebuild_fts5_indices()
    assert set(counts.keys()) == set(FTS5_TABLES)
    for name, count in counts.items():
        assert count >= 1, f"{name} should have at least the row we inserted"

    with get_db() as conn:
        bd = conn.execute(
            "SELECT rowid FROM brain_decisions_fts WHERE brain_decisions_fts MATCH ?",
            (TOKEN_INSERT,),
        ).fetchone()
        at = conn.execute(
            "SELECT rowid FROM agent_tasks_fts WHERE agent_tasks_fts MATCH ?",
            (TOKEN_INSERT,),
        ).fetchone()
        au = conn.execute(
            "SELECT rowid FROM task_audit_log_fts WHERE task_audit_log_fts MATCH ?",
            (TOKEN_INSERT,),
        ).fetchone()
    assert bd is not None and int(bd["rowid"]) == decision_id
    assert at is not None and int(at["rowid"]) == task_id
    assert au is not None and int(au["rowid"]) == audit_id


def test_fts5_rebuild_cli_command_runs(forven_db):
    """`forven fts5-rebuild` must exit 0 and print per-table counts."""
    from click.testing import CliRunner

    from forven.cli import cli

    _insert_decision(f"cli {TOKEN_INSERT}")

    runner = CliRunner()
    result = runner.invoke(cli, ["fts5-rebuild"])
    assert result.exit_code == 0, result.output
    for fts in FTS5_TABLES:
        assert fts in result.output
    assert "FTS5 indices rebuilt" in result.output
