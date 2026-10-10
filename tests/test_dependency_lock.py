"""requirements.lock is the fully hashed, reproducible install of the
package's dependencies plus the mcp extra, and it must agree with
pyproject.toml.

History: the lock once pinned rich==14.3.3 and pandas==3.0.1 against
`rich<14.0` / `pandas<3.0` in pyproject.toml while CI installed it with
dependency checks disabled, so every green run validated a set no user
could install. Later it pinned only the six direct dependencies under a
"pip freeze" header, so the audit resolved transitive versions at audit
time and the mcp extra (pydantic, starlette, httpx, ...) was never audited.

`packaging` is a hard dependency of pytest, so it is always importable here.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]

_PIN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)")


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def _requirements(names: list[str]) -> dict[str, Requirement]:
    reqs = [Requirement(r) for r in names]
    return {canonicalize_name(r.name): r for r in reqs}


def _pyproject_requirements() -> dict[str, Requirement]:
    return _requirements(_pyproject()["project"]["dependencies"])


def _mcp_extra() -> dict[str, Requirement]:
    return _requirements(_pyproject()["project"]["optional-dependencies"]["mcp"])


def _lock_entries() -> dict[str, tuple[Version, list[str]]]:
    """name -> (pinned version, hashes) from the hashed lock."""
    entries: dict[str, tuple[Version, list[str]]] = {}
    current: str | None = None
    for raw in (ROOT / "requirements.lock").read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash="):
            assert current is not None, f"hash without a requirement: {raw!r}"
            entries[current][1].append(line.split("=", 1)[1].rstrip(" \\"))
            continue
        m = _PIN_RE.match(line)
        assert m, f"requirements.lock line is not an exact pin: {raw!r}"
        current = canonicalize_name(m.group(1))
        entries[current] = (Version(m.group(2)), [])
    return entries


def test_every_lock_entry_is_pinned_and_hashed():
    entries = _lock_entries()
    assert len(entries) > 20  # direct deps plus the whole transitive closure
    for name, (_, hashes) in entries.items():
        assert hashes and all(h.startswith("sha256:") for h in hashes), name


@pytest.mark.parametrize("name", sorted(_pyproject_requirements()))
def test_lock_pin_satisfies_pyproject_range(name):
    req = _pyproject_requirements()[name]
    entries = _lock_entries()
    assert name in entries, f"{name} declared in pyproject.toml but not pinned in requirements.lock"
    assert req.specifier.contains(entries[name][0], prereleases=True), (
        f"requirements.lock pins {name}=={entries[name][0]} but pyproject.toml requires {req.specifier}"
    )


def test_the_mcp_extra_and_its_dependencies_are_locked():
    entries = _lock_entries()
    for name, req in _mcp_extra().items():
        assert name in entries and req.specifier.contains(entries[name][0], prereleases=True)
    # What the extra pulls in is audited too, not resolved at audit time.
    for transitive in ("pydantic", "starlette", "anyio", "uvicorn"):
        assert transitive in entries, transitive


@pytest.mark.parametrize("name", ["rich", "pandas"])
def test_tested_lower_bounds_are_the_lock_pins(name):
    """rich and pandas floors are the pins CI tests, so a user cannot land
    on a version the suite has never seen."""
    req = _pyproject_requirements()[name]
    floors = [Version(spec.version) for spec in req.specifier if spec.operator == ">="]
    assert floors == [_lock_entries()[name][0]]


def test_aiohttp_floor_excludes_the_advisories():
    """PYSEC-2026-3545, 3546 and 3547 affect aiohttp before 3.14.3; a user
    environment must not be able to keep a vulnerable server."""
    req = _pyproject_requirements()["aiohttp"]
    assert not req.specifier.contains("3.14.2") and req.specifier.contains("3.14.3")


def test_requirements_txt_mirrors_pyproject():
    """requirements.txt is a convenience copy; it must not drift."""
    py = _pyproject_requirements()
    txt = _requirements([
        line.strip() for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ])
    assert set(txt) == set(py)
    for name, req in py.items():
        assert str(txt[name].specifier) == str(req.specifier), name


def test_ci_installs_and_audits_the_hashed_lock():
    """The lock leg installs with --require-hashes and runs pip check (so a
    dependency missing from the lock fails), the audit reads exactly the
    hashed lock, and one leg installs what the README tells users to."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "pip install --require-hashes -r requirements.lock" in ci
    assert "pip check" in ci
    assert "pip-audit --require-hashes --disable-pip -r requirements.lock" in ci
    assert 'install: "pyproject"' in ci
