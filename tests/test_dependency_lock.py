"""H1: requirements.lock must satisfy pyproject.toml's dependency ranges.

Pre-fix, the lock pinned rich==14.3.3 and pandas==3.0.1 against
`rich<14.0` / `pandas<3.0` in pyproject.toml, and CI installed the lock
with `pip install -e . --no-deps` — which is exactly what stopped pip from
noticing. Every green run validated a dependency set no user could install.

`packaging` is a hard dependency of pytest, so it is always importable here.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]

_PIN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s;#]+)")


def _pyproject_requirements() -> dict[str, Requirement]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    reqs = [Requirement(r) for r in data["project"]["dependencies"]]
    return {r.name.lower().replace("_", "-"): r for r in reqs}


def _lock_pins() -> dict[str, Version]:
    pins: dict[str, Version] = {}
    for line in (ROOT / "requirements.lock").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _PIN_RE.match(line)
        assert m, f"requirements.lock line is not an exact pin: {line!r}"
        pins[m.group(1).lower().replace("_", "-")] = Version(m.group(2))
    return pins


def _requirements_txt() -> dict[str, Requirement]:
    reqs = []
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            reqs.append(Requirement(line))
    return {r.name.lower().replace("_", "-"): r for r in reqs}


@pytest.mark.parametrize("name", sorted(_pyproject_requirements()))
def test_lock_pin_satisfies_pyproject_range(name):
    req = _pyproject_requirements()[name]
    pins = _lock_pins()
    assert name in pins, f"{name} declared in pyproject.toml but not pinned in requirements.lock"
    assert req.specifier.contains(pins[name], prereleases=True), (
        f"requirements.lock pins {name}=={pins[name]} but pyproject.toml requires {req.specifier}"
    )


@pytest.mark.parametrize("name", ["rich", "pandas"])
def test_tested_lower_bounds_are_the_lock_pins(name):
    """S6: H1 widened rich/pandas down to versions nothing ever tests (CI
    runs the lock pins, and the pyproject leg resolves to the newest in
    range). The floor must be the pin, so a user cannot land on a version
    the suite has never seen."""
    req = _pyproject_requirements()[name]
    floors = [Version(spec.version) for spec in req.specifier if spec.operator == ">="]
    assert floors == [_lock_pins()[name]]


def test_lock_has_no_undeclared_direct_deps():
    extra = set(_lock_pins()) - set(_pyproject_requirements())
    assert not extra, f"pinned in requirements.lock but not declared in pyproject.toml: {sorted(extra)}"


def test_requirements_txt_mirrors_pyproject():
    """requirements.txt is a convenience copy; it must not drift."""
    py = _pyproject_requirements()
    txt = _requirements_txt()
    assert set(txt) == set(py)
    for name, req in py.items():
        assert str(txt[name].specifier) == str(req.specifier), name


def test_ci_installs_with_dependencies():
    """The lock leg must not use --no-deps and must run `pip check`, or a
    contradiction between the lock and pyproject is invisible again."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "--no-deps" not in ci
    assert "pip check" in ci
    assert 'install: "pyproject"' in ci   # one leg installs exactly what the README says
