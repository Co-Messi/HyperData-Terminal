"""
Binance long/short ratio collector.

Polls Binance futures globalLongShortAccountRatio for BTC, ETH, SOL every 30 seconds.
Stores as LongShortSnapshot accessible via hub.long_short_ratios[symbol].
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

BINANCE_LSR_URL = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
POLL_INTERVAL = 30.0
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL"]


@dataclass
class LongShortSnapshot:
    timestamp: float
    symbol: str
    long_ratio: float    # 0.0–1.0
    short_ratio: float   # 0.0–1.0
    long_short_ratio: float  # long_ratio / short_ratio


class LongShortCollector:
    """Polls Binance L/S ratio every POLL_INTERVAL seconds."""

    def __init__(self, symbols: list[str] | None = None) -> None:
        self.symbols = symbols or list(DEFAULT_SYMBOLS)
        self.ratios: dict[str, LongShortSnapshot] = {}
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False

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

    def _parse_response(self, symbol: str, data: list[dict]) -> None:
        if not data:
            return
        try:
            item = data[0]
            long_r = float(item["longAccount"])
            short_r = float(item["shortAccount"])
            lsr = long_r / short_r if short_r > 0 else 0.0
            self.ratios[symbol] = LongShortSnapshot(
                timestamp=int(item.get("timestamp", time.time() * 1000)) / 1000.0,
                symbol=symbol,
                long_ratio=long_r,
                short_ratio=short_r,
                long_short_ratio=lsr,
            )
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            logger.debug("Failed to parse LSR response for %s", symbol)

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                if self._session and not self._session.closed:
                    tasks = [self._fetch_symbol(sym) for sym in self.symbols]
                    await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("LongShortCollector poll error")
            await asyncio.sleep(POLL_INTERVAL)

    async def _fetch_symbol(self, symbol: str) -> None:
        if not self._session or self._session.closed:
            return
        params = {"symbol": f"{symbol}USDT", "period": "5m", "limit": "1"}
        async with self._session.get(BINANCE_LSR_URL, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if isinstance(data, list):
            self._parse_response(symbol, data)
