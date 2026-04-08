"""
Binance spot price collector with basis calculation.

Polls Binance spot ticker prices for BTC, ETH, SOL every 5 seconds.
Computes basis (perp - spot) / spot using market data perp prices.

Usage:
    collector = SpotPriceCollector()
    await collector.start(perp_price_fn=hub.market.assets.get)
    snap = collector.get_latest("BTC")  # SpotPriceSnapshot or None
    await collector.stop()
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

BINANCE_SPOT_URL = "https://api.binance.com/api/v3/ticker/price"
POLL_INTERVAL = 5.0
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL"]

SYMBOL_TO_BINANCE: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}


@dataclass
class SpotPriceSnapshot:
    timestamp: float
    symbol: str
    spot_price: float
    perp_price: float   # From hub market data (0.0 if unavailable)
    basis_pct: float    # (perp - spot) / spot * 100


class SpotPriceCollector:
    """Polls Binance spot prices every POLL_INTERVAL and computes basis."""

    def __init__(self, symbols: list[str] | None = None) -> None:
        self.symbols = symbols or list(DEFAULT_SYMBOLS)
        self.prices: dict[str, SpotPriceSnapshot] = {}
        self._get_perp_price = None  # Injected by hub: callable(symbol) -> AssetInfo | None
        self._task: asyncio.Task | None = None
        self._running = False

    # ── Lifecycle ────────────────────────────────────────────────

    async def start(self, perp_price_fn=None) -> None:
        """Start polling. Optionally inject a function to get perp prices."""
        if self._running:
            return
        self._get_perp_price = perp_price_fn
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="spot-price-poll")
        logger.info("SpotPriceCollector started for %s", self.symbols)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── Public API ───────────────────────────────────────────────

    def get_latest(self, symbol: str) -> SpotPriceSnapshot | None:
        return self.prices.get(symbol.upper())

    # ── Parsing (public for testability) ────────────────────────

    def _parse_response(self, data: list[dict], perp_prices: dict[str, float]) -> None:
        now = time.time()
        binance_to_sym = {v: k for k, v in SYMBOL_TO_BINANCE.items()}
        for item in data:
            try:
                binance_sym = item["symbol"]
                symbol = binance_to_sym.get(binance_sym)
                if not symbol:
                    continue
                spot = float(item["price"])
                perp = perp_prices.get(symbol, 0.0)
                basis = (perp - spot) / spot * 100 if spot > 0 and perp > 0 else 0.0
                self.prices[symbol] = SpotPriceSnapshot(
                    timestamp=now,
                    symbol=symbol,
                    spot_price=spot,
                    perp_price=perp,
                    basis_pct=basis,
                )
            except (KeyError, ValueError, TypeError):
                continue

    # ── Poll loop ────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                async with aiohttp.ClientSession() as session:
                    await self._fetch(session)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("SpotPriceCollector poll error")
            await asyncio.sleep(POLL_INTERVAL)

    async def _fetch(self, session: aiohttp.ClientSession) -> None:
        fetchable = [s for s in self.symbols if s in SYMBOL_TO_BINANCE]
        if not fetchable:
            return
        symbols_param = '["' + '","'.join(SYMBOL_TO_BINANCE[s] for s in fetchable) + '"]'
        async with session.get(
            BINANCE_SPOT_URL,
            params={"symbols": symbols_param},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        # Get current perp prices from hub market data
        perp_prices: dict[str, float] = {}
        if self._get_perp_price:
            for sym in self.symbols:
                asset = self._get_perp_price(sym)
                if asset is not None:
                    perp_prices[sym] = float(asset.price) if hasattr(asset, "price") else float(asset)

        if isinstance(data, list):
            self._parse_response(data, perp_prices)
