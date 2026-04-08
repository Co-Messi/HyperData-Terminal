"""
SQLite-backed store for discovered wallet addresses.

Replaces the previous JSON file (discovered_addresses.json) which had
race conditions between PositionScanner and SmartMoneyEngine writers.
On first use, migrates any existing JSON file into the table.
"""
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DB_PATH = DATA_DIR / "hyperdata.db"
LEGACY_JSON = DATA_DIR / "discovered_addresses.json"

_CREATE = """
CREATE TABLE IF NOT EXISTS discovered_addresses (
    address TEXT PRIMARY KEY,
    source TEXT,
    first_seen REAL,
    last_seen REAL
);
"""

_lock = threading.Lock()
_initialized = False


def _get_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init() -> None:
    global _initialized
    if _initialized:
        return
    try:
        conn = _get_conn()
        conn.execute(_CREATE)
        conn.commit()

        # One-time migration from legacy JSON
        if LEGACY_JSON.exists():
            try:
                data = json.loads(LEGACY_JSON.read_text())
                if isinstance(data, list):
                    now = time.time()
                    conn.executemany(
                        "INSERT OR IGNORE INTO discovered_addresses (address, source, first_seen, last_seen) "
                        "VALUES (?, ?, ?, ?)",
                        [(addr, "legacy_json", now, now) for addr in data],
                    )
                    conn.commit()
                    backup = LEGACY_JSON.with_suffix(".json.migrated")
                    LEGACY_JSON.rename(backup)
                    logger.info("[address_store] Migrated %d addresses from JSON", len(data))
            except Exception:
                logger.exception("[address_store] Legacy migration failed")

        conn.close()
        _initialized = True
    except Exception:
        logger.exception("[address_store] Init failed")


def add_address(address: str, source: str = "unknown") -> None:
    """Insert or update a single address. Idempotent."""
    _init()
    now = time.time()
    try:
        with _lock:
            conn = _get_conn()
            conn.execute(
                "INSERT INTO discovered_addresses (address, source, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(address) DO UPDATE SET last_seen = excluded.last_seen",
                (address, source, now, now),
            )
            conn.commit()
            conn.close()
    except Exception:
        logger.debug("[address_store] add failed", exc_info=True)


def add_addresses(addresses: list[str] | set[str], source: str = "unknown") -> int:
    """Batch insert. Returns count written."""
    _init()
    if not addresses:
        return 0
    now = time.time()
    rows = [(a, source, now, now) for a in addresses]
    try:
        with _lock:
            conn = _get_conn()
            conn.executemany(
                "INSERT INTO discovered_addresses (address, source, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(address) DO UPDATE SET last_seen = excluded.last_seen",
                rows,
            )
            conn.commit()
            conn.close()
            return len(rows)
    except Exception:
        logger.debug("[address_store] batch add failed", exc_info=True)
        return 0


def get_all_addresses() -> set[str]:
    """Return all discovered addresses."""
    _init()
    try:
        conn = _get_conn()
        rows = conn.execute("SELECT address FROM discovered_addresses").fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception:
        logger.debug("[address_store] get_all failed", exc_info=True)
        return set()
