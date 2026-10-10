"""The release workflow may publish to PyPI only after the tagged commit
passes the full test suite (M10): a PyPI release cannot be replaced."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _jobs(text: str) -> dict[str, str]:
    """Top-level jobs of a workflow file: name -> body (indentation based)."""
    body = text.split("\njobs:\n", 1)[1]
    jobs: dict[str, str] = {}
    for match in re.finditer(r"^  ([A-Za-z0-9_-]+):\n((?:    .*\n|\s*\n)*)", body, re.M):
        jobs[match.group(1)] = match.group(2)
    return jobs


def test_publish_waits_for_build_and_build_waits_for_the_tests():
    jobs = _jobs((ROOT / ".github" / "workflows" / "release.yml").read_text())
    assert "needs: build" in jobs["publish"]
    assert "needs: test" in jobs["build"]
    test = jobs["test"]
    assert "python -m pytest tests/" in test
    assert "ruff check" in test
    assert "pip check" in test
    assert "pip install --require-hashes -r requirements.lock" in test
    assert "pip-audit --require-hashes --disable-pip -r requirements.lock" in test
    assert '"3.12", "3.13"' in test


def test_release_smoke_test_imports_every_entry_point():
    build = _jobs((ROOT / ".github" / "workflows" / "release.yml").read_text())["build"]
    for module in ("hyperdata_terminal.cli", "hyperdata_terminal.mcp_server", "hyperdata_terminal.api_server",
                   "hyperdata_terminal.verify_data", "hyperdata_terminal.strategies.paper_trader"):
        assert module in build
    assert "hyperdata alerts --test" in build
    assert "pip install --require-hashes -r .github/build-requirements.txt" in build


def test_build_tooling_is_hash_pinned():
    lock = (ROOT / ".github" / "build-requirements.txt").read_text()
    assert "build==1.3.0" in lock and "--hash=sha256:" in lock
