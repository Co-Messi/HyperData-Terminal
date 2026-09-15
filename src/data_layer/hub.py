"""
HyperData Hub — Central data orchestrator.

Single source of truth that owns every data component, manages their lifecycles,
and exposes a unified interface for dashboards and agents. Start the hub once,
and everything works.

Usage:
    hub = HyperDataHub()
    await hub.start()          # connects to all exchanges, starts all engines
    ...
    await hub.stop()           # graceful shutdown

    # Access data from anywhere:
    hub.liquidations.get_stats(60)
    hub.positions.get_closest_longs(3)
    hub.orderflow.get_snapshot("BTC", "1h")
    hub.market.get_asset("ETH")
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from config.settings import DEFAULT_SYMBOLS
from src.api_server import HyperDataAPI
from src.data_layer import address_store, hub_demo
from src.data_layer.alerts import AlertManager
from src.data_layer.deribit import DeribitFeed, DeribitIVSnapshot
from src.data_layer.funding_rates import FundingRateCollector, FundingRateSnapshot
from src.data_layer.health_monitor import DataHealthMonitor
from src.data_layer.hlp_tracker import HLPPosition, HLPTracker, HLPTrade
from src.data_layer.liquidation_feed import LiquidationEvent, LiquidationFeed
from src.data_layer.long_short_ratio import LongShortCollector, LongShortSnapshot
from src.data_layer.market_data import AssetInfo, MarketData
from src.data_layer.orderbook import OrderBookEngine, OrderBookSnapshot
from src.data_layer.orderflow_engine import (
    STALE_AFTER_SECONDS as ORDERFLOW_STALE_AFTER,
)
from src.data_layer.orderflow_engine import (
    OrderFlowEngine,
    Trade,
)
from src.data_layer.persistence import DataStore
from src.data_layer.position_scanner import (
    ADDRESS_PRUNE_INTERVAL_SECONDS,
    SCAN_INTERVAL_SECONDS,
    PositionScanner,
    TrackedPosition,
)
from src.data_layer.smart_money import SmartMoneyEngine, SmartMoneySignal, WalletProfile
from src.data_layer.spot_prices import SpotPriceCollector, SpotPriceSnapshot

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = PROJECT_ROOT / "data"
STATE_FILE = STATE_DIR / "hub_state.json"

# The status loop ticks once a second; DB/address-store pruning runs on this
# many ticks. The position scanner's staleness threshold is derived from
# the address-store cap plus what discovery can add between prunes, so this
# must stay equal to position_scanner.ADDRESS_PRUNE_INTERVAL_SECONDS.
DB_PRUNE_INTERVAL_TICKS = int(ADDRESS_PRUNE_INTERVAL_SECONDS)


@dataclass
class HubStatus:
    """Real-time status of every component."""
    started_at: float = 0.0
    uptime_seconds: float = 0.0
    mode: str = "offline"  # 'live', 'demo', 'offline'

    # Component health:
    #   'connecting'  start() returned (tasks created) but no data has arrived
    #   'connected'   data flowing from every venue the component covers
    #   'partial'     data flowing, but at least one venue is not (order flow)
    #   'stale'       had data, nothing recently
    #   'error' / 'offline' / 'demo' / 'starting'
    liquidation_feed: str = "offline"
    position_scanner: str = "offline"
    orderflow_engine: str = "offline"
    orderbook_feed: str = "offline"
    market_data: str = "offline"

    # Components that raised during start(); non-empty means degraded mode.
    failed_components: list = field(default_factory=list)

    # Counters
    total_liquidations: int = 0
    total_trades_processed: int = 0
    tracked_positions: int = 0
    tracked_assets: int = 0
    discovered_addresses: int = 0

    # Last update times
    last_liq_event: float = 0.0
    last_position_scan: float = 0.0
    last_trade: float = 0.0
    last_market_refresh: float = 0.0

    scan_cycle: int = 0

    # Smart money
    tracked_wallets: int = 0
    ranked_wallets: int = 0
    smart_money_signals: int = 0

    # HLP
    hlp_status: str = "offline"
    hlp_account_value: float = 0.0
    hlp_net_delta: float = 0.0
    hlp_delta_zscore: float = 0.0
    hlp_positions: int = 0
    hlp_trades: int = 0
    hlp_liquidation_absorptions: int = 0
    hlp_session_pnl: float = 0.0

    # Persistence
    db_size_mb: float = 0.0
    events_persisted: int = 0

    # Funding rates
    funding_rate_symbols_binance: int = 0
    funding_rate_symbols_bybit: int = 0

    # Long/short ratios
    lsr_btc_ratio: float = 0.0
    lsr_eth_ratio: float = 0.0

    # Orderbook
    orderbook_symbols: int = 0

    # Spot prices / basis
    spot_btc_basis_pct: float = 0.0
    spot_eth_basis_pct: float = 0.0

    # Deribit IV
    deribit_btc_iv: float = 0.0
    deribit_eth_iv: float = 0.0


class HyperDataHub:
    """Central data hub that owns and orchestrates all data components."""

    def __init__(
        self,
        symbols: list[str] | None = None,
        demo: bool = False,
        scan_interval: float = SCAN_INTERVAL_SECONDS,
        market_refresh_interval: float = 5.0,
        api_port: int | None = None,
    ) -> None:
        self.demo = demo
        self._api_port = api_port
        self._api_server: HyperDataAPI | None = None
        self.symbols = symbols or list(DEFAULT_SYMBOLS)
        self.scan_interval = scan_interval
        if scan_interval > SCAN_INTERVAL_SECONDS:
            # POSITION_STALE_AFTER_SECONDS is derived from the default
            # interval; a slower cadence WILL trip it on a healthy scanner.
            logger.warning(
                "scan_interval=%.0fs exceeds the %.0fs the position-scan staleness "
                "threshold is derived from — a healthy scanner may read 'stale'",
                scan_interval, SCAN_INTERVAL_SECONDS,
            )
        self.market_refresh_interval = market_refresh_interval

        # ── Core components ──────────────────────────────────────
        # DataStore FIRST: it owns data/hyperdata.db, runs the integrity
        # check + quarantine path, and creates the versioned schema that
        # address_store (used by PositionScanner below) reads from. Opening
        # the scanner first would hit a corrupted file before it could be
        # quarantined.
        self.store = DataStore()
        self.liquidations = LiquidationFeed()
        self.positions = PositionScanner()
        # DEFAULT_SYMBOLS (config/settings.py). Nothing expands this list at
        # runtime: the Hyperliquid shard plan is fixed for the session at
        # OrderFlowEngine.start(), and add_symbol() only adds buckets.
        self.orderflow = OrderFlowEngine(symbols=self.symbols)
        self.market = MarketData()
        self.alerts = AlertManager()
        self.smart_money = SmartMoneyEngine()
        self.hlp = HLPTracker()
        self.funding = FundingRateCollector()
        self.lsr = LongShortCollector()
        self.orderbook = OrderBookEngine()
        self.spot = SpotPriceCollector()
        self.deribit = DeribitFeed()
        self.health = DataHealthMonitor(self)

        # ── Status tracking ──────────────────────────────────────
        self.status = HubStatus()

        # ── Event bus — anyone can subscribe ─────────────────────
        self._on_liquidation_cbs: list[Callable] = []
        self._on_trade_cbs: list[Callable] = []
        self._on_scan_cbs: list[Callable] = []
        self._on_signal_cbs: list[Callable] = []
        self._on_hlp_trade_cbs: list[Callable] = []

        # ── Background tasks ─────────────────────────────────────
        self._tasks: list[asyncio.Task] = []
        self._running = False
        # Debounce for per-venue orderflow staleness warnings.
        self._venue_stale_warned_at: dict[str, float] = {}

        # Wire up internal callbacks
        self.liquidations.on_liquidation(self._handle_liquidation)
        self.orderflow.on_trade(self._handle_trade)
        self.smart_money.on_signal(self._handle_signal)
        self.hlp.on_hlp_trade(self._handle_hlp_trade)

    # ── Event bus ─────────────────────────────────────────────────

    def on_liquidation(self, cb: Callable[[LiquidationEvent], Any]) -> None:
        self._on_liquidation_cbs.append(cb)

    def on_trade(self, cb: Callable[[Trade], Any]) -> None:
        self._on_trade_cbs.append(cb)

    def on_scan_complete(self, cb: Callable[[list[TrackedPosition]], Any]) -> None:
        self._on_scan_cbs.append(cb)

    def on_signal(self, cb: Callable[[SmartMoneySignal], Any]) -> None:
        self._on_signal_cbs.append(cb)

    def on_hlp_trade(self, cb: Callable[[HLPTrade], Any]) -> None:
        self._on_hlp_trade_cbs.append(cb)

    def _handle_signal(self, signal: SmartMoneySignal) -> None:
        self.status.smart_money_signals += 1
        for cb in self._on_signal_cbs:
            try:
                cb(signal)
            except Exception:
                logger.exception("Signal callback error")

    def _handle_liquidation(self, event: LiquidationEvent) -> None:
        self.status.total_liquidations += 1
        self.status.last_liq_event = time.time()
        for cb in self._on_liquidation_cbs:
            try:
                cb(event)
            except Exception:
                logger.exception("Liquidation callback error")

    def _handle_trade(self, trade: Trade) -> None:
        self.status.total_trades_processed += 1
        self.status.last_trade = time.time()
        for cb in self._on_trade_cbs:
            try:
                cb(trade)
            except Exception:
                logger.exception("Trade callback error")

    def _handle_hlp_trade(self, trade: HLPTrade) -> None:
        self.status.hlp_trades += 1
        if trade.is_liquidation:
            self.status.hlp_liquidation_absorptions += 1
        for cb in self._on_hlp_trade_cbs:
            try:
                cb(trade)
            except Exception:
                logger.exception("HLP trade callback error")

    # ── Lifecycle ─────────────────────────────────────────────────

    async def start(self) -> None:
        """Start all components and background loops."""
        if self._running:
            return
        self._running = True
        self.status.started_at = time.time()
        self.status.mode = "demo" if self.demo else "live"

        logger.info("HyperDataHub starting in %s mode...", self.status.mode)

        if self.demo:
            await self._start_demo()
        else:
            await self._start_live()

        # Start REST API server if port is configured (both modes)
        if self._api_port:
            try:
                # Loopback by default; a non-loopback HYPERDATA_API_HOST is
                # refused unless HYPERDATA_API_KEY or the explicit unsafe
                # acknowledgment is set (see HyperDataAPI._resolve_security).
                api_host = os.environ.get("HYPERDATA_API_HOST", "127.0.0.1")
                self._api_server = HyperDataAPI(self, host=api_host, port=self._api_port)
                await self._api_server.start()
            except Exception:
                self._api_server = None
                self.status.failed_components.append("api_server")
                logger.exception("Failed to start API server")

        # Background loops that run in both modes
        self._tasks.append(asyncio.create_task(
            self._position_scan_loop(), name="position-scan"
        ))
        self._tasks.append(asyncio.create_task(
            self._market_refresh_loop(), name="market-refresh"
        ))
        self._tasks.append(asyncio.create_task(
            self._status_update_loop(), name="status-update"
        ))
        # Continuous data-integrity verification against external sources.
        # Live mode only — demo prices would always "drift" from real Binance.
        if not self.demo:
            self._tasks.append(asyncio.create_task(
                self._health_monitor_loop(), name="health-monitor"
            ))

        # Attach persistence layer — saves all events to SQLite
        self.store.attach(self)

        # ── Alerts ─────────────────────────────────────────────
        try:
            await self.alerts.start()
            self.alerts.attach(self)
        except Exception:
            self.status.failed_components.append("alerts")
            logger.exception("Failed to start alert manager")

        if self.status.failed_components:
            logger.error(
                "HyperDataHub started DEGRADED — failed components: %s. "
                "Data from these sources will be missing or stale.",
                ", ".join(self.status.failed_components),
            )
        else:
            logger.info("HyperDataHub started — all components online")

    async def _start_component(self, name: str, coro, required: bool = False,
                               on_ok=None, on_fail=None) -> bool:
        """Start one component, recording failures instead of hiding them.

        A failed *required* component raises and aborts startup; a failed
        optional component is appended to status.failed_components so health
        surfaces (log line, /v1/health) report degraded mode honestly.
        """
        try:
            await coro
            if on_ok:
                on_ok()
            logger.info("%s: started", name)
            return True
        except Exception:
            if on_fail:
                on_fail()
            if required:
                logger.exception("Required component %s failed to start", name)
                raise
            self.status.failed_components.append(name)
            logger.exception("Failed to start %s (continuing degraded)", name)
            return False

    async def _start_live(self) -> None:
        """Connect to real exchange APIs.

        Every component is optional-but-reported: a failure puts it in
        status.failed_components (surfaced via /v1/health and the startup
        log) instead of being silently swallowed.
        """
        s = self.status
        # WS-driven components: start() only creates tasks and returns before
        # any handshake, so a successful start() means 'connecting', not
        # 'connected'. The staleness watchdog promotes to 'connected' on the
        # first real data (M11).
        await self._start_component(
            "liquidation_feed", self.liquidations.start(),
            on_ok=lambda: setattr(s, "liquidation_feed", "connecting"),
            on_fail=lambda: setattr(s, "liquidation_feed", "error"),
        )
        await self._start_component(
            "orderflow_engine", self.orderflow.start(),
            on_ok=lambda: setattr(s, "orderflow_engine", "connecting"),
            on_fail=lambda: setattr(s, "orderflow_engine", "error"),
        )
        await self._start_component("smart_money", self.smart_money.start())
        await self._start_component(
            "hlp_tracker", self.hlp.start(),
            on_ok=lambda: setattr(s, "hlp_status", "connecting"),
            on_fail=lambda: setattr(s, "hlp_status", "error"),
        )
        await self._start_component("funding_rates", self.funding.start())
        await self._start_component("long_short_ratio", self.lsr.start())
        await self._start_component(
            "orderbook", self.orderbook.start(),
            on_ok=lambda: setattr(s, "orderbook_feed", "connecting"),
            on_fail=lambda: setattr(s, "orderbook_feed", "error"),
        )
        await self._start_component(
            "spot_prices",
            self.spot.start(perp_price_fn=lambda sym: self.market.assets.get(sym)),
        )
        await self._start_component("deribit_iv", self.deribit.start())

        # Loop-driven components: 'starting' until their first cycle actually
        # succeeds (the loops flip these to 'connected'/'error'). Never claim
        # 'ready' for something that has not fetched anything yet.
        self.status.position_scanner = "starting"
        self.status.market_data = "starting"

    async def _start_demo(self) -> None:
        """Start mock data generators."""
        self.status.liquidation_feed = "demo"
        self.status.orderflow_engine = "demo"
        self.status.position_scanner = "demo"
        self.status.market_data = "demo"
        self.status.hlp_status = "demo"
        # Demo trades bypass the venue sockets; renderers label the CVD as
        # synthetic rather than attributing it to a venue.
        self.orderflow.synthetic = True

        self._tasks.append(asyncio.create_task(
            self._demo_liquidation_generator(), name="demo-liqs"
        ))
        self._tasks.append(asyncio.create_task(
            self._demo_trade_generator(), name="demo-trades"
        ))
        self._tasks.append(asyncio.create_task(
            self._demo_smart_money(), name="demo-smart-money"
        ))
        self._tasks.append(asyncio.create_task(
            self._demo_hlp(), name="demo-hlp"
        ))
        self._tasks.append(asyncio.create_task(
            self._demo_deribit(), name="demo-deribit"
        ))
        self._tasks.append(asyncio.create_task(
            self._demo_basis(), name="demo-basis"
        ))
        self._tasks.append(asyncio.create_task(
            self._demo_lsr(), name="demo-lsr"
        ))

    async def stop(self) -> None:
        """Graceful shutdown of all components."""
        self._running = False

        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Every component is stopped even if an earlier one raises, and a
        # component that fails to stop is LOGGED (a leaked socket or task at
        # shutdown is evidence, not noise). Nine bare `except: pass` blocks
        # used to live here.
        if not self.demo:
            components = (
                ("liquidation_feed", self.liquidations),
                ("orderflow_engine", self.orderflow),
                ("smart_money", self.smart_money),
                ("hlp_tracker", self.hlp),
                ("funding_rates", self.funding),
                ("long_short_ratio", self.lsr),
                ("orderbook", self.orderbook),
                ("spot_prices", self.spot),
                ("deribit_iv", self.deribit),
            )
            for name, component in components:
                try:
                    await component.stop()
                except Exception:
                    logger.exception("Error stopping %s (continuing shutdown)", name)

        # Stop API server
        if self._api_server:
            try:
                await self._api_server.stop()
            except Exception:
                logger.exception("Error stopping api_server (continuing shutdown)")

        # Flush and close persistence. close() flushes; a False return
        # means writes were lost (already logged at ERROR by the store) —
        # repeat it here so the shutdown log line itself says so.
        try:
            if not self.store.close():
                logger.error("HyperDataHub stopped with unflushed persistence writes (see DataStore errors above)")
        except Exception:
            logger.exception("Error closing persistence store")

        await self.alerts.stop()

        self.status.mode = "offline"
        logger.info("HyperDataHub stopped")

    # ── Background loops ──────────────────────────────────────────

    async def _position_scan_loop(self) -> None:
        """Periodically scan positions."""
        while self._running:
            try:
                if self.demo:
                    await self._demo_position_scan()
                else:
                    all_positions = await self.positions.scan()
                    self.status.tracked_positions = len(all_positions)
                    self.status.discovered_addresses = len(self.positions.discovered_addresses)
                    # 'connected' only after a scan actually succeeded.
                    self.status.position_scanner = "connected"

                self.status.last_position_scan = time.time()
                self.status.scan_cycle += 1

                # Notify subscribers
                for cb in self._on_scan_cbs:
                    try:
                        cb(self.positions.positions)
                    except Exception:
                        logger.exception("Scan callback error")

            except asyncio.CancelledError:
                break
            except Exception:
                if not self.demo:
                    self.status.position_scanner = "error"
                logger.exception("Position scan error")

            await asyncio.sleep(self.scan_interval)

    async def _market_refresh_loop(self) -> None:
        """Periodically refresh market data."""
        while self._running:
            try:
                if self.demo:
                    await self._demo_market_refresh()
                else:
                    await self.market.refresh()
                    # 'connected' only after a refresh actually succeeded.
                    self.status.market_data = "connected"

                self.status.last_market_refresh = time.time()
                self.status.tracked_assets = len(self.market.assets)

            except asyncio.CancelledError:
                break
            except Exception:
                if not self.demo:
                    self.status.market_data = "error"
                logger.exception("Market refresh error")

            await asyncio.sleep(self.market_refresh_interval)

    async def _status_update_loop(self) -> None:
        """Update uptime counter and periodically refresh DB stats.

        The body is wrapped so one component's bad stats shape can't kill the
        loop — this loop is also the staleness watchdog and the source of
        /v1/health data, so it must outlive individual component errors.
        """
        _db_tick = 0
        while self._running:
            try:
                _db_tick = await self._status_update_tick(_db_tick)
                # NOTE: HLP z-score alert dispatch was removed here — the
                # AlertManager z-score/cascade sends are deliberately disabled
                # (too noisy), so scheduling tasks for them was dead work.
                # Re-add scheduling here if those alerts are re-enabled.
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Status update loop error")

            await asyncio.sleep(1)

    async def _status_update_tick(self, _db_tick: int) -> int:
        """One status-loop iteration. Returns the incremented DB tick."""
        self.status.uptime_seconds = time.time() - self.status.started_at

        # Update smart money stats every tick
        sm_stats = self.smart_money.get_stats()
        self.status.tracked_wallets = sm_stats["total_wallets"]
        self.status.ranked_wallets = sm_stats["ranked_wallets"]
        self.status.smart_money_signals = sm_stats["total_signals"]

        # Update HLP stats every tick
        hlp_stats = self.hlp.get_stats()
        self.status.hlp_account_value = hlp_stats["account_value"]
        self.status.hlp_net_delta = hlp_stats["net_delta"]
        self.status.hlp_delta_zscore = hlp_stats["delta_zscore"]
        self.status.hlp_positions = hlp_stats["num_positions"]
        self.status.hlp_trades = hlp_stats["total_trades"]
        self.status.hlp_liquidation_absorptions = hlp_stats["liquidation_absorptions"]
        self.status.hlp_session_pnl = hlp_stats["session_pnl"]

        # Persist HLP snapshots periodically
        self.store.maybe_save_hlp_snapshot()

        _db_tick += 1
        # Prune old rows + checkpoint the WAL roughly hourly so the DB and
        # the COUNT(*) below stay bounded on long-running instances. These
        # are blocking full-table operations, so they run off the event loop
        # (H6) — this loop is also the staleness watchdog and must not stall.
        if _db_tick % DB_PRUNE_INTERVAL_TICKS == 0:
            try:
                await asyncio.to_thread(self.store.prune)
            except Exception:
                logger.exception("Error pruning DB")
            try:
                await asyncio.to_thread(address_store.prune)
                # prune() trims the TABLE; the scanner loaded its set once at
                # construction and would otherwise keep every pruned address
                # forever, making full-pass time — and therefore the staleness
                # guarantee — unbounded (B1).
                dropped = await self.positions.resync_addresses()
                if dropped:
                    logger.info(
                        "[hub] position scanner dropped %d pruned addresses; %d tracked",
                        dropped, len(self.positions.discovered_addresses),
                    )
            except Exception:
                logger.exception("Error pruning address store")
        # Update persistence stats every 30 seconds
        if _db_tick % 30 == 0:
            try:
                db_stats = await asyncio.to_thread(self.store.get_db_stats)
                self.status.db_size_mb = db_stats["db_size_mb"]
                self.status.events_persisted = (
                    db_stats["liquidations_stored"] + db_stats["trades_stored"]
                )
            except Exception:
                logger.exception("Error fetching DB stats")
            try:
                for ex_rates in self.funding.rates.values():
                    for snap in ex_rates.values():
                        self.store.save_funding_rate(snap)
            except Exception:
                logger.exception("Error saving funding rate snapshots")
            try:
                for snap in self.lsr.ratios.values():
                    self.store.save_long_short_ratio(snap)
            except Exception:
                logger.exception("Error saving LSR snapshots")
            try:
                for snap in self.deribit.snapshots.values():
                    self.store.save_options_snapshot(snap)
            except Exception:
                logger.exception("Error saving Deribit IV snapshots")

        # Update new component status fields every tick
        self.status.funding_rate_symbols_binance = len(self.funding.rates.get("binance", {}))
        self.status.funding_rate_symbols_bybit = len(self.funding.rates.get("bybit", {}))

        btc_lsr = self.lsr.get_latest("BTC")
        eth_lsr = self.lsr.get_latest("ETH")
        self.status.lsr_btc_ratio = btc_lsr.long_short_ratio if btc_lsr else 0.0
        self.status.lsr_eth_ratio = eth_lsr.long_short_ratio if eth_lsr else 0.0

        self.status.orderbook_symbols = len(self.orderbook.snapshots)

        btc_spot = self.spot.get_latest("BTC")
        eth_spot = self.spot.get_latest("ETH")
        self.status.spot_btc_basis_pct = btc_spot.basis_pct if btc_spot else 0.0
        self.status.spot_eth_basis_pct = eth_spot.basis_pct if eth_spot else 0.0

        btc_iv = self.deribit.get_latest("BTC")
        eth_iv = self.deribit.get_latest("ETH")
        self.status.deribit_btc_iv = btc_iv.mark_iv if btc_iv else 0.0
        self.status.deribit_eth_iv = eth_iv.mark_iv if eth_iv else 0.0
        # ── Staleness watchdog ──────────────────────────────────
        # Flag WS feeds that have stopped delivering data as 'stale' so the
        # UI/API never present frozen numbers as live, and force a reconnect
        # on a socket that's alive-but-silent (heartbeat only catches
        # half-open connections, not a venue that quietly stops sending).
        if not self.demo:
            await self._update_feed_staleness()

        return _db_tick

    async def _health_monitor_loop(self) -> None:
        """Run data-integrity checks against external sources on an interval.

        Caches the result on self.health for the REST API (/v1/health) and the
        dashboard health badge to read via self.health.latest().
        """
        await asyncio.sleep(15)  # warm-up so feeds have data before first check
        while self._running:
            try:
                await self.health.run_checks()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Health monitor loop error")
            await asyncio.sleep(30)

    # Debounce for per-venue order-flow warnings (a persistent regional block
    # would otherwise log every status tick).
    VENUE_WARN_INTERVAL = 300.0

    async def _update_feed_staleness(self) -> None:
        """Reflect real data flow into per-feed status; force-reconnect dead sockets.

        Live mode only. A feed already in 'error'/'offline' is left alone — that
        is a connection failure, not a data-flow stall. Liquidations are
        intentionally NOT aged out (they are sporadic; a quiet market is not a
        broken feed) but they ARE promoted from 'connecting' on the first event.
        """
        # Order flow (Hyperliquid + Binance trades).
        if self.status.orderflow_engine in ("connecting", "connected", "partial", "stale"):
            fresh = self.orderflow.venue_freshness()
            statuses = {v: info["status"] for v, info in fresh.items()}
            # The hub's own first seconds: sockets are still being opened, so
            # 'never connected' is expected and must not read as stale.
            in_startup_grace = (time.time() - self.status.started_at) < ORDERFLOW_STALE_AFTER
            if self.orderflow.last_message_at <= 0:
                # No venue has delivered a trade yet: 'connecting' while the
                # hub or any venue is inside its grace window, otherwise
                # nothing is flowing and that is 'stale', not 'connected'.
                still_connecting = in_startup_grace or "connecting" in statuses.values()
                state = "connecting" if still_connecting else "stale"
            elif self.orderflow.is_stale():
                state = "stale"
            elif all(s == "ok" for s in statuses.values()):
                state = "connected"
            else:
                # Combined freshness follows the freshest venue, so one dead
                # venue can hide behind the other. Say so.
                state = "partial"
            self.status.orderflow_engine = state

            # Per-venue warning for ANY non-ok venue — explicitly including one
            # that has never delivered a byte (the previous `!= inf` guard
            # excluded exactly the connected-but-silent case).
            now_w = time.time()
            contributing = self.orderflow.contributing_venues()
            for venue, info in fresh.items():
                if info["status"] in ("ok", "connecting") or in_startup_grace:
                    continue
                if now_w - self._venue_stale_warned_at.get(venue, 0.0) > self.VENUE_WARN_INTERVAL:
                    self._venue_stale_warned_at[venue] = now_w
                    logger.warning(
                        "[hub] order flow venue %s is %s (%s) — CVD/OFI currently "
                        "reflect %s", venue, info["status"], info["reason"],
                        ", ".join(contributing) if contributing else "NO venue",
                    )
            # Both venues silent for well past the threshold → kick the HL
            # socket so its backoff loop rebuilds it. The Binance loop self-heals
            # via its own heartbeat, and if Binance were still feeding, the
            # combined data_age() would not be stale in the first place.
            #
            # Guarded so it only fires when we HAD data and it stopped (not
            # during initial connect, where data_age is infinite), and debounced
            # so a persistent outage can't spam close()/logs every status tick.
            now = time.time()
            cooldown = ORDERFLOW_STALE_AFTER * 2
            had_data = self.orderflow.last_message_at > 0
            since_last_force = now - getattr(self, "_last_of_force_reconnect", 0.0)
            if had_data and self.orderflow.data_age() > cooldown and since_last_force > cooldown:
                if self.orderflow.hl_sockets_open:
                    self._last_of_force_reconnect = now
                    logger.warning(
                        "[hub] order flow silent %.0fs — forcing HL reconnect (%d sockets)",
                        self.orderflow.data_age(), self.orderflow.hl_sockets_open,
                    )
                    await self.orderflow.force_reconnect()

        # Orderbook (HL l2Book) — the engine's own watchdog forces reconnects,
        # so here we only reflect freshness into the status.
        if self.status.orderbook_feed in ("connecting", "connected", "stale"):
            if self.orderbook.last_message_at <= 0:
                pass  # still 'connecting': nothing received yet
            else:
                self.status.orderbook_feed = (
                    "stale" if self.orderbook.is_stale() else "connected"
                )

        # Position scanner (H4): 'connected' only while every displayed
        # position was re-fetched recently; a scanner that fell behind its
        # budget or stopped completing cycles reads 'stale', not 'connected'.
        if self.status.position_scanner in ("connected", "stale"):
            self.status.position_scanner = "stale" if self.positions.is_stale() else "connected"

        # Liquidations: promote on the first real event, never age out.
        if self.status.liquidation_feed == "connecting" and self.status.last_liq_event > 0:
            self.status.liquidation_feed = "connected"

        # HLP: promote once the first vault snapshot has landed.
        if self.status.hlp_status == "connecting" and self.hlp.snapshots:
            self.status.hlp_status = "connected"

    # ── Demo data generators ──────────────────────────────────────
    # Bodies live in src/data_layer/hub_demo.py (M13); these delegators keep
    # task names, call sites and tests unchanged.

    async def _demo_liquidation_generator(self) -> None:
        await hub_demo.demo_liquidation_generator(self)

    async def _demo_trade_generator(self) -> None:
        await hub_demo.demo_trade_generator(self)

    async def _demo_position_scan(self) -> None:
        await hub_demo.demo_position_scan(self)

    async def _demo_smart_money(self) -> None:
        await hub_demo.demo_smart_money(self)

    async def _demo_hlp(self) -> None:
        await hub_demo.demo_hlp(self)

    async def _demo_market_refresh(self) -> None:
        await hub_demo.demo_market_refresh(self)

    async def _demo_deribit(self) -> None:
        await hub_demo.demo_deribit(self)

    async def _demo_basis(self) -> None:
        await hub_demo.demo_basis(self)

    async def _demo_lsr(self) -> None:
        await hub_demo.demo_lsr(self)

    # ── Convenience accessors ─────────────────────────────────────
    # These let dashboards and agents query data without knowing internals.

    def get_btc_price(self) -> float:
        """Current BTC price from best available source."""
        # Try market data first
        asset = self.market.assets.get("BTC")
        if asset and asset.price > 0:
            return asset.price
        # Fall back to position scanner
        return self.positions.market_prices.get("BTC", 0.0)

    def get_positions_by_symbol(self, symbol: str) -> list[TrackedPosition]:
        """Get positions for a specific symbol, sorted by distance to liquidation."""
        return sorted(
            [p for p in self.positions.positions if p.symbol == symbol],
            key=lambda p: p.distance_pct,
        )

    def get_all_positions_sorted(self) -> list[TrackedPosition]:
        """All positions sorted by distance to liquidation."""
        return sorted(self.positions.positions, key=lambda p: p.distance_pct)

    def get_whale_positions(self, min_size_usd: float = 100_000) -> list[TrackedPosition]:
        """Get whale-sized positions sorted by size descending."""
        return sorted(
            [p for p in self.positions.positions if p.size_usd >= min_size_usd],
            key=lambda p: p.size_usd,
            reverse=True,
        )

    def get_all_assets(self) -> list[AssetInfo]:
        """All tracked assets sorted by volume."""
        return sorted(
            self.market.assets.values(),
            key=lambda a: a.volume_24h,
            reverse=True,
        )

    def get_extreme_funding(self, threshold_annualized: float = 0.10) -> list[AssetInfo]:
        """Assets with extreme funding rates."""
        return sorted(
            [a for a in self.market.assets.values()
             if abs(a.funding_rate * 8760) >= threshold_annualized],
            key=lambda a: abs(a.funding_rate),
            reverse=True,
        )

    @property
    def funding_rates(self) -> dict[str, dict[str, "FundingRateSnapshot"]]:
        """Live funding rates: funding_rates[exchange][symbol]."""
        return self.funding.rates

    @property
    def long_short_ratios(self) -> dict[str, "LongShortSnapshot"]:
        """Live L/S ratios: long_short_ratios[symbol] = LongShortSnapshot."""
        return self.lsr.ratios

    def get_orderbook(self, symbol: str) -> "OrderBookSnapshot | None":
        """Get latest orderbook snapshot for a symbol."""
        return self.orderbook.get_snapshot(symbol)

    @property
    def spot_prices(self) -> dict[str, "SpotPriceSnapshot"]:
        """Live spot prices: spot_prices[symbol] = SpotPriceSnapshot."""
        return self.spot.prices

    @property
    def options_data(self) -> dict[str, "DeribitIVSnapshot"]:
        """Live Deribit IV data: options_data[underlying] = DeribitIVSnapshot."""
        return self.deribit.snapshots

    # ── Smart Money accessors ──────────────────────────────────────────

    def get_smart_money(self, n: int = 20) -> list[WalletProfile]:
        """Get top N smart money wallets."""
        return self.smart_money.get_smart_money(n)

    def get_dumb_money(self, n: int = 20) -> list[WalletProfile]:
        """Get bottom N wallets."""
        return self.smart_money.get_dumb_money(n)

    def get_smart_money_signals(self, n: int = 50) -> list[SmartMoneySignal]:
        """Get recent smart money signals."""
        return self.smart_money.get_recent_signals(n)

    # ── HLP accessors ──────────────────────────────────────────────

    def get_hlp_stats(self) -> dict:
        """Get current HLP statistics."""
        return self.hlp.get_stats()

    def get_hlp_top_positions(self, n: int = 10) -> list[HLPPosition]:
        """Get top N HLP positions by size."""
        return self.hlp.get_top_positions(n)

    def get_hlp_recent_trades(self, n: int = 50) -> list[HLPTrade]:
        """Get recent HLP trades."""
        return self.hlp.get_recent_trades(n)

    def get_hlp_liquidation_absorptions(self, minutes: int = 60) -> list[HLPTrade]:
        """Get recent liquidation absorptions by HLP."""
        return self.hlp.get_liquidation_absorptions(minutes)

    def get_hlp_delta_history(self, n: int = 100) -> list[tuple[float, float]]:
        """Get HLP net delta history for charting."""
        return self.hlp.get_delta_history(n)

