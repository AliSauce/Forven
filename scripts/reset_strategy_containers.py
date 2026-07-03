#!/usr/bin/env python3
"""Nuclear reset for strategy containers and related backtest metadata."""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from forven.config import FORVEN_DB, FORVEN_HOME, ensure_dirs  # noqa: E402
from forven.db import get_db, init_db  # noqa: E402


RESET_TABLES = (
    "strategies",
    "trades",
    "portfolio_positions",
    "backtest_result_trash",
    "backtest_runs",
    "strategy_events",
    "strategy_candidates",
    # Added for container-first schema (PR2+) so reset stays complete.
    "backtest_results",
)
COUNTER_PREFIXES = ("S", "B", "E", "T")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _default_backup_dir() -> Path:
    return FORVEN_HOME / "backups" / f"container-reset-{_utc_stamp()}"


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _backup_sqlite(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        return
    with sqlite3.connect(str(src)) as source_conn:
        with sqlite3.connect(str(dst)) as dest_conn:
            source_conn.backup(dest_conn)


def backup_state(backup_dir: Path) -> dict[str, str | None]:
    backup_dir.mkdir(parents=True, exist_ok=True)

    db_backup_path = backup_dir / FORVEN_DB.name
    _backup_sqlite(FORVEN_DB, db_backup_path)

    return {
        "db_backup": str(db_backup_path) if db_backup_path.exists() else None,
    }


def reset_sqlite_tables() -> list[str]:
    reset_applied: list[str] = []
    with get_db() as conn:
        for table_name in RESET_TABLES:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
                raise ValueError(f"Unsafe table name rejected: {table_name}")
            if not _table_exists(conn, table_name):
                continue
            conn.execute(f"DELETE FROM {table_name}")
            reset_applied.append(table_name)

        conn.execute("DELETE FROM container_counters")
        for prefix in COUNTER_PREFIXES:
            conn.execute(
                "INSERT OR REPLACE INTO container_counters (prefix, next_val) VALUES (?, 1)",
                (prefix,),
            )
    return reset_applied


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backup and reset strategy container data (SQLite)."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Execute the destructive reset. Without this flag, the script only prints the plan.",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help="Optional backup directory. Default: <FORVEN_HOME>/backups/container-reset-<UTC timestamp>",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_dirs()
    init_db()

    backup_dir = (args.backup_dir or _default_backup_dir()).resolve()
    print("Container reset plan")
    print(f"- SQLite DB: {FORVEN_DB}")
    print(f"- Backup dir: {backup_dir}")
    print(f"- Tables: {', '.join(RESET_TABLES)}")
    print("- Counter seeds: S=1, B=1, E=1, T=1")

    if not args.yes:
        print("\nDry run only. Re-run with --yes to execute.")
        return 0

    backup_info = backup_state(backup_dir)
    reset_applied = reset_sqlite_tables()

    print("\nReset complete.")
    print(f"- DB backup: {backup_info['db_backup'] or 'not created (source missing)'}")
    print(f"- Truncated tables: {', '.join(reset_applied) if reset_applied else 'none'}")
    print("- Counters reseeded: S, B, E, T")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
