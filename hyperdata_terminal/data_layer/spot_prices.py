"""
Spot price collector with basis calculation.

Polls spot prices for BTC, ETH, SOL every 5 seconds and computes basis
(perp - spot) / spot against the hub's Hyperliquid perp prices.

Binance spot is the primary source, but it answers HTTP 451 in restricted
regions (the US among them). The collector then falls back to Coinbase
(USD) and OKX (USDT), and records which venue priced each snapshot so a
USD vs USDT spot is never passed off as the same thing. A source that fails
is skipped for a while instead of being retried every poll: 10 minutes when
it is geoblocked (401/403/451), 30 seconds after a transient failure.

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

from hyperdata_terminal.utils.helpers import source_cooldown_seconds

logger = logging.getLogger(__name__)

BINANCE_SPOT_URL = "https://api.binance.com/api/v3/ticker/price"
COINBASE_TICKER_URL = "https://api.exchange.coinbase.com/products/{product}/ticker"
OKX_TICKER_URL = "https://www.okx.com/api/v5/market/ticker"
POLL_INTERVAL = 5.0
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL"]
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=8)

SYMBOL_TO_BINANCE: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}

# Order of preference; the first source that prices anything wins the poll.
SPOT_SOURCES = ("binance", "coinbase", "okx")


@dataclass
class SpotPriceSnapshot:
    timestamp: float
    symbol: str
    spot_price: float
    perp_price: float   # From hub market data (0.0 if unavailable)
    basis_pct: float    # (perp - spot) / spot * 100
    source: str = "binance"  # venue that priced the spot leg


class SpotPriceCollector:
    """Polls spot prices every POLL_INTERVAL and computes basis."""

    def __init__(self, symbols: list[str] | None = None) -> None:
        self.symbols = symbols or list(DEFAULT_SYMBOLS)
        self.prices: dict[str, SpotPriceSnapshot] = {}
        self._get_perp_price = None  # Injected by hub: callable(symbol) -> AssetInfo | None
        self._task: asyncio.Task | None = None
        self._running = False
        self._source_down_until: dict[str, float] = {}
        self.active_source: str | None = None

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

    def _store(self, spot_prices: dict[str, float], perp_prices: dict[str, float], source: str) -> None:
        # Spot tickers carry no reliable event time across these venues, so
        # the stamp is local fetch time by necessity, not exchange time.
        now = time.time()
        for symbol, spot in spot_prices.items():
            if spot <= 0:
                continue
            perp = perp_prices.get(symbol, 0.0)
            basis = (perp - spot) / spot * 100 if perp > 0 else 0.0
            self.prices[symbol] = SpotPriceSnapshot(
                timestamp=now,
                symbol=symbol,
                spot_price=spot,
                perp_price=perp,
                basis_pct=basis,
                source=source,
            )

    def _parse_response(self, data: list[dict], perp_prices: dict[str, float]) -> None:
        """Parse Binance's /api/v3/ticker/price ({symbol, price} list)."""
        binance_to_sym = {v: k for k, v in SYMBOL_TO_BINANCE.items()}
        spot: dict[str, float] = {}
        for item in data:
            try:
                symbol = binance_to_sym.get(item["symbol"])
                if symbol:
                    spot[symbol] = float(item["price"])
            except (KeyError, ValueError, TypeError):
                continue
        self._store(spot, perp_prices, "binance")

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

    def _perp_prices(self) -> dict[str, float]:
        perp_prices: dict[str, float] = {}
        if self._get_perp_price:
            for sym in self.symbols:
                asset = self._get_perp_price(sym)
                if asset is not None:
                    perp_prices[sym] = float(asset.price) if hasattr(asset, "price") else float(asset)
        return perp_prices

    async def _fetch(self, session: aiohttp.ClientSession) -> None:
        fetchable = [s for s in self.symbols if s in SYMBOL_TO_BINANCE]
        if not fetchable:
            return
        now = time.time()
        for source in SPOT_SOURCES:
            if self._source_down_until.get(source, 0.0) > now:
                continue
            try:
                spot = await self._fetch_source(session, source, fetchable)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                cooldown = source_cooldown_seconds(exc)
                self._source_down_until[source] = now + cooldown
                logger.info("spot source %s unavailable (%s); skipping it for %.0fs", source, exc, cooldown)
                continue
            if spot:
                if self.active_source != source:
                    logger.info("spot prices now from %s", source)
                self.active_source = source
                self._store(spot, self._perp_prices(), source)
                return
        self.active_source = None

    async def _fetch_source(
        self, session: aiohttp.ClientSession, source: str, symbols: list[str],
    ) -> dict[str, float]:
        if source == "binance":
            symbols_param = '["' + '","'.join(SYMBOL_TO_BINANCE[s] for s in symbols) + '"]'
            async with session.get(BINANCE_SPOT_URL, params={"symbols": symbols_param}, timeout=HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                data = await resp.json()
            binance_to_sym = {v: k for k, v in SYMBOL_TO_BINANCE.items()}
            return {
                binance_to_sym[item["symbol"]]: float(item["price"])
                for item in (data if isinstance(data, list) else [])
                if item.get("symbol") in binance_to_sym
            }
        if source == "coinbase":
            out: dict[str, float] = {}
            for sym in symbols:
                url = COINBASE_TICKER_URL.format(product=f"{sym}-USD")
                async with session.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": "hyperdata-terminal"}) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                out[sym] = float(data["price"])
            return out
        if source == "okx":
            out = {}
            for sym in symbols:
                async with session.get(OKX_TICKER_URL, params={"instId": f"{sym}-USDT"}, timeout=HTTP_TIMEOUT) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                rows = data.get("data") or []
                if rows:
                    out[sym] = float(rows[0]["last"])
            return out
        raise ValueError(f"unknown spot source {source!r}")
