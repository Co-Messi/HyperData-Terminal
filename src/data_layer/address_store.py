"""
SQLite-backed store for discovered wallet addresses.

Replaces the previous JSON file (discovered_addresses.json) which had
race conditions between PositionScanner and SmartMoneyEngine writers.
On first use, migrates any existing JSON file into the table.
"""
import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DB_PATH = DATA_DIR / "hyperdata.db"
LEGACY_JSON = DATA_DIR / "discovered_addresses.json"

# EVM wallet address: 0x + 40 hex chars. Anything else from an exchange
# payload is junk and must not be persisted (it would be re-scanned forever).
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Retention cap: keep the most recently seen addresses; every tracked address
# costs a clearinghouseState call per scan cycle.
MAX_TRACKED_ADDRESSES = 50_000


def is_valid_address(address: object) -> bool:
    """True for a well-formed EVM wallet address string."""
    return isinstance(address, str) and bool(_ADDRESS_RE.match(address))


def normalize_address(address: str) -> str:
    """Canonical form: lowercase (EVM addresses are case-insensitive)."""
    return address.lower()

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
    """Insert or update a single address. Idempotent. Invalid input is dropped."""
    add_addresses([address], source=source)


def add_addresses(addresses: list[str] | set[str], source: str = "unknown") -> int:
    """Batch insert (validated + normalized). Returns count written.

    Non-address strings from exchange payloads are dropped and counted here
    so garbage identifiers never enter the store, and the table is capped at
    MAX_TRACKED_ADDRESSES by expiring the least recently seen rows.
    """
    _init()
    if not addresses:
        return 0
    now = time.time()
    valid = [normalize_address(a) for a in addresses if is_valid_address(a)]
    dropped = len(list(addresses)) - len(valid)
    if dropped:
        logger.warning("[address_store] dropped %d invalid address strings", dropped)
    if not valid:
        return 0
    rows = [(a, source, now, now) for a in valid]
    try:
        with _lock:
            conn = _get_conn()
            conn.executemany(
                "INSERT INTO discovered_addresses (address, source, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(address) DO UPDATE SET last_seen = excluded.last_seen",
                rows,
            )
            # Retention cap: expire the least-recently-seen overflow.
            count = conn.execute(
                "SELECT COUNT(*) FROM discovered_addresses"
            ).fetchone()[0]
            if count > MAX_TRACKED_ADDRESSES:
                overflow = count - MAX_TRACKED_ADDRESSES
                conn.execute(
                    "DELETE FROM discovered_addresses WHERE address IN ("
                    "SELECT address FROM discovered_addresses "
                    "ORDER BY last_seen ASC LIMIT ?)",
                    (overflow,),
                )
                logger.info("[address_store] expired %d least-recently-seen addresses", overflow)
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
