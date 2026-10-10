"""Pytest configuration and shared fixtures for HyperData tests."""
from __future__ import annotations

import os
import tempfile

import pytest

# Point every on-disk store at a throwaway directory BEFORE the package is
# imported: hyperdata_terminal.paths resolves the data dir once, at import,
# and the suite must never touch a developer's real wallet history.
os.environ["HYPERDATA_DATA_DIR"] = tempfile.mkdtemp(prefix="hyperdata-tests-")
# Same for the cross-process Hyperliquid budget registry: a test hub must
# never register next to (and share a budget with) a real running instance.
os.environ["HYPERDATA_HL_REGISTRY_DIR"] = tempfile.mkdtemp(prefix="hyperdata-hl-registry-tests-")


@pytest.fixture(autouse=True)
def _fresh_hl_governor():
    """Each test starts with an empty Hyperliquid weight window: the
    governor is process wide, so weight charged by one test would otherwise
    make the next one wait."""
    from hyperdata_terminal.data_layer import hl_rate

    hl_rate.reset_governor()
    yield
    hl_rate.reset_governor()


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False, help="Run live exchange tests")


def pytest_configure(config):
    config.addinivalue_line("markers", "live: mark test as requiring live exchange connections")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live"):
        skip_live = pytest.mark.skip(reason="Need --live option to run")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)
