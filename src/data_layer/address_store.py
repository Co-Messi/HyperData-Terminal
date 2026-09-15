"""
SQLite-backed store for discovered wallet addresses.

Replaces the previous JSON file (discovered_addresses.json) which had
race conditions between PositionScanner and SmartMoneyEngine writers.
On first use, migrates any existing JSON file into the table.

The table lives in the same ``data/hyperdata.db`` file as ``DataStore`` and is
declared in that module's versioned schema; the CREATE here only exists so
this module also works standalone (tests, one-off scripts). Contention with
DataStore's connection is handled by SQLite's busy timeout.

Failure policy: writes that fail are logged at WARNING and dropped (a
discovery batch is re-discoverable); reads that fail RAISE — an unreadable
store must not be mistaken for an empty one, or the scanner silently
re-discovers from scratch and every tracked address is forgotten.
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

# Retention cap: keep the most recently seen addresses. The position scanner
# re-fetches SCAN_ADDRESS_BUDGET (150) addresses per ~45s cycle (15 batches
# of rate-limited requests plus the 15s scan_interval), so 3,000 addresses
# is a ~17-minute worst-case full pass; position_scanner derives its
# staleness threshold (POSITION_STALE_AFTER_SECONDS) from this cap, so the
# two can never drift apart again. The old 50,000 cap implied an 83-minute
# pass that nothing reported. Enforced by prune(), not on every write; the
# hub re-syncs the scanner's in-memory set right after each prune.
MAX_TRACKED_ADDRESSES = 3_000


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
    """Ensure the table exists (and migrate the legacy JSON once). Raises on
    a DB that cannot be opened — see the module docstring."""
    global _initialized
    if _initialized:
        return
    try:
        conn = _get_conn()
    except sqlite3.Error:
        logger.error("[address_store] cannot open %s", DB_PATH, exc_info=True)
        raise
    try:
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
                logger.warning("[address_store] Legacy JSON migration failed", exc_info=True)
        _initialized = True
    except sqlite3.Error:
        logger.error("[address_store] init failed on %s", DB_PATH, exc_info=True)
        raise
    finally:
        conn.close()


def add_address(address: str, source: str = "unknown") -> None:
    """Insert or update a single address. Idempotent. Invalid input is dropped."""
    add_addresses([address], source=source)


def add_addresses(addresses, source: str = "unknown") -> int:
    """Batch upsert (validated + normalized). Returns count written.

    Non-address strings from exchange payloads are dropped and counted here
    so garbage identifiers never enter the store. Callers should pass only
    the addresses they just discovered — this is an upsert per row, so
    re-sending the whole known set every cycle is wasted work. The retention
    cap is enforced by prune(), not here, so the hot path never runs a
    COUNT(*) over the table.
    """
    _init()
    # Materialize first: a generator would be consumed by the comprehension
    # and the dropped count would go negative.
    addresses = list(addresses)
    if not addresses:
        return 0
    now = time.time()
    valid = [normalize_address(a) for a in addresses if is_valid_address(a)]
    dropped = len(addresses) - len(valid)
    if dropped:
        logger.warning("[address_store] dropped %d invalid address strings", dropped)
    if not valid:
        return 0
    rows = [(a, source, now, now) for a in valid]
    try:
        with _lock:
            conn = _get_conn()
            try:
                conn.executemany(
                    "INSERT INTO discovered_addresses (address, source, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(address) DO UPDATE SET last_seen = excluded.last_seen",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()
        return len(rows)
    except sqlite3.Error:
        logger.warning(
            "[address_store] batch add failed — %d addresses NOT persisted",
            len(rows), exc_info=True,
        )
        return 0


def prune(max_addresses: int | None = None) -> int:
    """Expire the least-recently-seen addresses above the retention cap.

    Returns the number of rows removed. Run periodically (the hub calls it
    hourly alongside DataStore.prune) rather than on every write.
    """
    _init()
    cap = MAX_TRACKED_ADDRESSES if max_addresses is None else max_addresses
    try:
        with _lock:
            conn = _get_conn()
            try:
                count = conn.execute("SELECT COUNT(*) FROM discovered_addresses").fetchone()[0]
                overflow = count - cap
                if overflow <= 0:
                    return 0
                conn.execute(
                    "DELETE FROM discovered_addresses WHERE address IN ("
                    "SELECT address FROM discovered_addresses "
                    "ORDER BY last_seen ASC LIMIT ?)",
                    (overflow,),
                )
                conn.commit()
            finally:
                conn.close()
        logger.info("[address_store] expired %d least-recently-seen addresses", overflow)
        return overflow
    except sqlite3.Error:
        logger.warning("[address_store] prune failed", exc_info=True)
        return 0


def get_all_addresses() -> set[str]:
    """Return all discovered addresses.

    Raises sqlite3.Error if the store cannot be read: an unreadable store is
    not an empty one, and callers must not quietly start over.
    """
    _init()
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT address FROM discovered_addresses").fetchall()
    except sqlite3.Error:
        logger.error("[address_store] read failed on %s", DB_PATH, exc_info=True)
        raise
    finally:
        conn.close()
    return {r[0] for r in rows}
