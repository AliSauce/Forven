"""The dashboard's entry/exit signal display must be DIRECTION-AWARE.

A reversal strategy (e.g. Donchian breakdown) emits, on a single bar, BOTH a short-entry
and a long-exit — the same event. Collapsing the four directional flags into one generic
``entry_signal``/``exit_signal`` made a held short read as "entry AND exit active at once".
The fix surfaces the four directional flags and scopes the display to the relevant side
(the held position's side, else the side whose entry is firing). Execution is unaffected —
the kernel always runs off the directional signals.
"""

from __future__ import annotations

import pandas as pd

import forven.scanner as sc
from forven.api_domains import paper as paper_domain
from forven.strategies.base import DirectionalSignals

# A Donchian-breakdown bar: short-entry + long-exit both fire; short-exit does NOT.
REVERSAL = {
    "entry_signal": True, "exit_signal": True, "atr_14": 12.0,
    "directional_signals": {"long_entry": False, "short_entry": True, "long_exit": True, "short_exit": False},
}


# ── scanner: surface the four directional flags ───────────────────────────────────────

def test_latest_directional_signals_extracts_last_bar():
    idx = pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC")

    class _Strat:
        def generate_signals(self, df):
            return DirectionalSignals(
                long_entries=pd.Series([False, False, False], index=idx),
                short_entries=pd.Series([False, False, True], index=idx),
                long_exits=pd.Series([False, False, True], index=idx),
                short_exits=pd.Series([False, False, False], index=idx),
            )

    assert sc._latest_directional_signals(_Strat(), pd.DataFrame(index=idx)) == {
        "long_entry": False, "short_entry": True, "long_exit": True, "short_exit": False,
    }


def test_latest_directional_signals_none_for_non_directional():
    class _Legacy:
        def generate_signals(self, df):
            return None  # legacy single-side strategy

    assert sc._latest_directional_signals(_Legacy(), None) is None


# ── display: scope to the relevant side ───────────────────────────────────────────────

def test_scoped_entry_exit_held_short_drops_cross_side_long_exit():
    # held SHORT: entry yes (short_entry), exit NO — the firing exit is the LONG exit.
    assert paper_domain._scoped_entry_exit(REVERSAL, {"short"}) == (True, False)


def test_scoped_entry_exit_held_long_honours_long_exit():
    # held LONG: the long_exit DOES apply; long_entry is 0.
    assert paper_domain._scoped_entry_exit(REVERSAL, {"long"}) == (False, True)


def test_scoped_entry_exit_flat_scopes_to_firing_entry_side():
    # flat: scope to the side whose entry is firing (short); no position → no exit.
    assert paper_domain._scoped_entry_exit(REVERSAL, set()) == (True, False)


def test_scoped_entry_exit_legacy_snapshot_falls_back_to_collapsed():
    # no directional flags → unchanged collapsed behaviour (backward compatible).
    assert paper_domain._scoped_entry_exit({"entry_signal": True, "exit_signal": True}, {"short"}) == (True, True)


def test_build_session_runtime_fields_direction_scoped():
    ind, pending, last = paper_domain._build_session_runtime_fields(REVERSAL, "t", position_sides={"short"})
    assert ind["entry_signal"]["value"] == 1.0
    assert ind["exit_signal"]["value"] == 0.0                 # the false cross-side exit is gone
    assert [s["signal_type"] for s in pending] == ["entry"]   # no phantom "exit" pending signal
    assert last == "entry"
    assert "directional_signals" not in ind                   # carrier, not a chart indicator
    assert ind["atr_14"]["value"] == 12.0                     # other indicators preserved
