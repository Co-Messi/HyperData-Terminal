"""
Deribit Implied Volatility feed via DVOL index.

Connects to Deribit's public WebSocket API and subscribes to the
DVOL (Deribit Volatility Index) for BTC and ETH. No API key required.

DVOL is Deribit's real-time 30-day implied volatility index, similar to VIX.
Note: Perpetual ticker channels do NOT contain IV data (mark_iv is always None
for perpetuals). DVOL is the correct source for implied volatility.

Usage:
    feed = DeribitFeed()
    await feed.start()
    snap = feed.get_latest("BTC")  # DeribitIVSnapshot or None
    await feed.stop()
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

DERIBIT_WS_URL = "wss://www.deribit.com/ws/api/v2"
RECONNECT_MAX_BACKOFF = 60.0

# DVOL channels — Deribit's 30-day implied volatility index (like VIX)
DVOL_CHANNELS = {
    "deribit_volatility_index.btc_usd": "BTC",
    "deribit_volatility_index.eth_usd": "ETH",
}
INDEX_CHANNELS = [
    "deribit_price_index.btc_usd",
    "deribit_price_index.eth_usd",
]


@dataclass
class DeribitIVSnapshot:
    timestamp: float
    underlying: str      # "BTC" or "ETH"
    mark_iv: float       # Current mark implied volatility (%)
    bid_iv: float        # Best bid implied volatility (%)
    ask_iv: float        # Best ask implied volatility (%)
    oi_usd: float        # Open interest in USD
    index_price: float   # Deribit index price


class DeribitFeed:
    """Streams Deribit IV data via public WebSocket."""

    def __init__(self) -> None:
        self.snapshots: dict[str, DeribitIVSnapshot] = {}
        self._index_prices: dict[str, float] = {}  # "BTC" -> price
        self._task: asyncio.Task | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False

    # ── Lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_forever(), name="deribit-ws")
        logger.info("DeribitFeed started")

    async def stop(self) -> None:
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── Public API ───────────────────────────────────────────────

    def get_latest(self, underlying: str) -> DeribitIVSnapshot | None:
        return self.snapshots.get(underlying.upper())

    # ── Message handling (public for testability) ─────────────────

    def _handle_message(self, data: dict) -> None:
        if data.get("method") != "subscription":
            return
        params = data.get("params", {})
        channel = params.get("channel", "")
        msg_data = params.get("data", {})

        # Handle DVOL channels (implied volatility)
        if channel in DVOL_CHANNELS:
            underlying = DVOL_CHANNELS[channel]
            self._update_dvol(underlying, msg_data)
            return

        # Handle price index channels
        if channel.startswith("deribit_price_index."):
            self._update_index(channel, msg_data)

    def _update_dvol(self, underlying: str, data: dict) -> None:
        """Parse DVOL (Deribit Volatility Index) message."""
        try:
            volatility = float(data.get("volatility", 0.0))
            ts = int(data.get("timestamp", time.time() * 1000)) / 1000.0

            # DVOL gives a single implied volatility number — store as mark_iv.
            # bid_iv/ask_iv not available from DVOL, set to 0.
            self.snapshots[underlying] = DeribitIVSnapshot(
                timestamp=ts,
                underlying=underlying,
                mark_iv=volatility,
                bid_iv=0.0,
                ask_iv=0.0,
                oi_usd=0.0,
                index_price=self._index_prices.get(underlying, 0.0),
            )
        except (ValueError, TypeError):
            logger.debug("[deribit] Failed to parse DVOL for %s", underlying)

    def _update_index(self, channel: str, data: dict) -> None:
        idx_map = {"btc_usd": "BTC", "eth_usd": "ETH"}
        for key, underlying in idx_map.items():
            if key in channel:
                try:
                    self._index_prices[underlying] = float(data.get("price", 0.0))
                except (ValueError, TypeError):
                    pass

    # ── WebSocket loop ────────────────────────────────────────────

    async def _run_forever(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                await self._connect_and_listen()
                backoff = 1.0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("[deribit] Connection error (%s), reconnecting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF)

    async def _connect_and_listen(self) -> None:
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(DERIBIT_WS_URL)
            logger.info("[deribit] connected")

            channels = list(DVOL_CHANNELS.keys()) + INDEX_CHANNELS
            sub_msg = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "public/subscribe",
                "params": {"channels": channels},
            }
            await self._ws.send_json(sub_msg)
            logger.info("[deribit] subscribed to %d channels", len(channels))

            async for msg in self._ws:
                if not self._running:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        self._handle_message(json.loads(msg.data))
                    except json.JSONDecodeError:
                        pass
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            if self._ws and not self._ws.closed:
                await self._ws.close()
            if self._session and not self._session.closed:
                await self._session.close()
