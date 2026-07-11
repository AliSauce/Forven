"""The runtime data-availability precheck must not hard-block sandbox-only
(imported/dropzone) strategies just because their class cannot be resolved in
the trusted parent — that is true BY DESIGN (the class loads only in the
worker), and their availability was already certified with the real class at
registration (a blocked verdict parks the strategy research_only at birth).
Pre-fix, every certified dropzone strategy re-blocked at quick_screen with
"Cannot verify data availability ... strategy class could not be resolved"
(the S06890 no-metrics chain; S06895's re-adjudication stall, 2026-07-11).
Non-sandbox unresolvable types keep the fail-closed hard block."""

from forven.strategies.data_availability import evaluate_data_availability


def test_sandbox_only_type_proceeds_with_warning(forven_db, monkeypatch):
    monkeypatch.setattr(
        "forven.strategies.backtest._resolve_strategy_class", lambda *_a, **_k: None
    )
    result = evaluate_data_availability(
        "imported__dropzone_btc_funding_trend_align_s63123_b1d5025a517a",
        "BTC",
        "4h",
        strategy_id="S06895",
    )
    assert result.blocked is False
    assert result.ok is True
    assert any("sandbox-only" in w for w in result.warnings)


def test_non_sandbox_unresolvable_type_still_blocks(forven_db, monkeypatch):
    monkeypatch.setattr(
        "forven.strategies.backtest._resolve_strategy_class", lambda *_a, **_k: None
    )
    result = evaluate_data_availability(
        "some_unknown_family",
        "BTC",
        "1h",
        strategy_id="S-UNKNOWN",
    )
    assert result.blocked is True
    assert "could not be resolved" in str(result.error)
