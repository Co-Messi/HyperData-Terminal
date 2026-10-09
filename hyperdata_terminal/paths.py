"""Where HyperData keeps its state on disk.

Everything the terminal writes (the SQLite stores, logs, the paper-trade
ledger, the smart-money anchor list) lives under one directory:

1. ``HYPERDATA_DATA_DIR`` when it is set.
2. ``<checkout>/data`` when running from a source checkout that already has
   one, so existing clones keep their accumulated wallet history.
3. Otherwise the platform's per-user data directory. An installed package
   (pipx, uvx, a wheel) must never write into site-packages.

The directory is resolved once, at import, and created lazily by whoever
writes first. Set ``HYPERDATA_DATA_DIR`` before importing the package to
move it (the test suite does exactly that).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "hyperdata"

_PACKAGE_DIR = Path(__file__).resolve().parent


def _platform_data_dir() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    return Path(base) / APP_NAME


def _legacy_checkout_dir() -> Path | None:
    """``<repo>/data`` for a source checkout that already has state in it."""
    root = _PACKAGE_DIR.parent
    legacy = root / "data"
    if (root / "pyproject.toml").is_file() and legacy.is_dir():
        return legacy
    return None


def resolve_data_dir() -> Path:
    override = os.environ.get("HYPERDATA_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return _legacy_checkout_dir() or _platform_data_dir()


DATA_DIR: Path = resolve_data_dir()
LOG_DIR: Path = DATA_DIR / "logs"
