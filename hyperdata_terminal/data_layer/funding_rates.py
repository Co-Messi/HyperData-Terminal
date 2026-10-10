"""
Multi-exchange funding rate collector.

Polls Binance and Bybit every 5 seconds for funding rates across all symbols.
Stores per-exchange per-symbol snapshots accessible via hub.funding_rates.

Both venues set the funding interval per symbol (8h for most, 4h or 1h for
many alts, and Binance changes it in volatile markets), so every rate is
divided by that symbol's own interval:

- Binance: ``/fapi/v1/fundingInfo`` lists the symbols whose interval (or
  cap/floor) was adjusted; every other symbol uses the standard 8h.
  Refreshed hourly. Until it has loaded once, Binance rates are held back
  rather than annualized with a guessed interval.
- Bybit: each tickers row carries ``fundingIntervalHour``; if a row lacks
  it, ``instruments-info`` ``fundingInterval`` (minutes) is used. A row
  with neither is skipped.

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
BINANCE_FUNDING_INFO_URL = "https://fapi.binance.com/fapi/v1/fundingInfo"
BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"
BYBIT_INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info"
POLL_INTERVAL = 5.0
# Funding intervals change rarely; re-read them hourly, or every minute
# until the first successful read.
INTERVAL_REFRESH_SECONDS = 3600.0
INTERVAL_RETRY_SECONDS = 60.0
# Binance's standard interval for a symbol fundingInfo does not list.
BINANCE_DEFAULT_INTERVAL_HOURS = 8.0
HOURS_PER_YEAR = 8760


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
    # Hours between this symbol's funding payments on this venue, and the
    # rate it pays per payment (funding_rate_hourly = rate_per_interval /
    # interval_hours).
    interval_hours: float | None = None
    rate_per_interval: float | None = None


def _snapshot(ts: float, exchange: str, symbol: str, rate: float, interval_hours: float) -> FundingRateSnapshot:
    hourly = rate / interval_hours
    return FundingRateSnapshot(
        timestamp=ts,
        exchange=exchange,
        symbol=symbol,
        funding_rate_hourly=hourly,
        funding_rate_annualized=hourly * HOURS_PER_YEAR,
        interval_hours=interval_hours,
        rate_per_interval=rate,
    )


class FundingRateCollector:
    """Polls Binance and Bybit funding rates every POLL_INTERVAL seconds."""

    def __init__(self) -> None:
        # rates[exchange][symbol] = FundingRateSnapshot
        self.rates: dict[str, dict[str, FundingRateSnapshot]] = {
            "binance": {},
            "bybit": {},
        }
        # Raw venue symbol -> funding interval in hours. Binance's dict holds
        # only the adjusted symbols (fundingInfo); None = never loaded.
        self.binance_intervals: dict[str, float] | None = None
        self.bybit_intervals: dict[str, float] = {}
        self._binance_intervals_at = 0.0
        self._binance_intervals_attempt_at = 0.0
        self._bybit_intervals_attempt_at = 0.0
        # Rows held back because their interval is unknown (cumulative).
        self.binance_unknown_interval_rows = 0
        self.bybit_unknown_interval_rows = 0
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

    # ── Intervals ────────────────────────────────────────────────

    def _apply_binance_intervals(self, data) -> None:
        """Load a fundingInfo response; a failed or malformed one keeps the last known."""
        if not isinstance(data, list):
            return
        intervals: dict[str, float] = {}
        for item in data:
            try:
                hours = float(item["fundingIntervalHours"])
                if hours > 0:
                    intervals[str(item["symbol"]).upper()] = hours
            except (KeyError, TypeError, ValueError):
                continue
        self.binance_intervals = intervals
        self._binance_intervals_at = time.time()

    def _apply_bybit_intervals(self, data) -> None:
        """Merge an instruments-info page (fundingInterval is in minutes)."""
        items = (data or {}).get("result", {}).get("list", []) if isinstance(data, dict) else []
        for item in items:
            try:
                minutes = float(item["fundingInterval"])
                if minutes > 0:
                    self.bybit_intervals[str(item["symbol"]).upper()] = minutes / 60.0
            except (KeyError, TypeError, ValueError):
                continue

    def binance_interval_hours(self, raw_symbol: str) -> float | None:
        if self.binance_intervals is None:
            return None
        return self.binance_intervals.get(raw_symbol.upper(), BINANCE_DEFAULT_INTERVAL_HOURS)

    # ── Parsing ──────────────────────────────────────────────────

    def _parse_binance(self, data: list[dict]) -> None:
        now = time.time()
        for item in data:
            try:
                raw = str(item["symbol"])
                symbol = normalise_fr_symbol(raw)
                if not symbol:
                    continue
                rate = float(item["lastFundingRate"])
                interval = self.binance_interval_hours(raw)
                if interval is None:
                    self.binance_unknown_interval_rows += 1
                    continue
                # Prefer the exchange's event time; fall back to local clock.
                ev_ms = item.get("time")
                ts = float(ev_ms) / 1000.0 if ev_ms else now
                self.rates["binance"][symbol] = _snapshot(ts, "binance", symbol, rate, interval)
            except (KeyError, ValueError, TypeError):
                continue

    def _parse_bybit(self, data: dict) -> None:
        now = time.time()
        # Bybit v5 puts server time (ms) on the response envelope; per-ticker
        # entries have no timestamp, so use the envelope time for all of them.
        env_ms = data.get("time")
        ts = float(env_ms) / 1000.0 if env_ms else now
        items = data.get("result", {}).get("list", [])
        for item in items:
            try:
                raw = str(item["symbol"])
                symbol = normalise_fr_symbol(raw)
                if not symbol:
                    continue
                rate = float(item["fundingRate"])
                interval = None
                if item.get("fundingIntervalHour") not in (None, ""):
                    interval = float(item["fundingIntervalHour"])
                if not interval or interval <= 0:
                    interval = self.bybit_intervals.get(raw.upper())
                if not interval:
                    self.bybit_unknown_interval_rows += 1
                    continue
                self.rates["bybit"][symbol] = _snapshot(ts, "bybit", symbol, rate, interval)
            except (KeyError, ValueError, TypeError):
                continue

    # ── Polling ──────────────────────────────────────────────────

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

    async def _maybe_refresh_binance_intervals(self, session: aiohttp.ClientSession) -> None:
        now = time.time()
        wait = INTERVAL_REFRESH_SECONDS if self.binance_intervals is not None else INTERVAL_RETRY_SECONDS
        if now - max(self._binance_intervals_at, self._binance_intervals_attempt_at) < wait:
            return
        self._binance_intervals_attempt_at = now
        try:
            async with session.get(BINANCE_FUNDING_INFO_URL, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                resp.raise_for_status()
                self._apply_binance_intervals(await resp.json())
        except Exception as exc:
            # Keep whatever was loaded before; rates without an interval wait.
            logger.info("Binance fundingInfo unavailable (%s); keeping %s intervals", exc,
                        "no" if self.binance_intervals is None else "the last known")

    async def _maybe_refresh_bybit_intervals(self, session: aiohttp.ClientSession) -> None:
        now = time.time()
        wait = INTERVAL_REFRESH_SECONDS if self.bybit_intervals else INTERVAL_RETRY_SECONDS
        if now - self._bybit_intervals_attempt_at < wait:
            return
        self._bybit_intervals_attempt_at = now
        cursor = ""
        try:
            for _ in range(5):  # 1000 instruments a page
                params = {"category": "linear", "limit": "1000"}
                if cursor:
                    params["cursor"] = cursor
                async with session.get(
                    BYBIT_INSTRUMENTS_URL, params=params, timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    resp.raise_for_status()
                    page = await resp.json()
                self._apply_bybit_intervals(page)
                cursor = (page.get("result") or {}).get("nextPageCursor") or ""
                if not cursor:
                    break
        except Exception as exc:
            logger.info("Bybit instruments-info unavailable (%s)", exc)

    async def _fetch_binance(self, session: aiohttp.ClientSession) -> None:
        await self._maybe_refresh_binance_intervals(session)
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
        rows = data.get("result", {}).get("list", []) if isinstance(data, dict) else []
        if any(r.get("fundingIntervalHour") in (None, "") for r in rows if isinstance(r, dict)):
            await self._maybe_refresh_bybit_intervals(session)
        self._parse_bybit(data)
        logger.debug("Bybit funding: %d symbols updated", len(self.rates["bybit"]))
