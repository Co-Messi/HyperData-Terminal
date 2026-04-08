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
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from config.settings import DEFAULT_SYMBOLS
from src.data_layer.alerts import AlertManager
from src.data_layer.liquidation_feed import LiquidationFeed, LiquidationEvent
from src.data_layer.position_scanner import PositionScanner, TrackedPosition
from src.data_layer.orderflow_engine import OrderFlowEngine, Trade, CVDSnapshot
from src.data_layer.market_data import MarketData, AssetInfo
from src.data_layer.persistence import DataStore
from src.data_layer.smart_money import SmartMoneyEngine, SmartMoneySignal, WalletProfile
from src.data_layer.hlp_tracker import HLPTracker, HLPSnapshot, HLPPosition, HLPTrade
from src.data_layer.funding_rates import FundingRateCollector, FundingRateSnapshot
from src.data_layer.long_short_ratio import LongShortCollector, LongShortSnapshot
from src.data_layer.orderbook import OrderBookEngine, OrderBookSnapshot
from src.data_layer.spot_prices import SpotPriceCollector, SpotPriceSnapshot
from src.data_layer.deribit import DeribitFeed, DeribitIVSnapshot
from src.api_server import HyperDataAPI

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = PROJECT_ROOT / "data"
STATE_FILE = STATE_DIR / "hub_state.json"


@dataclass
class HubStatus:
    """Real-time status of every component."""
    started_at: float = 0.0
    uptime_seconds: float = 0.0
    mode: str = "offline"  # 'live', 'demo', 'offline'

    # Component health
    liquidation_feed: str = "offline"   # 'connected', 'reconnecting', 'offline'
    position_scanner: str = "offline"
    orderflow_engine: str = "offline"
    market_data: str = "offline"

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
        scan_interval: float = 15.0,
        market_refresh_interval: float = 5.0,
        api_port: int | None = None,
    ) -> None:
        self.demo = demo
        self._api_port = api_port
        self._api_server: HyperDataAPI | None = None
        self.symbols = symbols or list(DEFAULT_SYMBOLS)
        self.scan_interval = scan_interval
        self.market_refresh_interval = market_refresh_interval

        # ── Core components ──────────────────────────────────────
        self.liquidations = LiquidationFeed()
        self.positions = PositionScanner()
        # Start with top 50 symbols — dynamically expanded after market data loads
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
        self.store = DataStore()

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
                self._api_server = HyperDataAPI(self, port=self._api_port)
                await self._api_server.start()
            except Exception:
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

        # Attach persistence layer — saves all events to SQLite
        self.store.attach(self)

        # ── Alerts ─────────────────────────────────────────────
        await self.alerts.start()
        self.alerts.attach(self)

        logger.info("HyperDataHub started — all components online")

    async def _start_live(self) -> None:
        """Connect to real exchange APIs."""
        # Start liquidation WebSocket feeds
        try:
            await self.liquidations.start()
            self.status.liquidation_feed = "connected"
            logger.info("Liquidation feed: connected")
        except Exception:
            self.status.liquidation_feed = "error"
            logger.exception("Failed to start liquidation feed")

        # Start order flow WebSocket
        try:
            await self.orderflow.start()
            self.status.orderflow_engine = "connected"
            logger.info("Order flow engine: connected")
        except Exception:
            self.status.orderflow_engine = "error"
            logger.exception("Failed to start order flow engine")

        # Start smart money engine
        try:
            await self.smart_money.start()
            logger.info("Smart money engine: started")
        except Exception:
            logger.exception("Failed to start smart money engine")

        # Start HLP tracker
        try:
            await self.hlp.start()
            self.status.hlp_status = "connected"
            logger.info("HLP tracker: started")
        except Exception:
            self.status.hlp_status = "error"
            logger.exception("Failed to start HLP tracker")

        try:
            await self.funding.start()
            logger.info("Funding rate collector: started")
        except Exception:
            logger.exception("Failed to start funding rate collector")

        try:
            await self.lsr.start()
            logger.info("Long/short ratio collector: started")
        except Exception:
            logger.exception("Failed to start long/short ratio collector")

        try:
            await self.orderbook.start()
            logger.info("OrderBook engine: started")
        except Exception:
            logger.exception("Failed to start orderbook engine")

        try:
            await self.spot.start(perp_price_fn=lambda sym: self.market.assets.get(sym))
            logger.info("Spot price collector: started")
        except Exception:
            logger.exception("Failed to start spot price collector")

        try:
            await self.deribit.start()
            logger.info("Deribit IV feed: started")
        except Exception:
            logger.exception("Failed to start Deribit IV feed")

        self.status.position_scanner = "ready"
        self.status.market_data = "ready"

    async def _start_demo(self) -> None:
        """Start mock data generators."""
        self.status.liquidation_feed = "demo"
        self.status.orderflow_engine = "demo"
        self.status.position_scanner = "demo"
        self.status.market_data = "demo"
        self.status.hlp_status = "demo"

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

        if not self.demo:
            try:
                await self.liquidations.stop()
            except Exception:
                pass
            try:
                await self.orderflow.stop()
            except Exception:
                pass
            try:
                await self.smart_money.stop()
            except Exception:
                pass
            try:
                await self.hlp.stop()
            except Exception:
                pass
            try:
                await self.funding.stop()
            except Exception:
                pass
            try:
                await self.lsr.stop()
            except Exception:
                pass
            try:
                await self.orderbook.stop()
            except Exception:
                pass
            try:
                await self.spot.stop()
            except Exception:
                pass
            try:
                await self.deribit.stop()
            except Exception:
                pass

        # Stop API server
        if self._api_server:
            try:
                await self._api_server.stop()
            except Exception:
                pass

        # Flush and close persistence
        try:
            self.store.flush()
            self.store.close()
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

                self.status.last_market_refresh = time.time()
                self.status.tracked_assets = len(self.market.assets)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Market refresh error")

            await asyncio.sleep(self.market_refresh_interval)

    async def _status_update_loop(self) -> None:
        """Update uptime counter and periodically refresh DB stats."""
        _db_tick = 0
        while self._running:
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

            # Check HLP Z-score for alert
            if abs(hlp_stats.get("delta_zscore", 0)) > 2.0:
                asyncio.create_task(self.alerts._check_hlp_zscore(hlp_stats))

            # Persist HLP snapshots periodically
            self.store.maybe_save_hlp_snapshot()

            _db_tick += 1
            # Update persistence stats every 30 seconds
            if _db_tick % 30 == 0:
                try:
                    db_stats = self.store.get_db_stats()
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

            await asyncio.sleep(1)

    # ── Demo data generators ──────────────────────────────────────

    async def _demo_liquidation_generator(self) -> None:
        """Generate realistic mock liquidations."""
        import random
        exchanges = ["binance", "bybit", "okx", "hyperliquid"]
        weights = [0.30, 0.35, 0.20, 0.15]
        symbol_prices = {
            "BTC": 83500, "ETH": 3450, "SOL": 178, "DOGE": 0.165,
            "XRP": 0.62, "AVAX": 38, "LINK": 18.5, "ARB": 1.35,
        }
        while self._running:
            exchange = random.choices(exchanges, weights=weights, k=1)[0]
            symbol = random.choice(list(symbol_prices.keys()))
            price = symbol_prices[symbol] * (1 + random.uniform(-0.02, 0.02))

            roll = random.random()
            if roll < 0.60:
                size_usd = random.uniform(500, 10_000)
            elif roll < 0.85:
                size_usd = random.uniform(10_000, 100_000)
            elif roll < 0.97:
                size_usd = random.uniform(100_000, 500_000)
            else:
                size_usd = random.uniform(500_000, 2_000_000)

            event = LiquidationEvent(
                timestamp=time.time(),
                exchange=exchange,
                symbol=symbol,
                side=random.choice(["long", "short"]),
                size_usd=size_usd,
                price=price,
                quantity=size_usd / price if price else 0,
            )
            await self.liquidations.emit(event)
            await asyncio.sleep(random.uniform(0.3, 2.0))

    async def _demo_trade_generator(self) -> None:
        """Generate realistic mock trades."""
        import random
        prices = {"BTC": 83500.0, "ETH": 3450.0, "SOL": 178.0}

        while self._running:
            for symbol, base in prices.items():
                base += random.uniform(-base * 0.001, base * 0.001)
                prices[symbol] = base

                side = random.choices(["buy", "sell"], weights=[0.48, 0.52])[0]
                size = random.uniform(0.001, 0.5) if symbol == "BTC" else random.uniform(0.1, 50)

                trade = Trade(
                    timestamp=time.time(),
                    symbol=symbol,
                    side=side,
                    price=round(base, 2),
                    size=round(size, 6),
                    size_usd=round(size * base, 2),
                )
                self.orderflow._process_trade(trade)

            await asyncio.sleep(random.uniform(0.05, 0.15))

    async def _demo_position_scan(self) -> None:
        """Generate mock positions for demo mode."""
        import random
        positions = []
        symbol_prices = {
            "BTC": 83500, "ETH": 3450, "SOL": 178, "DOGE": 0.165,
            "XRP": 0.62, "AVAX": 38, "LINK": 18.5, "ARB": 1.35,
            "WIF": 2.80, "SUI": 3.45,
        }

        for _ in range(60):
            symbol = random.choice(list(symbol_prices.keys()))
            base_price = symbol_prices[symbol]
            current_price = base_price * (1 + random.uniform(-0.03, 0.03))
            side = random.choice(["long", "short"])
            leverage = random.choice([5, 10, 20, 25, 50, 100])
            size_usd = random.uniform(10_000, 10_000_000)

            offset = random.uniform(-3, 3)
            entry_price = current_price * (1 + offset / 100)

            mm = 0.03
            if side == "long":
                liq_price = entry_price * (1 - 1 / leverage + mm / leverage)
                pnl_pct = (current_price - entry_price) / entry_price
            else:
                liq_price = entry_price * (1 + 1 / leverage - mm / leverage)
                pnl_pct = (entry_price - current_price) / entry_price

            distance = abs(current_price - liq_price) / current_price * 100

            positions.append(TrackedPosition(
                address=f"0x{''.join(random.choices('0123456789abcdef', k=40))}",
                symbol=symbol,
                side=side,
                size_usd=size_usd,
                entry_price=entry_price,
                current_price=current_price,
                liq_price=liq_price,
                distance_pct=distance,
                leverage=leverage,
                unrealized_pnl=size_usd * pnl_pct,
                margin_used=size_usd / leverage,
            ))

        self.positions.positions = sorted(positions, key=lambda p: p.distance_pct)
        self.positions.market_prices = {s: p for s, p in symbol_prices.items()}
        self.status.tracked_positions = len(positions)

    async def _demo_smart_money(self) -> None:
        """Generate mock smart money wallets and signals for demo mode."""
        import random

        symbols = ["BTC", "ETH", "SOL", "DOGE", "XRP", "AVAX", "LINK", "ARB"]
        actions = ["OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT"]

        # Generate initial batch of wallet profiles
        for i in range(150):
            addr = f"0x{''.join(random.choices('0123456789abcdef', k=40))}"
            total_trades = random.randint(10, 500)
            winning = int(total_trades * random.uniform(0.2, 0.85))
            losing = total_trades - winning
            win_rate = winning / total_trades if total_trades else 0
            total_pnl = random.uniform(-500_000, 2_000_000) * (win_rate - 0.3)
            volume = random.uniform(100_000, 50_000_000)
            sharpe = random.uniform(-1.5, 3.0) * win_rate
            acct_value = random.uniform(1_000, 5_000_000)

            w = WalletProfile(
                address=addr,
                discovered_at=time.time() - random.uniform(0, 86400),
                last_seen=time.time() - random.uniform(0, 3600),
                last_analyzed=time.time(),
                total_trades=total_trades,
                winning_trades=winning,
                losing_trades=losing,
                total_realized_pnl=total_pnl,
                total_volume_usd=volume,
                largest_win=abs(total_pnl) * random.uniform(0.05, 0.3) if total_pnl > 0 else random.uniform(100, 50000),
                largest_loss=-abs(total_pnl) * random.uniform(0.02, 0.15) if total_pnl < 0 else -random.uniform(100, 30000),
                avg_hold_time_seconds=random.uniform(60, 86400),
                win_rate=win_rate,
                sharpe_ratio=sharpe,
                account_value=acct_value,
                open_positions=random.randint(0, 5),
                active_symbols=random.sample(symbols, k=random.randint(0, 3)),
            )
            # Compute composite + pnl_score
            w.pnl_score = self.smart_money._compute_pnl_score(w.total_realized_pnl)
            w.composite_score = self.smart_money._compute_composite(w)
            self.smart_money.wallets[addr] = w

        self.smart_money.rank_all()

        # Continuously generate signals
        while self._running:
            try:
                # Occasionally add new wallets
                if random.random() < 0.1:
                    addr = f"0x{''.join(random.choices('0123456789abcdef', k=40))}"
                    self.smart_money.wallets[addr] = WalletProfile(
                        address=addr,
                        discovered_at=time.time(),
                        last_seen=time.time(),
                        last_analyzed=0,
                        total_trades=random.randint(0, 5),
                    )

                # Generate a signal from a ranked wallet
                ranked = [w for w in self.smart_money.wallets.values()
                          if w.tier in ("smart", "dumb") and w.rank > 0]
                if ranked:
                    wallet = random.choice(ranked)
                    action = random.choice(actions)
                    symbol = random.choice(symbols)
                    size = random.uniform(5_000, 500_000)

                    signal = SmartMoneySignal(
                        timestamp=time.time(),
                        address=wallet.address,
                        tier=wallet.tier,
                        action=action,
                        symbol=symbol,
                        size_usd=size,
                        wallet_rank=wallet.rank,
                        wallet_win_rate=wallet.win_rate,
                        wallet_pnl=wallet.total_realized_pnl,
                        signal_type="follow" if wallet.tier == "smart" else "fade",
                    )
                    self.smart_money.signals.append(signal)
                    self.smart_money._emit_signal(signal)

            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Demo smart money error")

            await asyncio.sleep(random.uniform(2, 8))

    async def _demo_hlp(self) -> None:
        """Generate mock HLP vault data for demo mode."""
        import random

        symbols = ["BTC", "ETH", "SOL", "DOGE", "XRP", "AVAX", "LINK", "ARB",
                    "WIF", "PEPE", "SUI", "APT", "OP", "SEI", "TIA", "JUP",
                    "ONDO", "RENDER", "INJ", "FET"]
        symbol_prices = {
            "BTC": 83500, "ETH": 3450, "SOL": 178, "DOGE": 0.165,
            "XRP": 0.62, "AVAX": 38, "LINK": 18.5, "ARB": 1.35,
            "WIF": 2.80, "PEPE": 0.0000125, "SUI": 3.45, "APT": 11.8,
            "OP": 3.20, "SEI": 0.85, "TIA": 12.5, "JUP": 1.15,
            "ONDO": 1.45, "RENDER": 8.9, "INJ": 28.5, "FET": 2.35,
        }

        base_account_value = 210_000_000.0  # ~$210M AUM
        session_start_value = base_account_value

        while self._running:
            try:
                # Build mock positions (HLP typically has 30-80 positions)
                num_positions = random.randint(35, 65)
                positions = []
                net_delta_usd = 0.0
                total_exposure_usd = 0.0
                total_unrealized_pnl = 0.0

                for _ in range(num_positions):
                    sym = random.choice(symbols)
                    base_price = symbol_prices[sym]
                    current_price = base_price * (1 + random.uniform(-0.02, 0.02))

                    # HLP tends to have large positions — range from $50K to $20M
                    size_usd = random.uniform(50_000, 20_000_000)
                    side = random.choice(["long", "short"])
                    size = size_usd / current_price
                    if side == "short":
                        size = -size

                    entry_offset = random.uniform(-0.005, 0.005)
                    entry_price = current_price * (1 + entry_offset)

                    if side == "long":
                        unrealized_pnl = (current_price - entry_price) / entry_price * size_usd
                    else:
                        unrealized_pnl = (entry_price - current_price) / entry_price * size_usd

                    leverage = random.choice([3, 5, 10, 20])

                    positions.append(HLPPosition(
                        symbol=sym,
                        side=side,
                        size=size,
                        size_usd=size_usd,
                        entry_price=entry_price,
                        current_price=current_price,
                        unrealized_pnl=unrealized_pnl,
                        leverage=leverage,
                    ))

                    signed_value = size_usd if side == "long" else -size_usd
                    net_delta_usd += signed_value
                    total_exposure_usd += size_usd
                    total_unrealized_pnl += unrealized_pnl

                # Drift account value slightly
                base_account_value += random.uniform(-50_000, 80_000)
                total_margin = total_exposure_usd * 0.1  # ~10x effective

                snapshot = HLPSnapshot(
                    timestamp=time.time(),
                    account_value=base_account_value,
                    total_margin_used=total_margin,
                    positions=positions,
                    net_delta_usd=net_delta_usd,
                    total_exposure_usd=total_exposure_usd,
                    num_positions=num_positions,
                    total_unrealized_pnl=total_unrealized_pnl,
                    session_pnl=base_account_value - session_start_value,
                )
                snapshot.delta_zscore = self.hlp._compute_delta_zscore(net_delta_usd)
                self.hlp.snapshots.append(snapshot)

                if self.hlp._session_start_value == 0:
                    self.hlp._session_start_value = base_account_value

                # Generate a few mock fills per cycle
                num_fills = random.randint(1, 5)
                for _ in range(num_fills):
                    sym = random.choice(symbols)
                    price = symbol_prices[sym] * (1 + random.uniform(-0.01, 0.01))
                    size = random.uniform(100, 50_000) / price
                    side = random.choice(["buy", "sell"])
                    directions = ["Open Long", "Open Short", "Close Long", "Close Short"]
                    direction = random.choice(directions)

                    # ~20% of opens are liquidation absorptions
                    is_liq = direction.startswith("Open") and random.random() < 0.2
                    closed_pnl = 0.0
                    if direction.startswith("Close"):
                        closed_pnl = random.uniform(-5_000, 10_000)

                    trade = HLPTrade(
                        timestamp=time.time(),
                        symbol=sym,
                        side=side,
                        price=price,
                        size=size,
                        size_usd=price * size,
                        direction=direction,
                        closed_pnl=closed_pnl,
                        is_liquidation=is_liq,
                    )
                    self.hlp.trades.append(trade)
                    for cb in self.hlp._callbacks:
                        try:
                            cb(trade)
                        except Exception:
                            logger.exception("Demo HLP trade callback error")

            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Demo HLP error")

            await asyncio.sleep(random.uniform(1.0, 3.0))

    async def _demo_market_refresh(self) -> None:
        """Generate mock market data for demo mode."""
        import random
        from src.data_layer.market_data import AssetInfo

        mock_assets = [
            ("BTC", 83500, 500), ("ETH", 3450, 50), ("SOL", 178, 5),
            ("DOGE", 0.165, 0.005), ("XRP", 0.62, 0.02), ("AVAX", 38, 1.5),
            ("LINK", 18.5, 0.5), ("ARB", 1.35, 0.1), ("WIF", 2.80, 0.15),
            ("PEPE", 0.0000125, 0.000002), ("SUI", 3.45, 0.2), ("APT", 11.8, 0.5),
            ("OP", 3.20, 0.15), ("SEI", 0.85, 0.05), ("TIA", 12.5, 0.8),
            ("JUP", 1.15, 0.08), ("ONDO", 1.45, 0.1), ("RENDER", 8.9, 0.4),
            ("INJ", 28.5, 1.5), ("FET", 2.35, 0.15),
        ]

        assets = {}
        for sym, base, jitter in mock_assets:
            price = base + random.uniform(-jitter, jitter)
            funding = random.uniform(-0.0005, 0.0005)
            if random.random() < 0.15:
                funding = random.choice([-1, 1]) * random.uniform(0.001, 0.005)

            mark = price * (1 + random.uniform(-0.001, 0.001))
            index = price * (1 + random.uniform(-0.002, 0.002))
            premium = (mark - index) / index * 100 if index > 0 else 0.0
            assets[sym] = AssetInfo(
                symbol=sym,
                price=price,
                funding_rate=funding,
                open_interest=random.uniform(5e6, 2e9),
                volume_24h=random.uniform(1e7, 5e9),
                price_change_24h_pct=random.uniform(-0.08, 0.08),
                mark_price=mark,
                index_price=index,
                premium_pct=premium,
            )

        self.market.assets = assets
        self.market.last_update = time.monotonic()

    async def _demo_deribit(self) -> None:
        """Generate synthetic Deribit DVOL data for demo mode."""
        import random
        from src.data_layer.deribit import DeribitIVSnapshot

        btc_iv = 55.0
        eth_iv = 75.0
        while self._running:
            try:
                btc_iv += random.uniform(-0.8, 0.8)
                btc_iv = max(45.0, min(65.0, btc_iv))
                eth_iv += random.uniform(-1.2, 1.2)
                eth_iv = max(60.0, min(90.0, eth_iv))

                now = time.time()
                self.deribit.snapshots["BTC"] = DeribitIVSnapshot(
                    timestamp=now, underlying="BTC", mark_iv=btc_iv,
                    bid_iv=0.0, ask_iv=0.0, oi_usd=0.0,
                    index_price=self.market.assets.get("BTC", None) and self.market.assets["BTC"].price or 83500.0,
                )
                self.deribit.snapshots["ETH"] = DeribitIVSnapshot(
                    timestamp=now, underlying="ETH", mark_iv=eth_iv,
                    bid_iv=0.0, ask_iv=0.0, oi_usd=0.0,
                    index_price=self.market.assets.get("ETH", None) and self.market.assets["ETH"].price or 3450.0,
                )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Demo Deribit error")
            await asyncio.sleep(random.uniform(2.0, 5.0))

    async def _demo_basis(self) -> None:
        """Generate synthetic spot/perp basis data for demo mode."""
        import random
        from src.data_layer.spot_prices import SpotPriceSnapshot

        bases = {"BTC": 0.05, "ETH": 0.03, "SOL": 0.08}
        while self._running:
            try:
                now = time.time()
                for sym in ["BTC", "ETH", "SOL"]:
                    bases[sym] += random.uniform(-0.02, 0.02)
                    bases[sym] = max(-0.05, min(0.15, bases[sym]))
                    # Occasional spike
                    if random.random() < 0.05:
                        bases[sym] += random.choice([-1, 1]) * random.uniform(0.05, 0.1)
                        bases[sym] = max(-0.15, min(0.25, bases[sym]))

                    asset = self.market.assets.get(sym)
                    perp_price = asset.price if asset else {"BTC": 83500, "ETH": 3450, "SOL": 178}[sym]
                    spot_price = perp_price / (1 + bases[sym] / 100)
                    self.spot.prices[sym] = SpotPriceSnapshot(
                        timestamp=now, symbol=sym,
                        spot_price=spot_price, perp_price=perp_price,
                        basis_pct=bases[sym],
                    )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Demo basis error")
            await asyncio.sleep(random.uniform(3.0, 6.0))

    async def _demo_lsr(self) -> None:
        """Generate synthetic long/short ratio data for demo mode."""
        import random
        from src.data_layer.long_short_ratio import LongShortSnapshot

        ratios = {"BTC": 1.1, "ETH": 0.95, "SOL": 1.2}
        while self._running:
            try:
                now = time.time()
                for sym in ["BTC", "ETH", "SOL"]:
                    ratios[sym] += random.uniform(-0.03, 0.03)
                    ratios[sym] = max(0.8, min(1.4, ratios[sym]))
                    ls = ratios[sym]
                    long_r = ls / (1 + ls)
                    short_r = 1.0 - long_r
                    self.lsr.ratios[sym] = LongShortSnapshot(
                        timestamp=now, symbol=sym,
                        long_ratio=long_r, short_ratio=short_r,
                        long_short_ratio=ls,
                    )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Demo LSR error")
            await asyncio.sleep(random.uniform(5.0, 10.0))

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

