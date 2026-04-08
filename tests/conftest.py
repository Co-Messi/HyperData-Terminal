"""Pytest configuration and shared fixtures for HyperData tests."""
from __future__ import annotations

import sys
import os

# Ensure project root and src/ are importable
_PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "src"))


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
