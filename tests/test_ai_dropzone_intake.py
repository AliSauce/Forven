from __future__ import annotations

import importlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import forven.brain as brain_mod
from forven.db import create_strategy_container, get_db
from forven.strategies import custom as custom_pkg
from forven.strategies import imported as imported_pkg
from forven.strategies import intake as intake_mod
from forven.strategies import registry
from forven.strategy_lifecycle import StrategyPromoteBody, promote_strategy, read_strategies


@pytest.fixture(autouse=True)
def isolated_custom_validation(monkeypatch, tmp_path):
    from forven.sandbox import strategy_worker

    imported_dir = tmp_path / "imported"
    imported_dir.mkdir()
    imported_init = imported_dir / "__init__.py"
    imported_init.write_text("", encoding="utf-8")
    monkeypatch.setattr(imported_pkg, "__file__", str(imported_init))

    def validate(module_name: str, *, package: str = "custom", timeout: int = 0):
        del timeout
        package_obj = custom_pkg if package == "custom" else imported_pkg
        module_path = package_obj.__file__
        source = (module_path and (Path(module_path).resolve().parent / f"{module_name}.py"))
        text = source.read_text(encoding="utf-8") if source and source.exists() else ""
        match = re.search(r"^TYPE_NAME\s*=\s*['\"]([^'\"]+)", text, re.MULTILINE)
        type_name = match.group(1) if match else module_name
        params = {"risk_pct": 0.01, "leverage": 1.0}
        return {
            "ok": True,
            "type_name": type_name,
            "default_params": params,
            "canonical_params": params,
            "asset": "BTC",
            "data_requirements": [{"asset": "BTC"}],
            "parameter_space": {},
            "certified": True,
            "cert_error": None,
            "lookahead_blocked": False,
            "lookahead_reason": None,
            "execution_crash_reason": None,
        }

    monkeypatch.setattr(strategy_worker, "validate_custom_module_isolated", validate)


def _write_custom_strategy(
    path,
    *,
    type_name: str = "ai_dropzone_wave_test",
    embedded_hypothesis_id: str | None = None,
) -> None:
    lines = [
        "import pandas as pd",
        "from forven.strategies.base import BaseStrategy, Signal",
        "",
    ]
    if embedded_hypothesis_id:
        lines.extend(
            [
                f'FORVEN_HYPOTHESIS_ID = "{embedded_hypothesis_id}"',
                "",
            ]
        )
    lines.extend(
        [
            "class AIDropzoneWave(BaseStrategy):",
            "    @property",
            "    def name(self) -> str:",
            "        return 'AI Dropzone Wave'",
            "",
            "    @property",
            "    def asset(self) -> str:",
            "        return 'BTC'",
            "",
            "    @property",
            "    def strategy_type(self) -> str:",
            "        return TYPE_NAME",
            "",
            "    @property",
            "    def default_params(self) -> dict:",
            "        return {'risk_pct': 0.01, 'leverage': 1.0}",
            "",
            "    def generate_signal(self, df: pd.DataFrame) -> Signal:",
            "        price = float(df['close'].iloc[-1]) if 'close' in df and len(df.index) else 0.0",
            "        return Signal(price=price)",
            "",
            "STRATEGY_CLASS = AIDropzoneWave",
            f"TYPE_NAME = '{type_name}'",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def test_register_custom_strategy_file_creates_quick_screen_ai_row(forven_db, monkeypatch, tmp_path):
    temp_custom_dir = tmp_path / "custom"
    temp_custom_dir.mkdir()
    strategy_file = temp_custom_dir / "btc_ai_dropzone_wave_test.py"
    _write_custom_strategy(strategy_file)

    monkeypatch.setattr(custom_pkg, "__path__", [str(temp_custom_dir)])
    monkeypatch.setattr(custom_pkg, "__file__", str(temp_custom_dir / "__init__.py"))

    registry.reset()
    importlib.invalidate_caches()
    sys.modules.pop("forven.strategies.custom.btc_ai_dropzone_wave_test", None)

    result = intake_mod.register_custom_strategy_file(file_path=str(strategy_file))

    assert result["module_name"] == "btc_ai_dropzone_wave_test"
    assert result["stage"] == "quick_screen"
    assert result["source"] == "ai_dropzone"
    assert Path(result["source_ref"]).parent == Path(imported_pkg.__file__).resolve().parent
    assert result["strategy_id"]

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, source, source_ref, type, runtime_type, sandbox_only "
            "FROM strategies WHERE id = ?",
            (result["strategy_id"],),
        ).fetchone()

    assert row is not None
    assert str(row["stage"]) == "quick_screen"
    assert str(row["source"]) == "ai_dropzone"
    assert str(row["source_ref"]) == result["source_ref"]
    assert str(row["type"]) == "ai_dropzone_wave_test"
    assert str(row["runtime_type"]).startswith("imported__dropzone_")
    assert row["sandbox_only"] == 1
    assert strategy_file.exists() is False
    assert "forven.strategies.custom.btc_ai_dropzone_wave_test" not in sys.modules


def test_scan_custom_strategies_registers_active_file_once(forven_db, monkeypatch, tmp_path):
    temp_custom_dir = tmp_path / "custom"
    temp_custom_dir.mkdir()
    strategy_file = temp_custom_dir / "btc_ai_dropzone_wave_test.py"
    _write_custom_strategy(strategy_file)

    monkeypatch.setattr(custom_pkg, "__path__", [str(temp_custom_dir)])
    monkeypatch.setattr(custom_pkg, "__file__", str(temp_custom_dir / "__init__.py"))

    registry.reset()
    importlib.invalidate_caches()
    sys.modules.pop("forven.strategies.custom.btc_ai_dropzone_wave_test", None)

    # Dry-run scan: discovers but does NOT create DB rows
    dry_result = intake_mod.scan_custom_strategies()
    assert dry_result["scanned"] == 1
    assert dry_result["new_count"] == 1
    assert dry_result["registered"] is False
    assert "forven.strategies.custom.btc_ai_dropzone_wave_test" not in sys.modules

    with get_db() as conn:
        rows = conn.execute("SELECT id FROM strategies").fetchall()
    assert len(rows) == 0, "Dry-run scan must not create DB rows"

    # Register scan: creates DB containers
    result = intake_mod.scan_custom_strategies(register=True)

    assert result["scanned"] == 1
    assert result["new_count"] == 1
    assert result["already_known"] == 0
    assert result["error_count"] == 0
    assert result["registered"] is True
    assert result["new_strategies"][0]["type_name"] == "ai_dropzone_wave_test"

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, type FROM strategies ORDER BY created_at"
        ).fetchall()

    assert len(rows) == 1
    assert str(rows[0]["type"]) == "ai_dropzone_wave_test"


def test_scan_custom_strategies_skips_archived_modules_by_default(forven_db, monkeypatch, tmp_path):
    temp_custom_dir = tmp_path / "custom"
    temp_custom_dir.mkdir()
    active_file = temp_custom_dir / "btc_active_wave.py"
    archived_file = temp_custom_dir / "btc_archived_wave_s00123.py"
    _write_custom_strategy(active_file, type_name="active_wave_test")
    _write_custom_strategy(archived_file, type_name="archived_wave_test")

    monkeypatch.setattr(custom_pkg, "__path__", [str(temp_custom_dir)])
    monkeypatch.setattr(custom_pkg, "__file__", str(temp_custom_dir / "__init__.py"))

    registry.reset()
    importlib.invalidate_caches()
    sys.modules.pop("forven.strategies.custom.btc_active_wave", None)
    sys.modules.pop("forven.strategies.custom.btc_archived_wave_s00123", None)

    result = intake_mod.scan_custom_strategies(register=True)

    assert result["scanned"] == 2
    assert result["new_count"] == 1
    assert result["already_known"] == 1
    assert result["error_count"] == 0
    assert result["new_strategies"][0]["module_name"] == "btc_active_wave"

    with get_db() as conn:
        rows = conn.execute(
            "SELECT type FROM strategies ORDER BY created_at"
        ).fetchall()

    assert [str(row["type"]) for row in rows] == ["active_wave_test"]


def test_register_custom_strategy_file_rejects_duplicate_db_strategy(forven_db, monkeypatch, tmp_path):
    temp_custom_dir = tmp_path / "custom"
    temp_custom_dir.mkdir()
    strategy_file = temp_custom_dir / "btc_ai_dropzone_wave_test.py"
    _write_custom_strategy(strategy_file)

    monkeypatch.setattr(custom_pkg, "__path__", [str(temp_custom_dir)])
    monkeypatch.setattr(custom_pkg, "__file__", str(temp_custom_dir / "__init__.py"))

    with get_db() as conn:
        create_strategy_container(
            conn=conn,
            name="ignored",
            type_="ai_dropzone_wave_test",
            symbol="BTC",
            timeframe="1h",
            params={"risk_pct": 0.01, "leverage": 1.0},
            source="ai_dropzone",
            source_ref=str(strategy_file.resolve()),
        )

    registry.reset()
    importlib.invalidate_caches()
    sys.modules.pop("forven.strategies.custom.btc_ai_dropzone_wave_test", None)

    with pytest.raises(ValueError, match="already registered"):
        intake_mod.register_custom_strategy_file(file_path=str(strategy_file))


def test_read_strategies_exposes_ai_dropzone_provenance_and_backtest_flag(forven_db):
    with get_db() as conn:
        strategy_id, _, _ = create_strategy_container(
            conn=conn,
            name="ignored",
            type_="macd",
            symbol="BTC",
            timeframe="1h",
            params={"fast": 12, "slow": 26, "signal": 9},
            source="ai_dropzone",
            source_ref="btc_ai_dropzone_wave_test.py",
        )

    rows = read_strategies()
    row = next(item for item in rows if item["id"] == strategy_id)
    assert row["source"] == "ai_dropzone"
    assert row["source_ref"] == "btc_ai_dropzone_wave_test.py"
    assert row["has_backtest_results"] is False

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at)
            VALUES (?, ?, 'backtest', ?, ?, ?, ?, ?)
            """,
            (
                "B90001",
                strategy_id,
                "BTC",
                "1h",
                "{}",
                '{"dataset":"btc-1h"}',
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    rows = read_strategies()
    row = next(item for item in rows if item["id"] == strategy_id)
    assert row["has_backtest_results"] is True


def test_promote_strategy_blocks_unbacktested_ai_dropzone_rows(forven_db):
    with get_db() as conn:
        strategy_id, _, _ = create_strategy_container(
            conn=conn,
            name="ignored",
            type_="macd",
            symbol="BTC",
            timeframe="1h",
            params={"fast": 12, "slow": 26, "signal": 9},
            source="ai_dropzone",
            source_ref="btc_ai_dropzone_wave_test.py",
        )

    result = promote_strategy(strategy_id, StrategyPromoteBody(to_status="gauntlet"))

    assert result["ok"] is False
    assert "completed backtest" in str(result["error"]).lower()


def test_promote_strategy_allows_ai_dropzone_rows_after_backtest(forven_db, monkeypatch):
    with get_db() as conn:
        strategy_id, _, _ = create_strategy_container(
            conn=conn,
            name="ignored",
            type_="macd",
            symbol="BTC",
            timeframe="1h",
            params={"fast": 12, "slow": 26, "signal": 9},
            source="ai_dropzone",
            source_ref="btc_ai_dropzone_wave_test.py",
        )
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at)
            VALUES (?, ?, 'backtest', ?, ?, ?, ?, ?)
            """,
            (
                "B90002",
                strategy_id,
                "BTC",
                "1h",
                "{}",
                '{"dataset":"btc-1h"}',
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    monkeypatch.setattr(
        brain_mod,
        "transition_stage",
        lambda **_kwargs: {"from": "quick_screen", "to": "gauntlet"},
    )

    result = promote_strategy(strategy_id, StrategyPromoteBody(to_status="gauntlet"))

    assert result["ok"] is True
    assert result["to_status"] == "gauntlet"


def test_promote_strategy_keeps_non_ai_rows_unchanged(forven_db, monkeypatch):
    with get_db() as conn:
        strategy_id, _, _ = create_strategy_container(
            conn=conn,
            name="ignored",
            type_="macd",
            symbol="BTC",
            timeframe="1h",
            params={"fast": 12, "slow": 26, "signal": 9},
        )

    monkeypatch.setattr(
        brain_mod,
        "transition_stage",
        lambda **_kwargs: {"from": "quick_screen", "to": "gauntlet"},
    )

    result = promote_strategy(strategy_id, StrategyPromoteBody(to_status="gauntlet"))

    assert result["ok"] is True
    assert result["to_status"] == "gauntlet"


def test_auto_intake_recent_files_requires_embedded_hypothesis_id(forven_db, monkeypatch, tmp_path):
    temp_custom_dir = tmp_path / "custom"
    temp_custom_dir.mkdir()
    strategy_file = temp_custom_dir / "btc_recent_auto_intake.py"
    _write_custom_strategy(strategy_file, type_name="recent_auto_intake")

    monkeypatch.setattr(custom_pkg, "__path__", [str(temp_custom_dir)])
    monkeypatch.setattr(custom_pkg, "__file__", str(temp_custom_dir / "__init__.py"))

    registry.reset()
    importlib.invalidate_caches()
    sys.modules.pop("forven.strategies.custom.btc_recent_auto_intake", None)

    result = intake_mod.auto_intake_recent_files(max_age_minutes=10)

    assert result["checked"] == 1
    assert result["registered"] == 0
    assert result["errors"]
    assert "embedded hypothesis_id" in str(result["errors"][0]["error"]).lower()

    with get_db() as conn:
        rows = conn.execute("SELECT id FROM strategies").fetchall()

    assert rows == []


def test_auto_intake_recent_files_links_embedded_hypothesis_id(forven_db, monkeypatch, tmp_path):
    from forven.hypotheses import create_hypothesis

    hypothesis = create_hypothesis(
        title="Recent auto-intake lineage",
        market_thesis="Fresh custom strategy files should stay linked to their parent hypothesis.",
        mechanism="Read an embedded hypothesis marker before auto-intake creates a container.",
        lane="exploration",
        source_type="agent_original",
        target_assets=["BTC"],
        target_timeframes=["1h"],
    )

    temp_custom_dir = tmp_path / "custom"
    temp_custom_dir.mkdir()
    strategy_file = temp_custom_dir / "btc_recent_hypothesis_linked.py"
    _write_custom_strategy(
        strategy_file,
        type_name="recent_hypothesis_linked",
        embedded_hypothesis_id=hypothesis["id"],
    )

    monkeypatch.setattr(custom_pkg, "__path__", [str(temp_custom_dir)])
    monkeypatch.setattr(custom_pkg, "__file__", str(temp_custom_dir / "__init__.py"))

    registry.reset()
    importlib.invalidate_caches()
    sys.modules.pop("forven.strategies.custom.btc_recent_hypothesis_linked", None)

    result = intake_mod.auto_intake_recent_files(max_age_minutes=10)

    assert result["checked"] == 1
    assert result["registered"] == 1
    assert result["errors"] == []

    with get_db() as conn:
        row = conn.execute(
            "SELECT source, hypothesis_id FROM strategies WHERE type = ?",
            ("recent_hypothesis_linked",),
        ).fetchone()

    assert row is not None
    assert row["source"] == "auto_intake"
    assert row["hypothesis_id"] == hypothesis["id"]
