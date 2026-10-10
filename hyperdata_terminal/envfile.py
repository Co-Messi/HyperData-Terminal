"""Load settings from ``.env`` files without letting a checkout reconfigure
network exposure.

The ``hyperdata`` command reads two files, never overriding the real
environment:

1. ``.env`` in the working directory: alert webhooks, LLM model and key,
   thresholds. It cannot set PROTECTED_KEYS. A cloned repository's
   ``.env`` (and an MCP server's working directory is usually whatever
   project the client has open) must not be able to bind the API to every
   interface without auth, pick its key, allow browser origins, move the
   data dir, or point the LLM strategy (and the user's LLM_API_KEY) at
   another server. Such keys are ignored with a warning.
2. ``<data dir>/.env``: the user's own state directory, trusted for every
   key, including the protected ones.
"""
from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path

PROTECTED_KEYS = frozenset({
    "HYPERDATA_API_HOST",
    "HYPERDATA_API_PORT",
    "HYPERDATA_API_KEY",
    "HYPERDATA_UNSAFE_PUBLIC_API",
    "HYPERDATA_CORS_ORIGINS",
    "HYPERDATA_DATA_DIR",
    "HYPERDATA_HL_REGISTRY_DIR",
    "LLM_BASE_URL",
})


def _values(path: Path) -> dict[str, str]:
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def load_env(cwd: Path | None = None, environ: MutableMapping[str, str] | None = None,
             data_dir: Path | None = None) -> list[str]:
    """Apply the working directory's and the data dir's ``.env`` files.

    Returns the protected keys the working directory's file tried to set
    (and that were ignored), so the caller can warn once logging is up.
    """
    environ = os.environ if environ is None else environ
    ignored: list[str] = []
    cwd_file = (cwd or Path.cwd()) / ".env"
    if cwd_file.is_file():
        for key, value in _values(cwd_file).items():
            if key in PROTECTED_KEYS:
                if environ.get(key) != value:
                    ignored.append(key)
                continue
            environ.setdefault(key, value)

    if data_dir is None:
        # HYPERDATA_DATA_DIR comes only from the real environment by now.
        from hyperdata_terminal.paths import resolve_data_dir

        data_dir = resolve_data_dir()
    data_file = data_dir / ".env"
    if data_file.is_file() and data_file.resolve() != cwd_file.resolve():
        for key, value in _values(data_file).items():
            if key != "HYPERDATA_DATA_DIR":
                environ.setdefault(key, value)
    return ignored
