"""
Long/short account ratio collector.

Polls the account long/short ratio for BTC, ETH, SOL every 30 seconds and
stores it as a LongShortSnapshot, accessible via hub.lsr.get_latest(symbol).

Binance futures (globalLongShortAccountRatio) is the primary source but is
geoblocked in several regions (HTTP 451). The collector then falls back to
Bybit's account-ratio and OKX's long-short-account-ratio-contract. All three
are ratios of accounts net long vs net short on that venue, so they measure
the same thing on different crowds; ``source`` says which crowd you are
looking at. A failing source is skipped for SOURCE_COOLDOWN seconds.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

BINANCE_LSR_URL = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
BYBIT_LSR_URL = "https://api.bybit.com/v5/market/account-ratio"
OKX_LSR_URL = "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio-contract"
POLL_INTERVAL = 30.0
SOURCE_COOLDOWN = 600.0
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL"]
LSR_SOURCES = ("binance", "bybit", "okx")
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=8)


@dataclass
class LongShortSnapshot:
    timestamp: float
    symbol: str
    long_ratio: float    # 0.0–1.0
    short_ratio: float   # 0.0–1.0
    long_short_ratio: float  # long_ratio / short_ratio
    source: str = "binance"  # venue whose accounts were counted


class LongShortCollector:
    """Polls the L/S account ratio every POLL_INTERVAL seconds."""

    def __init__(self, symbols: list[str] | None = None) -> None:
        self.symbols = symbols or list(DEFAULT_SYMBOLS)
        self.ratios: dict[str, LongShortSnapshot] = {}
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._source_down_until: dict[str, float] = {}
        self.active_source: str | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._poll_loop(), name="lsr-poll")
        logger.info("LongShortCollector started for %s", self.symbols)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()

    def get_latest(self, symbol: str) -> LongShortSnapshot | None:
        return self.ratios.get(symbol.upper())

    def _store(self, symbol: str, long_r: float, short_r: float, ts: float, source: str) -> None:
        if long_r <= 0 and short_r <= 0:
            return
        self.ratios[symbol] = LongShortSnapshot(
            timestamp=ts,
            symbol=symbol,
            long_ratio=long_r,
            short_ratio=short_r,
            long_short_ratio=long_r / short_r if short_r > 0 else 0.0,
            source=source,
        )

    def _parse_response(self, symbol: str, data: list[dict]) -> None:
        """Parse Binance's globalLongShortAccountRatio list."""
        if not data:
            return
        try:
            item = data[0]
            self._store(
                symbol,
                float(item["longAccount"]),
                float(item["shortAccount"]),
                int(item.get("timestamp", time.time() * 1000)) / 1000.0,
                "binance",
            )
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            logger.debug("Failed to parse LSR response for %s", symbol)

    def _parse_bybit(self, symbol: str, data: dict) -> None:
        rows = (data.get("result") or {}).get("list") or []
        if not rows:
            return
        item = rows[0]
        self._store(
            symbol, float(item["buyRatio"]), float(item["sellRatio"]),
            int(item.get("timestamp", time.time() * 1000)) / 1000.0, "bybit",
        )

    def _parse_okx(self, symbol: str, data: dict) -> None:
        rows = data.get("data") or []
        if not rows:
            return
        ts_ms, ratio = rows[0][0], float(rows[0][1])
        if ratio <= 0:
            return
        # OKX reports only long/short; recover the shares that sum to 1.
        self._store(symbol, ratio / (1 + ratio), 1 / (1 + ratio), int(ts_ms) / 1000.0, "okx")

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                if self._session and not self._session.closed:
                    await self._poll_once()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("LongShortCollector poll error")
            await asyncio.sleep(POLL_INTERVAL)

    async def _poll_once(self) -> None:
        now = time.time()
        for source in LSR_SOURCES:
            if self._source_down_until.get(source, 0.0) > now:
                continue
            results = await asyncio.gather(
                *(self._fetch_symbol(sym, source) for sym in self.symbols), return_exceptions=True,
            )
            if all(isinstance(r, BaseException) for r in results):
                self._source_down_until[source] = now + SOURCE_COOLDOWN
                logger.info("L/S source %s unavailable (%s); trying the next one", source, results[0])
                continue
            if self.active_source != source:
                logger.info("L/S ratios now from %s", source)
            self.active_source = source
            return
        self.active_source = None

    async def _fetch_symbol(self, symbol: str, source: str = "binance") -> None:
        if not self._session or self._session.closed:
            return
        if source == "binance":
            params = {"symbol": f"{symbol}USDT", "period": "5m", "limit": "1"}
            url = BINANCE_LSR_URL
        elif source == "bybit":
            params = {"category": "linear", "symbol": f"{symbol}USDT", "period": "5min", "limit": "1"}
            url = BYBIT_LSR_URL
        elif source == "okx":
            params = {"instId": f"{symbol}-USDT-SWAP", "period": "5m", "limit": "1"}
            url = OKX_LSR_URL
        else:
            raise ValueError(f"unknown L/S source {source!r}")
        async with self._session.get(url, params=params, timeout=HTTP_TIMEOUT) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if source == "binance" and isinstance(data, list):
            self._parse_response(symbol, data)
        elif source == "bybit" and isinstance(data, dict):
            self._parse_bybit(symbol, data)
        elif source == "okx" and isinstance(data, dict):
            self._parse_okx(symbol, data)
