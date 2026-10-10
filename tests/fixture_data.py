"""Load exchange payloads captured from the real public endpoints.

Every file under tests/fixtures/ is ``{"_meta": {source, captured, note},
"payload": <the exchange's own JSON, unmodified>}``. Exchange-semantics
tests use these instead of hand-written shapes, so a test can only pass if
the code handles what the exchange actually sends.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(relative: str) -> Any:
    """The captured payload stored at tests/fixtures/<relative>."""
    return json.loads((FIXTURES / relative).read_text())["payload"]


def load_jsonl_fixture(relative: str) -> list[Any]:
    """Captured frames, one JSON payload per line (``_meta`` lines skipped)."""
    frames = []
    for line in (FIXTURES / relative).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if isinstance(obj, dict) and set(obj) == {"_meta"}:
            continue
        frames.append(obj)
    return frames
