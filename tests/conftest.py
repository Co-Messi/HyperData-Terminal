"""Pytest configuration and shared fixtures for HyperData tests."""
from __future__ import annotations

import os
import tempfile

# Point every on-disk store at a throwaway directory BEFORE the package is
# imported: hyperdata_terminal.paths resolves the data dir once, at import,
# and the suite must never touch a developer's real wallet history.
os.environ["HYPERDATA_DATA_DIR"] = tempfile.mkdtemp(prefix="hyperdata-tests-")


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False, help="Run live exchange tests")


def pytest_configure(config):
    config.addinivalue_line("markers", "live: mark test as requiring live exchange connections")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live"):
        skip_live = __import__("pytest").mark.skip(reason="Need --live option to run")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)
