from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

from config.settings import HYPERLIQUID_INFO_URL, MAX_REQUESTS_PER_SECOND

logger = logging.getLogger(__name__)


@dataclass
class AssetInfo:
    symbol: str
    price: float
    funding_rate: float
    open_interest: float
    volume_24h: float
    price_change_24h_pct: float
    mark_price: float
    index_price: float
    premium_pct: float = 0.0  # (mark - index) / index * 100


class MarketData:
    """Fetches and caches market data from the Hyperliquid info API."""

    def __init__(self) -> None:
        self.assets: dict[str, AssetInfo] = {}
        self.last_update: float = 0.0
        self._cache_ttl: float = 1.0  # 1 second (was 5 — too stale for trading)
        self._semaphore = asyncio.Semaphore(MAX_REQUESTS_PER_SECOND)
        self._session: aiohttp.ClientSession | None = None

    # ── Public API ────────────────────────────────────────────────

    async def refresh(self) -> None:
        """Fetch all market data from Hyperliquid and update the cache."""
        async with self._managed_session() as session:
            data = await self._post(session, {"type": "metaAndAssetCtxs"})

        if not isinstance(data, list) or len(data) < 2:
            logger.warning("Unexpected response from metaAndAssetCtxs")
            return

        meta, asset_ctxs = data[0], data[1]
        universe = meta.get("universe", [])

        assets: dict[str, AssetInfo] = {}
        for asset_meta, ctx in zip(universe, asset_ctxs):
            symbol = asset_meta.get("name", "")
            if not symbol:
                continue

            try:
                mark_px = float(ctx.get("markPx", 0))
                oracle_px = float(ctx.get("oraclePx", 0))
                funding = float(ctx.get("funding", 0))
                open_interest = float(ctx.get("openInterest", 0))
                day_ntl_vlm = float(ctx.get("dayNtlVlm", 0))
                prev_day_px = float(ctx.get("prevDayPx", 0))

                price_change_pct = 0.0
                if prev_day_px > 0:
                    price_change_pct = (mark_px - prev_day_px) / prev_day_px

                oi_usd = open_interest * mark_px

                premium_pct = 0.0
                if oracle_px > 0:
                    premium_pct = (mark_px - oracle_px) / oracle_px * 100
                    if abs(premium_pct) > 0.5 and day_ntl_vlm > 100_000:
                        logger.warning(
                            "High premium on %s: mark=%.4f index=%.4f premium=%.3f%%",
                            symbol, mark_px, oracle_px, premium_pct,
                        )

                assets[symbol] = AssetInfo(
                    symbol=symbol,
                    price=mark_px,
                    funding_rate=funding,
                    open_interest=oi_usd,
                    volume_24h=day_ntl_vlm,
                    price_change_24h_pct=price_change_pct,
                    mark_price=mark_px,
                    index_price=oracle_px,
                    premium_pct=premium_pct,
                )
            except (ValueError, TypeError) as exc:
                logger.debug("Skipping %s: %s", symbol, exc)

        self.assets = assets
        self.last_update = time.monotonic()
        logger.info("Refreshed market data for %d assets", len(assets))

    async def get_asset(self, symbol: str) -> AssetInfo:
        """Get data for a single asset, refreshing if cache is stale."""
        await self._ensure_fresh()
        symbol = symbol.upper()
        if symbol not in self.assets:
            raise KeyError(f"Unknown asset: {symbol}")
        return self.assets[symbol]

    async def get_all(self) -> list[AssetInfo]:
        """Get all assets sorted by 24h volume descending."""
        await self._ensure_fresh()
        return sorted(self.assets.values(), key=lambda a: a.volume_24h, reverse=True)

    async def get_funding_extremes(self, threshold: float = 0.01) -> list[AssetInfo]:
        """Get assets with extreme funding rates.

        Threshold is annualized -- e.g. 0.01 means > 1% annualized.
        Hyperliquid funding is hourly, so annualized = hourly * 8760.
        """
        await self._ensure_fresh()
        results: list[AssetInfo] = []
        for asset in self.assets.values():
            annualized = abs(asset.funding_rate) * 8760
            if annualized >= threshold:
                results.append(asset)
        return sorted(results, key=lambda a: abs(a.funding_rate), reverse=True)

    async def get_orderbook(self, symbol: str, depth: int = 20) -> dict:
        """Get L2 orderbook for a symbol."""
        async with self._managed_session() as session:
            data = await self._post(session, {"type": "l2Book", "coin": symbol.upper()})

        if not isinstance(data, dict):
            return {"bids": [], "asks": []}

        levels = data.get("levels", [[], []])
        bids = [{"price": float(b["px"]), "size": float(b["sz"])} for b in levels[0][:depth]]
        asks = [{"price": float(a["px"]), "size": float(a["sz"])} for a in levels[1][:depth]]
        return {"bids": bids, "asks": asks}

    async def get_candles(
        self,
        symbol: str,
        timeframe: str = "15m",
        bars: int = 100,
    ) -> list[dict]:
        """Get OHLCV candle data.

        Timeframe: '1m', '5m', '15m', '1h', '4h', '1d'
        """
        interval_ms = _timeframe_to_ms(timeframe)
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (interval_ms * bars)

        async with self._managed_session() as session:
            data = await self._post(session, {
                "type": "candleSnapshot",
                "coin": symbol.upper(),
                "interval": timeframe,
                "startTime": start_ms,
                "endTime": now_ms,
            })

        if not isinstance(data, list):
            return []

        candles: list[dict] = []
        for c in data:
            candles.append({
                "timestamp": c.get("t"),
                "open": float(c.get("o", 0)),
                "high": float(c.get("h", 0)),
                "low": float(c.get("l", 0)),
                "close": float(c.get("c", 0)),
                "volume": float(c.get("v", 0)),
            })
        return candles

    async def get_recent_trades(self, symbol: str, limit: int = 100) -> list[dict]:
        """Get recent trades for a symbol."""
        async with self._managed_session() as session:
            data = await self._post(session, {
                "type": "recentTrades",
                "coin": symbol.upper(),
            })

        if not isinstance(data, list):
            return []

        trades: list[dict] = []
        for t in data[:limit]:
            trades.append({
                "time": t.get("time"),
                "price": float(t.get("px", 0)),
                "size": float(t.get("sz", 0)),
                "side": t.get("side", ""),
            })
        return trades

    # ── Internals ─────────────────────────────────────────────────

    async def _ensure_fresh(self) -> None:
        if not self.assets or (time.monotonic() - self.last_update) > self._cache_ttl:
            await self.refresh()

    def _managed_session(self) -> _SessionCtx:
        """Return a context manager that provides an aiohttp session."""
        return _SessionCtx(self)

    async def _post(self, session: aiohttp.ClientSession, payload: dict) -> dict | list | None:
        async with self._semaphore:
            async with session.post(
                HYPERLIQUID_INFO_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as resp:
                resp.raise_for_status()
                return await resp.json()


class _SessionCtx:
    """Async context manager that creates a temporary aiohttp session if the
    owner doesn't already have one open."""

    def __init__(self, owner: MarketData):
        self._owner = owner
        self._temp: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> aiohttp.ClientSession:
        if self._owner._session and not self._owner._session.closed:
            return self._owner._session
        self._temp = aiohttp.ClientSession()
        return self._temp

    async def __aexit__(self, *exc: object) -> None:
        if self._temp is not None:
            await self._temp.close()
            self._temp = None


def _timeframe_to_ms(tf: str) -> int:
    """Convert a timeframe string like '15m' or '4h' to milliseconds."""
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    suffix = tf[-1]
    amount = int(tf[:-1])
    return amount * units.get(suffix, 60_000)
