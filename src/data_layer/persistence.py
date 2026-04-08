"""
Persistence layer — stores events to SQLite for historical analysis.
Integrates with HyperDataHub via callbacks.

Usage:
    store = DataStore("data/hyperdata.db")
    store.attach(hub)  # Automatically saves all events

    # Query historical data:
    store.get_liquidations(since_hours=24)
    store.get_trade_summary(symbol="BTC", hours=1)
    store.get_liquidation_stats(hours=24)
"""

import sqlite3
import time
import threading
import logging
from pathlib import Path
from dataclasses import asdict

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "hyperdata.db"


class DataStore:
    def __init__(self, db_path: str | Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._event_count = 0

        try:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA integrity_check")
            self._init_tables()
        except sqlite3.DatabaseError:
            logger.warning("Database corrupted at %s — recreating", self.db_path)
            self._conn.close()
            # Remove corrupted db and WAL/SHM files
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(self.db_path) + suffix)
                if p.exists():
                    p.unlink()
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._init_tables()

    def _init_tables(self):
        """Create tables if they don't exist."""
        with self._lock:
            self._conn.executescript("""
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

                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    cvd REAL,
                    buy_volume REAL,
                    sell_volume REAL,
                    ofi REAL,
                    signal TEXT,
                    trades_per_sec REAL
                );
                CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(timestamp);

                CREATE TABLE IF NOT EXISTS wallets (
                    address TEXT PRIMARY KEY,
                    discovered_at REAL,
                    last_analyzed REAL,
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    losing_trades INTEGER DEFAULT 0,
                    total_realized_pnl REAL DEFAULT 0.0,
                    total_volume_usd REAL DEFAULT 0.0,
                    win_rate REAL DEFAULT 0.0,
                    pnl_score REAL DEFAULT 0.0,
                    sharpe_ratio REAL DEFAULT 0.0,
                    composite_score REAL DEFAULT 0.0,
                    rank INTEGER DEFAULT 0,
                    tier TEXT DEFAULT 'unknown',
                    account_value REAL DEFAULT 0.0
                );
                CREATE INDEX IF NOT EXISTS idx_wallet_tier ON wallets(tier);
                CREATE INDEX IF NOT EXISTS idx_wallet_rank ON wallets(rank);

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
                    created_at REAL NOT NULL
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
                    created_at REAL NOT NULL
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
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_hlp_trade_ts ON hlp_trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_hlp_trade_liq ON hlp_trades(is_liquidation);

                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    size_usd REAL NOT NULL,
                    pnl REAL NOT NULL,
                    pnl_pct REAL NOT NULL,
                    exit_reason TEXT NOT NULL,
                    validation_tier TEXT NOT NULL,
                    strategy_source TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_ts ON paper_trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_paper_tier ON paper_trades(validation_tier);

                CREATE TABLE IF NOT EXISTS funding_rates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    funding_rate_hourly REAL NOT NULL,
                    funding_rate_annualized REAL NOT NULL,
                    created_at REAL NOT NULL
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
                    created_at REAL NOT NULL
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
            """)
            # Migration: add new columns to existing tables if not present
            for table, col, col_type in [
                ("snapshots", "premium_pct", "REAL DEFAULT 0.0"),
                ("snapshots", "basis_pct", "REAL DEFAULT 0.0"),
                ("paper_trades", "funding_collected", "REAL DEFAULT 0.0"),
            ]:
                try:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                except Exception:
                    pass  # Column already exists
            self._conn.commit()

    def attach(self, hub) -> None:
        """Attach to a HyperDataHub — automatically persists all events."""
        hub.on_liquidation(self._save_liquidation)
        hub.on_trade(self._save_trade)
        # Attach to smart money engine if available
        if hasattr(hub, "smart_money") and hub.smart_money is not None:
            hub.smart_money.on_signal(self._save_smart_money_signal)
        # Attach to HLP tracker
        if hasattr(hub, "hlp") and hub.hlp is not None:
            hub.hlp.on_hlp_trade(self._save_hlp_trade)
            self._hlp_snapshot_count = 0
            self._hlp_hub = hub

    def _save_liquidation(self, event) -> None:
        """Callback: save a liquidation event."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO liquidations (timestamp, exchange, symbol, side, size_usd, price, quantity, confirmed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event.timestamp, event.exchange, event.symbol, event.side,
                 event.size_usd, event.price, event.quantity,
                 1 if getattr(event, 'confirmed', True) else 0,
                 time.time())
            )
            self._event_count += 1
            # Batch commit every 50 events for performance
            if self._event_count % 50 == 0:
                self._conn.commit()

    TRADE_SAMPLE_RATE = 2  # keep 1 in N trades

    def _save_trade(self, trade) -> None:
        """Callback: save a trade. Saves 1 in TRADE_SAMPLE_RATE."""
        self._event_count += 1
        if self._event_count % self.TRADE_SAMPLE_RATE != 0:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO trades (timestamp, symbol, side, price, size, size_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (trade.timestamp, trade.symbol, trade.side, trade.price,
                 trade.size, trade.size_usd, time.time())
            )
            if self._event_count % 50 == 0:
                self._conn.commit()

    def flush(self) -> None:
        """Force commit any pending writes."""
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        self.flush()
        self._conn.close()

    # ── Query methods ────────────────────────────────────

    def get_liquidations(self, since_hours: float = 24, exchange: str | None = None,
                         symbol: str | None = None, limit: int = 1000) -> list[dict]:
        """Get historical liquidation events."""
        cutoff = time.time() - (since_hours * 3600)
        query = "SELECT timestamp, exchange, symbol, side, size_usd, price, quantity, confirmed FROM liquidations WHERE timestamp > ?"
        params: list = [cutoff]
        if exchange:
            query += " AND exchange = ?"
            params.append(exchange)
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            rows = self._conn.execute(query, params).fetchall()

        return [
            {"timestamp": r[0], "exchange": r[1], "symbol": r[2], "side": r[3],
             "size_usd": r[4], "price": r[5], "quantity": r[6], "confirmed": bool(r[7])}
            for r in rows
        ]

    def get_liquidation_stats(self, hours: float = 24) -> dict:
        """Get aggregated liquidation stats for a time window."""
        cutoff = time.time() - (hours * 3600)
        with self._lock:
            row = self._conn.execute("""
                SELECT COUNT(*), COALESCE(SUM(size_usd), 0),
                       SUM(CASE WHEN side='long' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN side='short' THEN 1 ELSE 0 END),
                       COALESCE(SUM(CASE WHEN side='long' THEN size_usd ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN side='short' THEN size_usd ELSE 0 END), 0)
                FROM liquidations WHERE timestamp > ?
            """, (cutoff,)).fetchone()

        return {
            "total_count": row[0], "total_volume": row[1],
            "long_count": row[2], "short_count": row[3],
            "long_volume": row[4], "short_volume": row[5],
        }

    def get_liquidations_by_exchange(self, hours: float = 24) -> dict[str, dict]:
        """Get liquidation counts/volume per exchange."""
        cutoff = time.time() - (hours * 3600)
        with self._lock:
            rows = self._conn.execute("""
                SELECT exchange, COUNT(*), COALESCE(SUM(size_usd), 0)
                FROM liquidations WHERE timestamp > ?
                GROUP BY exchange ORDER BY SUM(size_usd) DESC
            """, (cutoff,)).fetchall()
        return {r[0]: {"count": r[1], "volume": r[2]} for r in rows}

    def get_trade_summary(self, symbol: str = "BTC", hours: float = 1) -> dict:
        """Get trade volume summary for a symbol."""
        cutoff = time.time() - (hours * 3600)
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
        """Get database statistics."""
        with self._lock:
            liq_count = self._conn.execute("SELECT COUNT(*) FROM liquidations").fetchone()[0]
            trade_count = self._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            # DB file size
            size_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "liquidations_stored": liq_count,
            "trades_stored": trade_count,
            "db_size_mb": round(size_bytes / (1024 * 1024), 2),
            "db_path": str(self.db_path),
        }

    # ── Smart Money Persistence ───────────────────────────────

    def _save_smart_money_signal(self, signal) -> None:
        """Callback: save a smart money signal."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO smart_money_signals (timestamp, address, tier, action, symbol, size_usd, wallet_rank, signal_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (signal.timestamp, signal.address, signal.tier, signal.action,
                 signal.symbol, signal.size_usd, signal.wallet_rank,
                 signal.signal_type, time.time()),
            )
            self._event_count += 1
            if self._event_count % 50 == 0:
                self._conn.commit()

    def save_wallet(self, profile) -> None:
        """Save or update a wallet profile."""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO wallets
                   (address, discovered_at, last_analyzed, total_trades, winning_trades,
                    losing_trades, total_realized_pnl, total_volume_usd, win_rate,
                    pnl_score, sharpe_ratio, composite_score, rank, tier, account_value)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (profile.address, profile.discovered_at, profile.last_analyzed,
                 profile.total_trades, profile.winning_trades, profile.losing_trades,
                 profile.total_realized_pnl, profile.total_volume_usd,
                 profile.win_rate, profile.pnl_score, profile.sharpe_ratio,
                 profile.composite_score, profile.rank, profile.tier,
                 profile.account_value),
            )
            self._conn.commit()

    def save_signal(self, signal) -> None:
        """Explicitly save a smart money signal (non-callback path)."""
        self._save_smart_money_signal(signal)
        with self._lock:
            self._conn.commit()

    def load_wallets(self) -> list:
        """Load all wallet profiles from the database."""
        from src.data_layer.smart_money import WalletProfile

        with self._lock:
            rows = self._conn.execute(
                """SELECT address, discovered_at, last_analyzed, total_trades,
                          winning_trades, losing_trades, total_realized_pnl,
                          total_volume_usd, win_rate, pnl_score, sharpe_ratio,
                          composite_score, rank, tier, account_value
                   FROM wallets ORDER BY rank ASC"""
            ).fetchall()

        profiles = []
        for r in rows:
            profiles.append(WalletProfile(
                address=r[0],
                discovered_at=r[1] or 0.0,
                last_seen=r[1] or 0.0,  # use discovered_at as fallback
                last_analyzed=r[2] or 0.0,
                total_trades=r[3] or 0,
                winning_trades=r[4] or 0,
                losing_trades=r[5] or 0,
                total_realized_pnl=r[6] or 0.0,
                total_volume_usd=r[7] or 0.0,
                win_rate=r[8] or 0.0,
                pnl_score=r[9] or 0.0,
                sharpe_ratio=r[10] or 0.0,
                composite_score=r[11] or 0.0,
                rank=r[12] or 0,
                tier=r[13] or "unknown",
                account_value=r[14] or 0.0,
            ))
        return profiles

    def get_signals(self, hours: float = 24) -> list[dict]:
        """Get smart money signals from the last N hours."""
        cutoff = time.time() - (hours * 3600)
        with self._lock:
            rows = self._conn.execute(
                """SELECT timestamp, address, tier, action, symbol, size_usd,
                          wallet_rank, signal_type
                   FROM smart_money_signals
                   WHERE timestamp > ?
                   ORDER BY timestamp DESC LIMIT 500""",
                (cutoff,),
            ).fetchall()
        return [
            {"timestamp": r[0], "address": r[1], "tier": r[2], "action": r[3],
             "symbol": r[4], "size_usd": r[5], "wallet_rank": r[6], "signal_type": r[7]}
            for r in rows
        ]

    # ── HLP Persistence ──────────────────────────────────────

    def _save_hlp_trade(self, trade) -> None:
        """Callback: save an HLP trade."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO hlp_trades
                   (timestamp, symbol, side, price, size, size_usd, direction,
                    closed_pnl, is_liquidation, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (trade.timestamp, trade.symbol, trade.side, trade.price,
                 trade.size, trade.size_usd, trade.direction,
                 trade.closed_pnl, 1 if trade.is_liquidation else 0,
                 time.time()),
            )
            self._event_count += 1
            if self._event_count % 50 == 0:
                self._conn.commit()

    def save_hlp_snapshot(self, snapshot) -> None:
        """Save an HLP snapshot (call periodically, e.g. every 5th snapshot)."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO hlp_snapshots
                   (timestamp, account_value, net_delta, delta_zscore,
                    total_exposure, num_positions, session_pnl,
                    total_unrealized_pnl, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (snapshot.timestamp, snapshot.account_value,
                 snapshot.net_delta_usd, snapshot.delta_zscore,
                 snapshot.total_exposure_usd, snapshot.num_positions,
                 snapshot.session_pnl, snapshot.total_unrealized_pnl,
                 time.time()),
            )
            self._conn.commit()

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
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {"timestamp": r[0], "symbol": r[1], "side": r[2], "price": r[3],
             "size": r[4], "size_usd": r[5], "direction": r[6],
             "closed_pnl": r[7], "is_liquidation": bool(r[8])}
            for r in rows
        ]

    # ── Paper Trading Persistence ─────────────────────────────

    def save_paper_trade(self, trade) -> None:
        """Save a closed paper trade to the database."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO paper_trades
                   (timestamp, symbol, action, entry_price, exit_price,
                    size_usd, pnl, pnl_pct, exit_reason, validation_tier,
                    strategy_source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (trade.exit_timestamp, trade.symbol, trade.action,
                 trade.entry_price, trade.exit_price, trade.size_usd,
                 trade.pnl, trade.pnl_pct, trade.exit_reason,
                 trade.validation_tier, trade.strategy_source,
                 time.time()),
            )
            self._conn.commit()

    def get_paper_trades(self, hours: float = 24, limit: int = 500) -> list[dict]:
        """Get recent paper trades from the last N hours."""
        cutoff = time.time() - (hours * 3600)
        with self._lock:
            rows = self._conn.execute(
                """SELECT timestamp, symbol, action, entry_price, exit_price,
                          size_usd, pnl, pnl_pct, exit_reason, validation_tier,
                          strategy_source
                   FROM paper_trades WHERE timestamp > ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (cutoff, limit),
            ).fetchall()
        return [
            {"timestamp": r[0], "symbol": r[1], "action": r[2],
             "entry_price": r[3], "exit_price": r[4], "size_usd": r[5],
             "pnl": r[6], "pnl_pct": r[7], "exit_reason": r[8],
             "validation_tier": r[9], "strategy_source": r[10]}
            for r in rows
        ]

    def save_funding_rate(self, snap) -> None:
        """Save a funding rate snapshot."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO funding_rates (timestamp, exchange, symbol, funding_rate_hourly, funding_rate_annualized, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (snap.timestamp, snap.exchange, snap.symbol,
                 snap.funding_rate_hourly, snap.funding_rate_annualized, time.time()),
            )
            self._event_count += 1
            if self._event_count % 50 == 0:
                self._conn.commit()

    def get_funding_rates(self, exchange: str | None = None, symbol: str | None = None,
                          hours: float = 24, limit: int = 500) -> list[dict]:
        """Get historical funding rate snapshots."""
        cutoff = time.time() - (hours * 3600)
        query = "SELECT timestamp, exchange, symbol, funding_rate_hourly, funding_rate_annualized FROM funding_rates WHERE timestamp > ?"
        params: list = [cutoff]
        if exchange:
            query += " AND exchange = ?"
            params.append(exchange)
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol.upper())
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {"timestamp": r[0], "exchange": r[1], "symbol": r[2],
             "funding_rate_hourly": r[3], "funding_rate_annualized": r[4]}
            for r in rows
        ]

    def save_long_short_ratio(self, snap) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO long_short_ratios (timestamp, symbol, long_ratio, short_ratio, long_short_ratio, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (snap.timestamp, snap.symbol, snap.long_ratio, snap.short_ratio, snap.long_short_ratio, time.time()),
            )
            self._event_count += 1
            if self._event_count % 50 == 0:
                self._conn.commit()

    def get_long_short_ratios(self, symbol: str | None = None, hours: float = 24, limit: int = 200) -> list[dict]:
        cutoff = time.time() - (hours * 3600)
        query = "SELECT timestamp, symbol, long_ratio, short_ratio, long_short_ratio FROM long_short_ratios WHERE timestamp > ?"
        params: list = [cutoff]
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol.upper())
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {"timestamp": r[0], "symbol": r[1], "long_ratio": r[2],
             "short_ratio": r[3], "long_short_ratio": r[4]}
            for r in rows
        ]

    def save_options_snapshot(self, snap) -> None:
        """Save a Deribit IV snapshot."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO options_data (timestamp, underlying, mark_iv, bid_iv, ask_iv, oi_usd, index_price, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (snap.timestamp, snap.underlying, snap.mark_iv, snap.bid_iv, snap.ask_iv, snap.oi_usd, snap.index_price, time.time()),
            )
            self._event_count += 1
            if self._event_count % 50 == 0:
                self._conn.commit()

    def get_options_data(self, underlying: str | None = None, hours: float = 24, limit: int = 200) -> list[dict]:
        """Get historical Deribit IV snapshots."""
        cutoff = time.time() - (hours * 3600)
        query = "SELECT timestamp, underlying, mark_iv, bid_iv, ask_iv, oi_usd, index_price FROM options_data WHERE timestamp > ?"
        params: list = [cutoff]
        if underlying:
            query += " AND underlying = ?"
            params.append(underlying.upper())
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {"timestamp": r[0], "underlying": r[1], "mark_iv": r[2],
             "bid_iv": r[3], "ask_iv": r[4], "oi_usd": r[5], "index_price": r[6]}
            for r in rows
        ]
