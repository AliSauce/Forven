"""Boot-time dependency preflight.

Verify the running interpreter actually satisfies forven's DECLARED dependencies
(``pyproject.toml -> [project.dependencies]`` — the single source of truth) rather
than a hand-maintained probe list that silently drifts. The old launcher check
imported only 4 modules (forven.api / uvicorn / websockets / multipart), so a
missing *lazily*-imported dependency (feedparser, trafilatura, yt-dlp, yfinance,
mcp, ...) sailed straight through boot and only blew up later mid-operation.

Design notes:
- Driven by **distribution metadata** (``importlib.metadata.version``), so the
  import-name vs distribution-name mismatches that make import-probing fragile
  (PyYAML->yaml, beautifulsoup4->bs4, python-multipart->multipart, scikit-learn
  ->sklearn) cannot cause false misses.
- **Import-light**: only stdlib at module load (``packaging`` is imported lazily
  inside the checker), so `python -m forven.preflight` runs even from a source
  checkout via PYTHONPATH before anything is installed.
- Cross-platform: both start_all.ps1 (Windows) and start_all.sh (Unix) call it,
  so the fix covers every user, not just this machine.
"""
from __future__ import annotations

import importlib.metadata as _md
import sys
from collections.abc import Callable
from pathlib import Path

_DIST_NAME = "forven"


def _declared_requirements() -> list[str]:
    """The declared dependency specs.

    Prefer ``pyproject.toml`` next to the package (a source / editable checkout —
    the freshest truth, catches a newly-declared dep that was never reinstalled),
    and fall back to installed wheel metadata otherwise.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib

            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            deps = data.get("project", {}).get("dependencies")
            if isinstance(deps, list) and deps:
                return [str(d) for d in deps]
        except Exception:
            pass  # fall through to metadata
    try:
        return [str(r) for r in (_md.requires(_DIST_NAME) or [])]
    except _md.PackageNotFoundError:
        return []


def _bare_name(spec: str) -> str:
    """Best-effort distribution name from a requirement spec, without packaging."""
    name = spec.split(";", 1)[0].split("[", 1)[0]
    for op in ("<=", ">=", "==", "~=", "!=", "<", ">", "==="):
        name = name.split(op, 1)[0]
    return name.strip()


def _problems(requirements: list[str], version_of: Callable[[str], str | None]) -> list[str]:
    """Pure core: given dependency specs and a ``name -> installed version | None``
    lookup, return one human-readable message per UNSATISFIED dependency (missing,
    or installed version violating the declared specifier). Empty == all good.

    Kept free of I/O so it is deterministically unit-testable.
    """
    try:
        from packaging.requirements import Requirement
    except Exception:
        Requirement = None  # packaging missing -> degrade to presence-only

    out: list[str] = []
    for spec in requirements:
        specifier = None
        if Requirement is not None:
            try:
                req = Requirement(spec)
            except Exception:
                continue
            # Skip deps whose environment marker doesn't apply (extras, os, etc.).
            if req.marker is not None and not req.marker.evaluate():
                continue
            name = req.name
            specifier = req.specifier if str(req.specifier) else None
        else:
            name = _bare_name(spec)
            if not name:
                continue

        version = version_of(name)
        if version is None:
            need = f" (need {specifier})" if specifier is not None else ""
            out.append(f"{name}: not installed{need}")
        elif specifier is not None and version not in specifier:
            out.append(f"{name}: {version} installed, need {specifier}")
    return out


def _installed_version(name: str) -> str | None:
    try:
        return _md.version(name)
    except _md.PackageNotFoundError:
        return None


def check_dependencies() -> list[str]:
    """Return one message per missing/incompatible declared dependency ([] == ok)."""
    return _problems(_declared_requirements(), _installed_version)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--print-deps" in argv:
        # Emit the declared specs (one per line) so a launcher's install fallback
        # can pip-install exactly what pyproject declares, with no drift.
        for spec in _declared_requirements():
            print(spec)
        return 0

    problems = check_dependencies()
    if not problems:
        print("forven preflight: all declared dependencies satisfied")
        return 0

    print("forven preflight: MISSING or INCOMPATIBLE dependencies:", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)

    if "--install" in argv:
        import subprocess

        repo = Path(__file__).resolve().parent.parent
        print(f"forven preflight: pip install -e . into {sys.executable}", file=sys.stderr)
        rc = subprocess.call([sys.executable, "-m", "pip", "install", "-e", str(repo)])
        if rc != 0:
            print("forven preflight: pip install failed", file=sys.stderr)
            return rc
        remaining = check_dependencies()
        if remaining:
            print("forven preflight: STILL unsatisfied after install:", file=sys.stderr)
            for p in remaining:
                print(f"  - {p}", file=sys.stderr)
            return 1
        print("forven preflight: dependencies satisfied after install")
        return 0

    print(
        "\nFix: run `pip install -e .` in your forven checkout "
        "(or re-run this with --install).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
