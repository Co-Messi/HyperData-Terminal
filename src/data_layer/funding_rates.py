"""
Multi-exchange funding rate collector.

Polls Binance and Bybit every 5 seconds for funding rates across all symbols.
Stores per-exchange per-symbol snapshots accessible via hub.funding_rates.

Usage:
    collector = FundingRateCollector()
    await collector.start()
    snap = collector.get_latest("binance", "BTC")
    await collector.stop()
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

BINANCE_PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"
POLL_INTERVAL = 5.0


def normalise_fr_symbol(raw: str) -> str:
    """Strip common exchange suffixes to return a bare symbol like 'BTC'."""
    raw = raw.upper()
    for suffix in ("USDT", "USD", "PERP", "BUSD"):
        if raw.endswith(suffix):
            return raw[: -len(suffix)]
    return raw


@dataclass
class FundingRateSnapshot:
    timestamp: float
    exchange: str
    symbol: str
    funding_rate_hourly: float
    funding_rate_annualized: float  # hourly * 8760


class FundingRateCollector:
    """Polls Binance and Bybit funding rates every POLL_INTERVAL seconds."""

    def __init__(self) -> None:
        # rates[exchange][symbol] = FundingRateSnapshot
        self.rates: dict[str, dict[str, FundingRateSnapshot]] = {
            "binance": {},
            "bybit": {},
        }
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._poll_loop(), name="funding-rate-poll")
        logger.info("FundingRateCollector started")

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
        logger.info("FundingRateCollector stopped")

    def get_latest(self, exchange: str, symbol: str) -> FundingRateSnapshot | None:
        return self.rates.get(exchange, {}).get(symbol.upper())

    def get_all_for_symbol(self, symbol: str) -> list[FundingRateSnapshot]:
        symbol = symbol.upper()
        result = []
        for ex_rates in self.rates.values():
            if symbol in ex_rates:
                result.append(ex_rates[symbol])
        return result

    def get_all_rates(self) -> dict[str, dict[str, FundingRateSnapshot]]:
        return self.rates

    def _parse_binance(self, data: list[dict]) -> None:
        now = time.time()
        for item in data:
            try:
                symbol = normalise_fr_symbol(item["symbol"])
                if not symbol:
                    continue
                rate = float(item["lastFundingRate"])
                hourly = rate / 8.0  # Binance rate is per 8h interval
                self.rates["binance"][symbol] = FundingRateSnapshot(
                    timestamp=now,
                    exchange="binance",
                    symbol=symbol,
                    funding_rate_hourly=hourly,
                    funding_rate_annualized=hourly * 8760,
                )
            except (KeyError, ValueError, TypeError):
                continue

    def _parse_bybit(self, data: dict) -> None:
        now = time.time()
        items = data.get("result", {}).get("list", [])
        for item in items:
            try:
                symbol = normalise_fr_symbol(item["symbol"])
                if not symbol:
                    continue
                rate = float(item["fundingRate"])
                hourly = rate / 8.0  # Bybit rate is per 8h interval
                self.rates["bybit"][symbol] = FundingRateSnapshot(
                    timestamp=now,
                    exchange="bybit",
                    symbol=symbol,
                    funding_rate_hourly=hourly,
                    funding_rate_annualized=hourly * 8760,
                )
            except (KeyError, ValueError, TypeError):
                continue

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                if self._session and not self._session.closed:
                    await asyncio.gather(
                        self._fetch_binance(self._session),
                        self._fetch_bybit(self._session),
                        return_exceptions=True,
                    )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("FundingRateCollector poll error")
            await asyncio.sleep(POLL_INTERVAL)

    async def _fetch_binance(self, session: aiohttp.ClientSession) -> None:
        async with session.get(BINANCE_PREMIUM_INDEX_URL, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if isinstance(data, list):
            self._parse_binance(data)
            logger.debug("Binance funding: %d symbols updated", len(self.rates["binance"]))

    async def _fetch_bybit(self, session: aiohttp.ClientSession) -> None:
        async with session.get(
            BYBIT_TICKERS_URL,
            params={"category": "linear"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        self._parse_bybit(data)
        logger.debug("Bybit funding: %d symbols updated", len(self.rates["bybit"]))
