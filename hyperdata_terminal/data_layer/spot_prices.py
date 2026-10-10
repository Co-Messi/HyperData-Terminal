"""
Spot price collector with basis calculation.

Polls spot prices for BTC, ETH, SOL every 5 seconds and computes basis
(perp - spot) / spot against the hub's Hyperliquid perp prices.

The perp leg is Hyperliquid, quoted in USD (USDC). The spot leg must be in
USD too: the USDT/USD premium (often 0.05% to 0.1%) is the same size as the
basis being measured. Coinbase (USD) is the primary source; Binance and OKX
(USDT) are fallbacks whose prices are converted to USD with a live USDT/USD
rate (Coinbase USDT-USD, else Kraken USDTZUSD, refreshed every minute). With
no fresh rate a USDT source is not used at all. Every snapshot records its
venue, its quote and the rate applied. A source that fails is skipped for a
while instead of being retried every poll: 10 minutes when it is geoblocked
(401/403/451), 30 seconds after a transient failure.

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
KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker"
USDT_RATE_REFRESH_SECONDS = 60.0
USDT_RATE_MAX_AGE_SECONDS = 300.0
POLL_INTERVAL = 5.0
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL"]
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=8)

SYMBOL_TO_BINANCE: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}

# Order of preference; the first source that prices anything wins the poll.
SPOT_SOURCES = ("coinbase", "binance", "okx")
SOURCE_QUOTE = {"coinbase": "USD", "binance": "USDT", "okx": "USDT"}


@dataclass
class SpotPriceSnapshot:
    timestamp: float
    symbol: str
    spot_price: float
    perp_price: float   # From hub market data (0.0 if unavailable)
    basis_pct: float    # (perp - spot) / spot * 100, both in USD
    source: str = "coinbase"  # venue that priced the spot leg
    quote: str = "USD"        # the venue's quote currency
    native_price: float = 0.0  # spot price in that quote currency
    usdt_usd: float | None = None  # rate applied to a USDT price (None for USD venues)


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
        self.usdt_usd: float | None = None
        self.usdt_usd_at: float = 0.0
        self._usdt_attempt_at: float = 0.0

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

    def set_usdt_usd(self, rate: float, at: float | None = None) -> None:
        if rate > 0:
            self.usdt_usd = float(rate)
            self.usdt_usd_at = time.time() if at is None else at

    def fresh_usdt_usd(self, now: float | None = None) -> float | None:
        now = time.time() if now is None else now
        if self.usdt_usd and now - self.usdt_usd_at <= USDT_RATE_MAX_AGE_SECONDS:
            return self.usdt_usd
        return None

    def _store(self, spot_prices: dict[str, float], perp_prices: dict[str, float], source: str) -> bool:
        """Store one source's prices in USD; False if a USDT source has no fresh rate."""
        # Spot tickers carry no reliable event time across these venues, so
        # the stamp is local fetch time by necessity, not exchange time.
        now = time.time()
        quote = SOURCE_QUOTE.get(source, "USD")
        rate = None
        if quote == "USDT":
            rate = self.fresh_usdt_usd(now)
            if rate is None:
                return False
        for symbol, native in spot_prices.items():
            if native <= 0:
                continue
            spot = native * rate if rate else native
            perp = perp_prices.get(symbol, 0.0)
            basis = (perp - spot) / spot * 100 if perp > 0 else 0.0
            self.prices[symbol] = SpotPriceSnapshot(
                timestamp=now,
                symbol=symbol,
                spot_price=spot,
                perp_price=perp,
                basis_pct=basis,
                source=source,
                quote=quote,
                native_price=native,
                usdt_usd=rate,
            )
        return True

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

    async def _fetch_usdt_usd(self, session: aiohttp.ClientSession) -> None:
        """Refresh the USDT/USD rate (Coinbase, else Kraken) once a minute."""
        now = time.time()
        if now - max(self.usdt_usd_at, self._usdt_attempt_at) < USDT_RATE_REFRESH_SECONDS:
            return
        self._usdt_attempt_at = now
        try:
            url = COINBASE_TICKER_URL.format(product="USDT-USD")
            async with session.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": "hyperdata-terminal"}) as resp:
                resp.raise_for_status()
                self.set_usdt_usd(float((await resp.json())["price"]))
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("USDT/USD from Coinbase unavailable (%s); trying Kraken", exc)
        try:
            async with session.get(KRAKEN_TICKER_URL, params={"pair": "USDTZUSD"}, timeout=HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                body = await resp.json()
            result = body.get("result") or {}
            ticker = next(iter(result.values())) if result else {}
            self.set_usdt_usd(float(ticker["c"][0]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("USDT/USD from Kraken unavailable (%s); USDT spot sources are skipped", exc)

    async def _fetch(self, session: aiohttp.ClientSession) -> None:
        fetchable = [s for s in self.symbols if s in SYMBOL_TO_BINANCE]
        if not fetchable:
            return
        await self._fetch_usdt_usd(session)
        now = time.time()
        for source in SPOT_SOURCES:
            if self._source_down_until.get(source, 0.0) > now:
                continue
            if SOURCE_QUOTE.get(source) == "USDT" and self.fresh_usdt_usd(now) is None:
                continue  # cannot convert to USD: not comparable with the USD perp
            try:
                spot = await self._fetch_source(session, source, fetchable)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                cooldown = source_cooldown_seconds(exc)
                self._source_down_until[source] = now + cooldown
                logger.info("spot source %s unavailable (%s); skipping it for %.0fs", source, exc, cooldown)
                continue
            if spot and self._store(spot, self._perp_prices(), source):
                if self.active_source != source:
                    logger.info("spot prices now from %s", source)
                self.active_source = source
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
