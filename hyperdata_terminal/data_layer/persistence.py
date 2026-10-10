"""
Persistence layer — stores events to SQLite for historical analysis.
Integrates with HyperDataHub via callbacks.

Usage:
    store = DataStore(DATA_DIR / "hyperdata.db")
    store.attach(hub)  # saves liquidations, HLP absorptions, signals, snapshots

    # Query historical data (confirmed liquidations unless include_estimated):
    store.get_liquidations(since_hours=24)
    store.get_liquidation_stats(hours=24)
    store.get_liquidation_stats(hours=24, include_estimated=True)

Raw trades are stored (1 in 2) only with HYPERDATA_PERSIST_TRADES=1:
nothing in the package reads them back, and they were most of the file.

Rows written by 1.0.0 and earlier carry known errors that no migration can
undo, because the inputs needed to recompute them were never stored: Bybit
liquidations have long and short swapped, OKX sizes from builds before the
contract value fix are up to 100x too large, and funding rows have no
interval (interval_hours NULL) and were all divided by 8. Retention removes
them within RETENTION_DAYS of upgrading.
"""

import atexit
import logging
import os
import shutil
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

from hyperdata_terminal.paths import DATA_DIR

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "hyperdata.db"

# Writer-thread wake-up interval (seconds). Every batch the writer applies is
# committed immediately (see DataStore._apply), so this only bounds how long
# the thread sleeps between checks when the queue is idle; worst-case data
# loss on an uncatchable crash (SIGKILL/OOM) is whatever was still queued.
COMMIT_INTERVAL_SECONDS = 5.0

# Delete rows older than this on prune(); keeps the DB (and the periodic
# COUNT(*) on the status loop) bounded on long-running instances. 0 disables.
RETENTION_DAYS = 7.0

# Upper bound on INSERTs waiting for the writer thread (H6). At full
# Binance+HL trade rates with 1-in-2 sampling this is well over a minute
# of backlog; beyond it new events are dropped and COUNTED (dropped_writes
# in get_db_stats) rather than allowed to grow memory without bound.
WRITE_QUEUE_MAX = 50_000

# How long flush()/close()/reads wait for the writer to catch up.
DRAIN_TIMEOUT_SECONDS = 10.0


class CorruptDatabase(sqlite3.DatabaseError):
    """The file failed SQLite's integrity check or is not a database."""


def _is_corruption(exc: BaseException) -> bool:
    if isinstance(exc, CorruptDatabase):
        return True
    text = str(exc).lower()
    return "file is not a database" in text or "malformed" in text


def persist_trades_enabled() -> bool:
    return os.environ.get("HYPERDATA_PERSIST_TRADES", "").strip().lower() in ("1", "true", "yes", "on")


class DataStore:
    """SQLite event store with a dedicated writer thread.

    Every INSERT path (hub callbacks for trades/liquidations/signals/HLP
    trades, plus the periodic funding/LSR/IV/HLP snapshot saves) ENQUEUES
    a (sql, params) pair and returns immediately; a single daemon thread
    drains the queue in batches, executes under the connection lock and
    commits on the usual 50-event / COMMIT_INTERVAL cadence. Before H6 the
    trade callback ran a blocking INSERT on the asyncio event loop inside
    the WebSocket read path — thousands of synchronous disk writes per
    second on the thread running every feed, the API and the heartbeat.

    Readers (get_*, flush, close, prune) drain the queue first so a read
    immediately after a write still sees it.
    """

    def __init__(self, db_path: str | Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._event_count = 0
        # Dedicated trade counter for sampling — must NOT share the global
        # _event_count (which is bumped by 6 unrelated event types), or the
        # "1-in-N" sample becomes biased and get_trade_summary's xN rescale wrong.
        self._trade_count = 0
        self._sample_lock = threading.Lock()
        self._last_commit_at = 0.0

        # Writer queue state (see class docstring). Sequence numbers let
        # _drain() wait for exactly the writes enqueued before it was called.
        self._write_q: deque[tuple[str, tuple]] = deque()
        self._q_cond = threading.Condition()
        self._enqueued_seq = 0
        self._applied_seq = 0
        self.dropped_writes = 0
        # Writes the writer thread attempted but SQLite rejected (a locked
        # file, a full disk): lost rows, counted like dropped_writes.
        self.failed_writes = 0
        # False when the file could not be opened and this session runs on
        # an in-memory database (nothing it writes survives the process).
        self.persistent = True
        self._writer_stop = False
        self._writer: threading.Thread | None = None

        # Use a local handle so the corruption-recovery path can close a
        # half-opened connection without assuming self._conn was ever assigned.
        conn = None
        try:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._check_integrity(conn)
            self._conn = conn
            self._init_tables()
        except sqlite3.DatabaseError as exc:
            if conn is not None:
                conn.close()
            # Lock/busy contention is NOT corruption: another process (a
            # dashboard, a verification run) holding the DB must not get the
            # healthy database quarantined out from under it.
            if "lock" in str(exc).lower() or "busy" in str(exc).lower():
                logger.error(
                    "Database at %s is locked/busy — failing startup rather "
                    "than quarantining a healthy DB: %s", self.db_path, exc,
                )
                raise
            # Only a failed integrity check or a file that is not a database
            # is corruption. A full disk, a read-only mount or an I/O error
            # says nothing about the file: it stays where it is and this
            # session runs without persistence.
            if not _is_corruption(exc):
                logger.error(
                    "Database at %s could not be opened (%s); it is not quarantined. "
                    "Running on an in-memory store: nothing is persisted this session.",
                    self.db_path, exc,
                )
                self.persistent = False
                self._conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._init_tables()
                self._start_writer()
                return
            # Quarantine, never delete: move the corrupted DB (and WAL/SHM)
            # aside with a timestamp so history survives for postmortem and
            # possible `.recover`, then start fresh.
            quarantine_dir = self.db_path.parent / "corrupted"
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(self.db_path) + suffix)
                if p.exists():
                    dest = quarantine_dir / f"{p.name}.{stamp}"
                    try:
                        shutil.move(str(p), str(dest))  # handles cross-device
                    except OSError:
                        logger.exception("Failed to quarantine %s", p)
                        try:
                            p.unlink()  # last resort so we can still start
                        except OSError:
                            logger.exception("Could not remove %s either", p)
            logger.error(
                "Database corrupted at %s — quarantined to %s and recreated. "
                "Historical data is preserved there for recovery.",
                self.db_path, quarantine_dir,
            )
            try:
                self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10)
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._init_tables()
            except sqlite3.DatabaseError:
                # The corrupted file could not be moved OR removed (held
                # handle, read-only mount). Run on an in-memory DB so the
                # terminal stays alive; persistence is lost for this session.
                logger.critical(
                    "Could not recreate database at %s — falling back to an "
                    "in-memory store (NO persistence this session)", self.db_path,
                )
                self.persistent = False
                self._conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._init_tables()

        self._start_writer()

    def _start_writer(self) -> None:
        self._writer = threading.Thread(
            target=self._writer_loop, name="datastore-writer", daemon=True,
        )
        self._writer.start()

        # Safety net for graceful exits (normal return, unhandled exception,
        # Ctrl-C → KeyboardInterrupt unwinds to interpreter exit). The
        # time-based commit above covers uncatchable kills.
        atexit.register(self._atexit_flush)

    # ── Writer thread ────────────────────────────────────────

    def _enqueue(self, sql: str, params: tuple) -> bool:
        """Queue one INSERT for the writer thread. Never blocks the caller;
        returns False (and counts) when the queue is full."""
        with self._q_cond:
            if len(self._write_q) >= WRITE_QUEUE_MAX:
                self.dropped_writes += 1
                if self.dropped_writes % 1000 == 1:
                    logger.warning(
                        "DataStore write queue full (%d) — %d events dropped so far; "
                        "the writer thread is not keeping up with the feeds",
                        WRITE_QUEUE_MAX, self.dropped_writes,
                    )
                return False
            self._write_q.append((sql, params))
            self._enqueued_seq += 1
            self._q_cond.notify()
        return True

    def _writer_loop(self) -> None:
        while True:
            with self._q_cond:
                while not self._write_q and not self._writer_stop:
                    self._q_cond.wait(timeout=COMMIT_INTERVAL_SECONDS)
                if not self._write_q and self._writer_stop:
                    return
                batch = list(self._write_q)
                self._write_q.clear()
            try:
                self._apply(batch)
            except Exception:
                # Never let the writer die: a dead writer means every later
                # event silently queues until the cap and is then dropped.
                self.failed_writes += len(batch)
                logger.exception("DataStore writer batch failed (%d writes)", len(batch))
            finally:
                with self._q_cond:
                    self._applied_seq += len(batch)
                    self._q_cond.notify_all()

    def _apply(self, batch: list[tuple[str, tuple]]) -> None:
        """Execute one batch and COMMIT it.

        Each batch is its own transaction. Holding the write transaction open
        across batches (committing only every 50 events / 5s) kept SQLite's
        WAL write lock for seconds at a time under load, and address_store —
        a second connection to the same file — starved on the millisecond
        gaps and failed with "database is locked" despite its 10s busy
        timeout. In WAL mode with synchronous=NORMAL a commit is not an
        fsync, so per-batch commits are cheap; a batch under load is dozens
        of rows, idle it is one.
        """
        if not batch:
            return
        with self._lock:
            for sql, params in batch:
                try:
                    self._conn.execute(sql, params)
                    self._event_count += 1
                except sqlite3.Error:
                    # The row is lost: count it (surfaced in /v1/health),
                    # and log the first few and then every 1000th.
                    self.failed_writes += 1
                    if self.failed_writes <= 3 or self.failed_writes % 1000 == 0:
                        logger.exception("DataStore write failed (%d so far): %s", self.failed_writes, sql[:60])
            self._conn.commit()
            self._last_commit_at = time.time()

    def _drain(self, timeout: float | None = None) -> bool:
        """Block until every write enqueued so far has been applied.

        Returns False if the writer did not catch up within `timeout` (or is
        not running). That is an ERROR, not a warning: every caller is a
        read that will now miss rows, or flush()/close() about to let
        those writes go — the only evidence is this line.
        """
        # Read the module constant at call time (not as a default argument
        # bound at definition) so tests and operators can tune it.
        timeout = DRAIN_TIMEOUT_SECONDS if timeout is None else timeout
        writer = self._writer
        if writer is None:
            return True
        if threading.current_thread() is writer:
            return True  # called from inside _apply(): nothing to wait for
        with self._q_cond:
            target = self._enqueued_seq
            self._q_cond.notify_all()
            deadline = time.monotonic() + timeout
            while self._applied_seq < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not writer.is_alive():
                    logger.error(
                        "DataStore drain incomplete after %.0fs: %d writes still pending "
                        "(writer alive=%s)", timeout, target - self._applied_seq, writer.is_alive(),
                    )
                    return False
                self._q_cond.wait(timeout=remaining)
        return True

    def pending_writes(self) -> int:
        """Writes enqueued but not yet applied by the writer thread."""
        with self._q_cond:
            return self._enqueued_seq - self._applied_seq

    @staticmethod
    def _check_integrity(conn: sqlite3.Connection) -> None:
        """Run SQLite's quick_check and RAISE on anything but 'ok'.

        PRAGMA integrity_check / quick_check do not raise — they return rows,
        `('ok',)` or a list of corruption descriptions. The previous code
        executed the pragma and discarded the cursor, so only corruption loud
        enough to fail the open itself ("file is not a database") reached the
        quarantine path; page-level damage passed straight through and the
        app ran on a broken DB. quick_check skips the index-consistency scan
        so startup on a large DB stays fast.
        """
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError as exc:
            if "lock" in str(exc).lower() or "busy" in str(exc).lower():
                raise
            # Some SQLite builds raise instead of returning a verdict.
            raise CorruptDatabase(f"quick_check failed: {exc}") from exc
        verdict = row[0] if row else None
        if verdict != "ok":
            raise CorruptDatabase(f"quick_check failed: {verdict!r}")

    def _atexit_flush(self) -> None:
        """Best-effort flush registered with atexit; never raises."""
        try:
            self.flush()
        except Exception:
            pass

    def _init_tables(self):
        """Create tables, run migrations, then create indexes.

        Indexes come after the migrations, one at a time: an older table
        missing a column an index names (a legacy DB) used to fail the whole
        script with "no such column", which the open path treated as
        corruption and quarantined a healthy database.
        """
        sql = "\n".join(line for line in self._SCHEMA_SQL.splitlines() if not line.strip().startswith("--"))
        statements = [st.strip() for st in sql.split(";") if st.strip()]
        tables = [st for st in statements if not st.upper().startswith("CREATE INDEX")]
        indexes = [st for st in statements if st.upper().startswith("CREATE INDEX")]
        with self._lock:
            self._conn.executescript(";\n".join(tables) + ";")
            self._run_migrations()
            for st in indexes:
                try:
                    self._conn.execute(st)
                except sqlite3.OperationalError as exc:
                    logger.warning("Skipped index on a legacy table (%s): %.80s", exc, st)
            self._conn.commit()

    _SCHEMA_SQL = """
                CREATE TABLE IF NOT EXISTS liquidations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    size_usd REAL NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    confirmed INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_liq_ts ON liquidations(timestamp);
                CREATE INDEX IF NOT EXISTS idx_liq_exchange ON liquidations(exchange);
                CREATE INDEX IF NOT EXISTS idx_liq_symbol ON liquidations(symbol);

                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    size REAL NOT NULL,
                    size_usd REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trade_ts ON trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_trade_symbol ON trades(symbol);

                -- Discovered wallet addresses. Written by data_layer.address_store
                -- (its own connection to this same file); declared here so the
                -- table is part of the versioned schema instead of being created
                -- ad hoc by a second module.
                CREATE TABLE IF NOT EXISTS discovered_addresses (
                    address TEXT PRIMARY KEY,
                    source TEXT,
                    first_seen REAL,
                    last_seen REAL
                );

                -- NOTE: there is deliberately no `wallets` table. SmartMoneyEngine
                -- recomputes every WalletProfile from fills each session and
                -- nothing ever wrote one here (S2); v4 drops the dead table.

                CREATE TABLE IF NOT EXISTS smart_money_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    address TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    action TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    size_usd REAL NOT NULL,
                    wallet_rank INTEGER NOT NULL,
                    signal_type TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    wallet_confidence REAL NOT NULL DEFAULT 0.0
                );
                CREATE INDEX IF NOT EXISTS idx_sm_signal_ts ON smart_money_signals(timestamp);
                CREATE INDEX IF NOT EXISTS idx_sm_signal_type ON smart_money_signals(signal_type);

                CREATE TABLE IF NOT EXISTS hlp_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    account_value REAL NOT NULL,
                    net_delta REAL NOT NULL,
                    delta_zscore REAL NOT NULL,
                    total_exposure REAL NOT NULL,
                    num_positions INTEGER NOT NULL,
                    session_pnl REAL NOT NULL,
                    total_unrealized_pnl REAL NOT NULL,
                    created_at REAL NOT NULL,
                    aum_source TEXT NOT NULL DEFAULT 'clearinghouseState',
                    pnl_known INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_hlp_snap_ts ON hlp_snapshots(timestamp);

                CREATE TABLE IF NOT EXISTS hlp_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    size REAL NOT NULL,
                    size_usd REAL NOT NULL,
                    direction TEXT NOT NULL,
                    closed_pnl REAL NOT NULL,
                    is_liquidation INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    fill_hash TEXT,
                    vault TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_hlp_trade_ts ON hlp_trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_hlp_trade_liq ON hlp_trades(is_liquidation);

                CREATE TABLE IF NOT EXISTS funding_rates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    funding_rate_hourly REAL NOT NULL,
                    funding_rate_annualized REAL NOT NULL,
                    created_at REAL NOT NULL,
                    interval_hours REAL
                );
                CREATE INDEX IF NOT EXISTS idx_fr_ts ON funding_rates(timestamp);
                CREATE INDEX IF NOT EXISTS idx_fr_exchange_symbol ON funding_rates(exchange, symbol);

                CREATE TABLE IF NOT EXISTS long_short_ratios (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    symbol TEXT NOT NULL,
                    long_ratio REAL NOT NULL,
                    short_ratio REAL NOT NULL,
                    long_short_ratio REAL NOT NULL,
                    created_at REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT 'binance'
                );
                CREATE INDEX IF NOT EXISTS idx_lsr_ts ON long_short_ratios(timestamp);
                CREATE INDEX IF NOT EXISTS idx_lsr_symbol ON long_short_ratios(symbol);

                CREATE TABLE IF NOT EXISTS options_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    underlying TEXT NOT NULL,
                    mark_iv REAL NOT NULL,
                    bid_iv REAL NOT NULL,
                    ask_iv REAL NOT NULL,
                    oi_usd REAL NOT NULL,
                    index_price REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_options_ts ON options_data(timestamp);
                CREATE INDEX IF NOT EXISTS idx_options_underlying ON options_data(underlying);
    """

    # Bump when adding a migration to _MIGRATIONS below. The schema_version
    # table lets a release tell an old DB from a new one, and _run_migrations
    # only runs the steps ABOVE the DB's recorded version — so a migration
    # does not need to be idempotent-by-accident to be safe on restart.
    #
    # History:
    #   v1  original schema (implicit for pre-versioning DBs)
    #   v2  added columns to the `snapshots` / `paper_trades` tables — both
    #       tables were dead (no writer anywhere) and were removed in v3, so
    #       v2 is now a no-op.
    #   v3  drop the dead `snapshots` / `paper_trades` tables (only if empty);
    #       add smart_money_signals.wallet_confidence so a persisted
    #       smart/dumb label never travels without its sample-size confidence
    #   v4  drop the dead `wallets` table (only if empty): save_wallet /
    #       load_wallets had no production caller — SmartMoneyEngine keeps
    #       profiles in memory and recomputes them from fills — so the
    #       column v3 added to it (`confidence`) could only ever be 0.0.
    #   v5  long_short_ratios.source: which venue's accounts a row counts
    #       (Binance, or the Bybit/OKX fallback where Binance is blocked), so
    #       stored history never silently mixes crowds. Existing rows were all
    #       Binance, hence the default.
    #   v6  hlp_snapshots.aum_source; hlp_trades.fill_hash (unique) and
    #       .vault, so an absorbed liquidation is one row however many vaults
    #       filled it or how often the tracker restarts. (A separate version
    #       because DBs created by an early build of this branch already
    #       recorded v5 without these columns.)
    #   v7  hlp_snapshots.pnl_known: session_pnl is 0 both when PnL is truly
    #       flat and when it is unknown (no fresh vaultDetails reading); the
    #       flag tells them apart.
    #   v8  funding_rates.interval_hours: the symbol's funding interval the
    #       hourly and annualized figures were divided by. NULL on rows from
    #       earlier builds, which divided every Binance and Bybit rate by 8
    #       (wrong for every 4h or 1h symbol).
    #   v9  hlp_trades.fill_hash holds the absorption key (hash|coin|side):
    #       one transaction can carry several coins. Stored plain hashes are
    #       rewritten to that form so a re-read upserts instead of
    #       duplicating; rows that already merged two coins cannot be split.
    SCHEMA_VERSION = 9

    # Tables that no code path has ever written to, by the version that
    # drops them. A dead table is dropped only when EMPTY; a populated one
    # is left in place and reported rather than destroyed.
    _DEAD_TABLES = {3: ("snapshots", "paper_trades"), 4: ("wallets",)}

    def _add_column(self, table: str, col: str, col_type: str) -> None:
        """ALTER TABLE ADD COLUMN that tolerates the column already existing
        (a fresh DB creates it in _init_tables; an upgraded DB adds it here).
        Table/column names are hardcoded literals — no injection."""
        try:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                logger.error("Migration failed for %s.%s: %s", table, col, exc)
                raise

    def _drop_dead_tables(self, version: int) -> None:
        """Drop the tables `version` retires — only the empty ones. Table
        names are hardcoded literals — no injection."""
        for table in self._DEAD_TABLES[version]:
            exists = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            rows = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if rows:
                logger.warning(
                    "Legacy table %s has %d rows; leaving it in place (nothing "
                    "reads or writes it any more — drop it manually if unwanted)",
                    table, rows,
                )
                continue
            self._conn.execute(f"DROP TABLE {table}")
            logger.info("Dropped empty legacy table %s", table)

    def _migrate_v3(self) -> None:
        self._add_column("smart_money_signals", "wallet_confidence", "REAL NOT NULL DEFAULT 0.0")
        self._drop_dead_tables(3)

    def _migrate_v4(self) -> None:
        self._drop_dead_tables(4)

    def _migrate_v5(self) -> None:
        self._add_column("long_short_ratios", "source", "TEXT NOT NULL DEFAULT 'binance'")

    def _migrate_v6(self) -> None:
        self._add_column("hlp_snapshots", "aum_source", "TEXT NOT NULL DEFAULT 'clearinghouseState'")
        self._add_column("hlp_trades", "fill_hash", "TEXT")
        self._add_column("hlp_trades", "vault", "TEXT NOT NULL DEFAULT ''")
        # One row per absorbed liquidation: a restart re-reading the same
        # fills upserts instead of duplicating. Old rows (NULL hash) are distinct.
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_hlp_trade_hash ON hlp_trades(fill_hash)")

    # version -> migration step. Steps run in order for every version above
    # the DB's recorded one, each followed by a schema_version row.
    def _migrate_v7(self) -> None:
        self._add_column("hlp_snapshots", "pnl_known", "INTEGER NOT NULL DEFAULT 0")

    def _migrate_v8(self) -> None:
        self._add_column("funding_rates", "interval_hours", "REAL")

    def _migrate_v9(self) -> None:
        self._conn.execute(
            "UPDATE hlp_trades SET fill_hash = fill_hash || '|' || symbol || '|' || side "
            "WHERE fill_hash LIKE '0x%' AND instr(fill_hash, ':') = 0 AND instr(fill_hash, '|') = 0"
        )

    _MIGRATIONS = {
        3: _migrate_v3, 4: _migrate_v4, 5: _migrate_v5, 6: _migrate_v6, 7: _migrate_v7, 8: _migrate_v8,
        9: _migrate_v9,
    }

    def _run_migrations(self) -> None:
        """Versioned migrations. Caller holds the lock.

        Runs only the steps above the DB's recorded version. Any migration
        failure is raised so the app doesn't keep running against a
        half-migrated schema.
        """
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER NOT NULL, applied_at REAL NOT NULL)"
        )
        row = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] or 0

        for version in range(current + 1, self.SCHEMA_VERSION + 1):
            step = self._MIGRATIONS.get(version)
            if step is not None:
                step(self)
            self._conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, time.time()),
            )

    def get_schema_version(self) -> int:
        """Highest applied schema version (0 for a brand-new/legacy DB)."""
        with self._lock:
            row = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return row[0] or 0

    def attach(self, hub) -> None:
        """Attach to a HyperDataHub — automatically persists all events."""
        hub.on_liquidation(self._save_liquidation)
        # Raw trades only on request (HYPERDATA_PERSIST_TRADES=1): nothing in
        # the package reads them, and at full Binance plus Hyperliquid rates
        # they were most of the file (4M rows in a week).
        if persist_trades_enabled():
            hub.on_trade(self._save_trade)
        # Attach to smart money engine if available
        if hasattr(hub, "smart_money") and hub.smart_money is not None:
            hub.smart_money.on_signal(self._save_smart_money_signal)
        # Attach to HLP tracker
        if hasattr(hub, "hlp") and hub.hlp is not None:
            hub.hlp.on_hlp_absorption(self._save_hlp_absorption)
            self._hlp_snapshot_count = 0
            self._hlp_hub = hub

    def _save_liquidation(self, event) -> None:
        """Callback: queue a liquidation event for the writer thread."""
        self._enqueue(
            "INSERT INTO liquidations (timestamp, exchange, symbol, side, size_usd, "
            "price, quantity, confirmed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event.timestamp, event.exchange, event.symbol, event.side,
             event.size_usd, event.price, event.quantity,
             1 if getattr(event, 'confirmed', True) else 0,
             time.time()),
        )

    TRADE_SAMPLE_RATE = 2  # keep 1 in N trades

    def _save_trade(self, trade) -> None:
        """Callback: queue 1 in TRADE_SAMPLE_RATE trades for the writer thread.

        Sampling is keyed on a dedicated trade counter (not the shared
        _event_count), so every Nth *trade* is kept regardless of other event
        streams — making get_trade_summary's xN rescale unbiased. This runs
        on the WebSocket read path: no disk I/O happens here.
        """
        with self._sample_lock:
            self._trade_count += 1
            keep = self._trade_count % self.TRADE_SAMPLE_RATE == 0
        if not keep:
            return
        self._enqueue(
            "INSERT INTO trades (timestamp, symbol, side, price, size, size_usd, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trade.timestamp, trade.symbol, trade.side, trade.price,
             trade.size, trade.size_usd, time.time()),
        )

    # Time-series tables that grow unbounded and are safe to age out.
    _PRUNABLE_TABLES = (
        "liquidations", "trades", "smart_money_signals",
        "hlp_snapshots", "hlp_trades", "funding_rates",
        "long_short_ratios", "options_data",
    )

    def prune(self, retention_days: float = RETENTION_DAYS) -> None:
        """Delete rows older than retention_days and checkpoint the WAL.

        Without this the DB and its -wal file grow forever and the periodic
        COUNT(*) on the hub status loop becomes an ever-slower full scan under
        the write lock. retention_days <= 0 keeps everything (WAL is still
        checkpointed). Table names are hardcoded literals — no injection.
        Blocking (full-table DELETEs): the hub calls it via asyncio.to_thread.
        """
        self._drain()
        with self._lock:
            if retention_days and retention_days > 0:
                cutoff = time.time() - retention_days * 86400
                for table in self._PRUNABLE_TABLES:
                    try:
                        self._conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
                    except sqlite3.Error:
                        logger.exception("prune failed for %s", table)
                self._conn.commit()
                self._last_commit_at = time.time()
            # Truncate the WAL so it can't grow without bound.
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                logger.exception("wal_checkpoint failed")

    def flush(self) -> bool:
        """Apply every queued write and commit.

        Returns False — after an ERROR log — if the writer did not drain
        within DRAIN_TIMEOUT_SECONDS; the commit still happens for whatever
        WAS applied. Pre-fix the drain result was discarded (S4).
        """
        drained = self._drain()
        with self._lock:
            self._conn.commit()
            self._last_commit_at = time.time()
        return drained

    def close(self) -> bool:
        """Flush, stop the writer thread, close the connection.

        Returns False if any write was lost: the drain timed out, or the
        writer did not finish its backlog before the join timeout. The loss
        is logged at ERROR with the count, because closing the connection
        underneath a wedged writer discards everything still queued and
        nothing else will ever say so.
        """
        drained = self.flush()
        writer = self._writer
        if writer is not None and writer.is_alive():
            with self._q_cond:
                self._writer_stop = True
                self._q_cond.notify_all()
            writer.join(timeout=DRAIN_TIMEOUT_SECONDS)
        lost = self.pending_writes()
        if writer is not None and writer.is_alive():
            logger.error(
                "DataStore writer thread did not stop within %.0fs; closing the connection "
                "underneath it — %d queued writes lost", DRAIN_TIMEOUT_SECONDS, lost,
            )
        elif lost:
            logger.error("DataStore closed with %d queued writes NOT persisted", lost)
        self._writer = None
        with self._lock:
            self._conn.close()
        return drained and lost == 0

    # ── Query methods ────────────────────────────────────

    def get_liquidations(self, since_hours: float = 24, exchange: str | None = None,
                         symbol: str | None = None, limit: int = 1000) -> list[dict]:
        """Get historical liquidation events."""
        cutoff = time.time() - (since_hours * 3600)
        query = ("SELECT timestamp, exchange, symbol, side, size_usd, price, quantity, "
                 "confirmed FROM liquidations WHERE timestamp > ?")
        params: list = [cutoff]
        if exchange:
            query += " AND exchange = ?"
            params.append(exchange)
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        self._drain()
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()

        return [
            {"timestamp": r[0], "exchange": r[1], "symbol": r[2], "side": r[3],
             "size_usd": r[4], "price": r[5], "quantity": r[6], "confirmed": bool(r[7])}
            for r in rows
        ]

    def get_liquidation_stats(self, hours: float = 24, include_estimated: bool = False) -> dict:
        """Aggregated CONFIRMED liquidation stats for a time window
        (include_estimated=True adds Hyperliquid large prints)."""
        cutoff = time.time() - (hours * 3600)
        only_confirmed = "" if include_estimated else " AND confirmed = 1"
        self._drain()
        with self._lock:
            row = self._conn.execute(f"""
                SELECT COUNT(*), COALESCE(SUM(size_usd), 0),
                       SUM(CASE WHEN side='long' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN side='short' THEN 1 ELSE 0 END),
                       COALESCE(SUM(CASE WHEN side='long' THEN size_usd ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN side='short' THEN size_usd ELSE 0 END), 0)
                FROM liquidations WHERE timestamp > ?{only_confirmed}
            """, (cutoff,)).fetchone()

        return {
            "total_count": row[0], "total_volume": row[1],
            "long_count": row[2], "short_count": row[3],
            "long_volume": row[4], "short_volume": row[5],
        }

    def get_liquidations_by_exchange(self, hours: float = 24, include_estimated: bool = False) -> dict[str, dict]:
        """CONFIRMED liquidation counts/volume per exchange."""
        cutoff = time.time() - (hours * 3600)
        only_confirmed = "" if include_estimated else " AND confirmed = 1"
        self._drain()
        with self._lock:
            rows = self._conn.execute(f"""
                SELECT exchange, COUNT(*), COALESCE(SUM(size_usd), 0)
                FROM liquidations WHERE timestamp > ?{only_confirmed}
                GROUP BY exchange ORDER BY SUM(size_usd) DESC
            """, (cutoff,)).fetchall()
        return {r[0]: {"count": r[1], "volume": r[2]} for r in rows}

    def get_trade_summary(self, symbol: str = "BTC", hours: float = 1) -> dict:
        """Get trade volume summary for a symbol."""
        cutoff = time.time() - (hours * 3600)
        self._drain()
        with self._lock:
            row = self._conn.execute("""
                SELECT COUNT(*),
                       COALESCE(SUM(CASE WHEN side='buy' THEN size_usd ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN side='sell' THEN size_usd ELSE 0 END), 0)
                FROM trades WHERE symbol = ? AND timestamp > ?
            """, (symbol, cutoff)).fetchone()
        s = self.TRADE_SAMPLE_RATE
        return {"count": row[0] * s, "buy_volume": row[1] * s, "sell_volume": row[2] * s}

    def get_db_stats(self) -> dict:
        """Get database statistics. Blocking (two COUNT(*) scans): the hub
        calls it via asyncio.to_thread."""
        self._drain()
        with self._lock:
            liq_count = self._conn.execute("SELECT COUNT(*) FROM liquidations").fetchone()[0]
            trade_count = self._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            # DB file size
            size_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
        with self._q_cond:
            pending = len(self._write_q)
        return {
            "liquidations_stored": liq_count,
            "trades_stored": trade_count,
            "db_size_mb": round(size_bytes / (1024 * 1024), 2),
            "db_path": str(self.db_path),
            "write_queue_pending": pending,
            "dropped_writes": self.dropped_writes,
            "failed_writes": self.failed_writes,
            "persistent": self.persistent,
        }

    # ── Smart Money Persistence ───────────────────────────────

    def _save_smart_money_signal(self, signal) -> None:
        """Callback: queue a smart money signal for the writer thread."""
        self._enqueue(
            "INSERT INTO smart_money_signals (timestamp, address, tier, action, symbol, "
            "size_usd, wallet_rank, signal_type, created_at, wallet_confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (signal.timestamp, signal.address, signal.tier, signal.action,
             signal.symbol, signal.size_usd, signal.wallet_rank,
             signal.signal_type, time.time(),
             getattr(signal, "wallet_confidence", 0.0)),
        )

    def save_signal(self, signal) -> None:
        """Explicitly save a smart money signal (non-callback path): queued,
        then flushed so it is durable when this returns."""
        self._save_smart_money_signal(signal)
        self.flush()

    def get_signals(self, hours: float = 24) -> list[dict]:
        """Get smart money signals from the last N hours."""
        cutoff = time.time() - (hours * 3600)
        self._drain()
        with self._lock:
            rows = self._conn.execute(
                """SELECT timestamp, address, tier, action, symbol, size_usd,
                          wallet_rank, signal_type, wallet_confidence
                   FROM smart_money_signals
                   WHERE timestamp > ?
                   ORDER BY timestamp DESC LIMIT 500""",
                (cutoff,),
            ).fetchall()
        return [
            {"timestamp": r[0], "address": r[1], "tier": r[2], "action": r[3],
             "symbol": r[4], "size_usd": r[5], "wallet_rank": r[6], "signal_type": r[7],
             "wallet_confidence": r[8]}
            for r in rows
        ]

    # ── HLP Persistence ──────────────────────────────────────

    def _save_hlp_absorption(self, absorption, first_seen: bool = True) -> None:
        """Callback: upsert one liquidation HLP absorbed, keyed by transaction hash.

        Only absorptions are stored: HLP's strategy vaults make ~300 ordinary
        market making fills a minute (~400k rows a day), which would swamp the
        table without saying anything the snapshots don't. The upsert keeps
        one row per liquidation when several vaults fill it, when a later poll
        adds a vault's share (size grows), and across restarts. It only ever
        grows a row: after a restart a busy vault's first page can hold just
        one vault's share, which must not overwrite the full size stored before.
        """
        if not getattr(absorption, "is_liquidation", False):
            return
        # Keyed by transaction hash, coin and side (HLPTrade.absorption_key).
        fill_hash = getattr(absorption, "absorption_key", None) or getattr(absorption, "fill_hash", "") or None
        self._enqueue(
            """INSERT INTO hlp_trades
               (timestamp, symbol, side, price, size, size_usd, direction,
                closed_pnl, is_liquidation, created_at, fill_hash, vault)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(fill_hash) DO UPDATE SET
                 price = CASE WHEN excluded.size > hlp_trades.size
                              THEN excluded.price ELSE hlp_trades.price END,
                 vault = CASE WHEN excluded.size > hlp_trades.size
                              THEN excluded.vault ELSE hlp_trades.vault END,
                 size = MAX(hlp_trades.size, excluded.size),
                 size_usd = MAX(hlp_trades.size_usd, excluded.size_usd)""",
            (absorption.timestamp, absorption.symbol, absorption.side, absorption.price,
             absorption.size, absorption.size_usd, absorption.direction,
             absorption.closed_pnl, 1, time.time(), fill_hash, getattr(absorption, "vault", "")),
        )

    # Old name, kept for callers and tests.
    _save_hlp_trade = _save_hlp_absorption

    def save_hlp_snapshot(self, snapshot) -> None:
        """Queue an HLP snapshot (called periodically from the status loop)."""
        self._enqueue(
            """INSERT INTO hlp_snapshots
               (timestamp, account_value, net_delta, delta_zscore,
                total_exposure, num_positions, session_pnl,
                total_unrealized_pnl, created_at, aum_source, pnl_known)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (snapshot.timestamp, snapshot.account_value,
             snapshot.net_delta_usd, snapshot.delta_zscore,
             snapshot.total_exposure_usd, snapshot.num_positions,
             snapshot.session_pnl, snapshot.total_unrealized_pnl,
             time.time(), getattr(snapshot, "aum_source", "clearinghouseState"),
             1 if getattr(snapshot, "pnl_known", False) else 0),
        )

    def maybe_save_hlp_snapshot(self) -> None:
        """Save every 5th HLP snapshot to avoid DB bloat. Called from status loop."""
        hub = getattr(self, "_hlp_hub", None)
        if hub is None:
            return
        snap = hub.hlp.get_latest_snapshot()
        if snap is None:
            return
        count = getattr(self, "_hlp_snapshot_count", 0)
        count += 1
        self._hlp_snapshot_count = count
        if count % 5 == 0:
            self.save_hlp_snapshot(snap)

    def get_hlp_snapshots(self, hours: float = 24, limit: int = 500) -> list[dict]:
        """Get historical HLP snapshots."""
        cutoff = time.time() - (hours * 3600)
        self._drain()
        with self._lock:
            rows = self._conn.execute(
                """SELECT timestamp, account_value, net_delta, delta_zscore,
                          total_exposure, num_positions, session_pnl,
                          total_unrealized_pnl
                   FROM hlp_snapshots WHERE timestamp > ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (cutoff, limit),
            ).fetchall()
        return [
            {"timestamp": r[0], "account_value": r[1], "net_delta": r[2],
             "delta_zscore": r[3], "total_exposure": r[4], "num_positions": r[5],
             "session_pnl": r[6], "total_unrealized_pnl": r[7]}
            for r in rows
        ]

    def get_hlp_trades(self, hours: float = 24, liquidations_only: bool = False,
                       limit: int = 500) -> list[dict]:
        """Get historical HLP trades."""
        cutoff = time.time() - (hours * 3600)
        query = """SELECT timestamp, symbol, side, price, size, size_usd,
                          direction, closed_pnl, is_liquidation
                   FROM hlp_trades WHERE timestamp > ?"""
        params: list = [cutoff]
        if liquidations_only:
            query += " AND is_liquidation = 1"
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        self._drain()
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {"timestamp": r[0], "symbol": r[1], "side": r[2], "price": r[3],
             "size": r[4], "size_usd": r[5], "direction": r[6],
             "closed_pnl": r[7], "is_liquidation": bool(r[8])}
            for r in rows
        ]

    # NOTE: paper trades are persisted by src/strategies/paper_trader.py into
    # its own data/paper_trades.db — they are deliberately NOT in this store.

    def save_funding_rate(self, snap) -> None:
        """Queue a funding rate snapshot."""
        self._enqueue(
            "INSERT INTO funding_rates (timestamp, exchange, symbol, funding_rate_hourly, "
            "funding_rate_annualized, created_at, interval_hours) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (snap.timestamp, snap.exchange, snap.symbol,
             snap.funding_rate_hourly, snap.funding_rate_annualized, time.time(),
             getattr(snap, "interval_hours", None)),
        )

    def get_funding_rates(self, exchange: str | None = None, symbol: str | None = None,
                          hours: float = 24, limit: int = 500) -> list[dict]:
        """Get historical funding rate snapshots."""
        cutoff = time.time() - (hours * 3600)
        query = ("SELECT timestamp, exchange, symbol, funding_rate_hourly, "
                 "funding_rate_annualized, interval_hours FROM funding_rates WHERE timestamp > ?")
        params: list = [cutoff]
        if exchange:
            query += " AND exchange = ?"
            params.append(exchange)
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol.upper())
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        self._drain()
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {"timestamp": r[0], "exchange": r[1], "symbol": r[2],
             "funding_rate_hourly": r[3], "funding_rate_annualized": r[4], "interval_hours": r[5]}
            for r in rows
        ]

    def save_long_short_ratio(self, snap) -> None:
        self._enqueue(
            "INSERT INTO long_short_ratios (timestamp, symbol, long_ratio, short_ratio, "
            "long_short_ratio, created_at, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (snap.timestamp, snap.symbol, snap.long_ratio, snap.short_ratio, snap.long_short_ratio, time.time(),
             getattr(snap, "source", "binance")),
        )

    def get_long_short_ratios(self, symbol: str | None = None, hours: float = 24, limit: int = 200) -> list[dict]:
        cutoff = time.time() - (hours * 3600)
        query = ("SELECT timestamp, symbol, long_ratio, short_ratio, long_short_ratio, source "
                 "FROM long_short_ratios WHERE timestamp > ?")
        params: list = [cutoff]
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol.upper())
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        self._drain()
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {"timestamp": r[0], "symbol": r[1], "long_ratio": r[2],
             "short_ratio": r[3], "long_short_ratio": r[4], "source": r[5]}
            for r in rows
        ]

    def save_options_snapshot(self, snap) -> None:
        """Queue a Deribit IV snapshot."""
        self._enqueue(
            "INSERT INTO options_data (timestamp, underlying, mark_iv, bid_iv, ask_iv, "
            "oi_usd, index_price, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (snap.timestamp, snap.underlying, snap.mark_iv, snap.bid_iv, snap.ask_iv,
             snap.oi_usd, snap.index_price, time.time()),
        )

    def get_options_data(self, underlying: str | None = None, hours: float = 24, limit: int = 200) -> list[dict]:
        """Get historical Deribit IV snapshots."""
        cutoff = time.time() - (hours * 3600)
        query = ("SELECT timestamp, underlying, mark_iv, bid_iv, ask_iv, oi_usd, "
                 "index_price FROM options_data WHERE timestamp > ?")
        params: list = [cutoff]
        if underlying:
            query += " AND underlying = ?"
            params.append(underlying.upper())
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        self._drain()
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {"timestamp": r[0], "underlying": r[1], "mark_iv": r[2],
             "bid_iv": r[3], "ask_iv": r[4], "oi_usd": r[5], "index_price": r[6]}
            for r in rows
        ]
