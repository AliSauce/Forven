"""Boot-time dependency preflight.

Guards the root fix for the "missing dependency only blows up mid-operation"
class: the launcher dep-check must be driven by the DECLARED dependency set
(pyproject) and catch missing/incompatible deps by distribution metadata, not by
a hand-maintained import probe that silently drifts.
"""
from __future__ import annotations

from forven import preflight


def test_reports_missing_and_version_mismatch_but_not_satisfied():
    reqs = ["foo>=1.0", "bar>=2.0", "baz"]
    installed = {"foo": "1.5", "bar": "1.0"}  # foo ok, bar too old, baz absent
    problems = preflight._problems(reqs, lambda n: installed.get(n))
    blob = " ".join(problems)

    assert "foo" not in blob          # satisfied -> not reported
    assert any("bar" in p for p in problems)   # version too low
    assert any("baz" in p for p in problems)   # missing entirely
    assert len(problems) == 2


def test_clean_when_all_satisfied():
    assert preflight._problems(["foo>=1.0", "bar"], lambda n: "9.9") == []


def test_environment_marker_that_does_not_apply_is_skipped():
    # An extra-gated dep (e.g. the discord extra) must NOT be required in a base env.
    reqs = ["discord.py>=2.3; extra == 'discord'"]
    # Even though it's "absent", the marker doesn't apply -> no problem reported.
    assert preflight._problems(reqs, lambda n: None) == []


def test_distribution_name_not_import_name():
    # The check keys off the DISTRIBUTION name, so dist/import mismatches resolve.
    reqs = ["PyYAML>=6.0", "beautifulsoup4>=4.12", "python-multipart>=0.0.9"]
    installed = {"PyYAML": "6.0.1", "beautifulsoup4": "4.12.3", "python-multipart": "0.0.9"}
    assert preflight._problems(reqs, lambda n: installed.get(n)) == []


def test_declared_requirements_reads_pyproject():
    reqs = preflight._declared_requirements()
    assert isinstance(reqs, list) and reqs
    names = {preflight._bare_name(r).lower() for r in reqs}
    # A handful of core deps that must always be declared.
    assert {"fastapi", "uvicorn", "pandas", "ccxt"} <= names


def test_real_environment_satisfies_declared_dependencies():
    # The test environment installs forven's deps, so the live check must be clean.
    # This is the regression guard: if a pyproject dep's distribution name can't be
    # resolved (or a real dep goes missing), this fails loudly at CI time.
    assert preflight.check_dependencies() == []
