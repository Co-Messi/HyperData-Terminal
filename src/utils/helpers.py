from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

import aiohttp

from config.settings import WEBSOCKET_MAX_RECONNECT_DELAY, WEBSOCKET_RECONNECT_DELAY

logger = logging.getLogger(__name__)


def normalize_symbol(symbol: str, exchange: str) -> str:
    """Normalize symbol names across exchanges.

    'BTCUSDT' (binance/bybit) -> 'BTC'
    'BTC-USDT-SWAP' (okx) -> 'BTC'
    'BTC' (hyperliquid) -> 'BTC'
    """
    symbol = symbol.upper().strip()
    exchange = exchange.lower()

    if exchange in ("binance", "bybit"):
        for suffix in ("USDT", "USD", "BUSD", "USDC"):
            if symbol.endswith(suffix):
                return symbol[: -len(suffix)]
        return symbol

    if exchange == "okx":
        return symbol.split("-")[0]

    return symbol


def timestamp_ms() -> int:
    """Current timestamp in milliseconds."""
    return int(time.time() * 1000)


def format_usd(value: float) -> str:
    """Format USD value with appropriate suffix.

    1234567.89 -> '$1.23M'
    1234.56    -> '$1.23K'
    0.56       -> '$0.56'
    """
    negative = value < 0
    abs_val = abs(value)
    prefix = "-" if negative else ""

    if abs_val >= 1_000_000_000:
        return f"{prefix}${abs_val / 1_000_000_000:.2f}B"
    if abs_val >= 1_000_000:
        return f"{prefix}${abs_val / 1_000_000:.2f}M"
    if abs_val >= 1_000:
        return f"{prefix}${abs_val / 1_000:.2f}K"
    return f"{prefix}${abs_val:.2f}"


def format_pct(value: float, include_sign: bool = True) -> str:
    """Format a decimal ratio as a percentage string.

    0.0234  -> '+2.34%'
    -0.0051 -> '-0.51%'
    """
    pct = value * 100
    if include_sign:
        sign = "+" if pct >= 0 else ""
        return f"{sign}{pct:.2f}%"
    return f"{pct:.2f}%"


def format_price(value: float) -> str:
    """Format a price with appropriate decimal places.

    83500.0 -> '$83,500.00'
    178.5   -> '$178.50'
    0.165   -> '$0.1650'
    0.005   -> '$0.005000'
    """
    if value >= 100:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.4f}"
    if value >= 0.01:
        return f"${value:,.6f}"
    return f"${value:,.8f}"


def format_pct_value(value: float) -> str:
    """Format a value that is already a percentage (not a decimal ratio).

    2.34  -> '2.34%'
    -0.51 -> '-0.51%'
    """
    return f"{value:.2f}%"


class RateLimiter:
    """Async rate limiter using a token-bucket algorithm."""

    def __init__(self, max_per_second: int = 10):
        self._max_per_second = max_per_second
        self._tokens = float(max_per_second)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a request slot is available."""
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    self._max_per_second,
                    self._tokens + elapsed * self._max_per_second,
                )
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                wait = (1.0 - self._tokens) / self._max_per_second
                await asyncio.sleep(wait)


class WebSocketManager:
    """Reusable async WebSocket client with auto-reconnect and exponential backoff."""

    def __init__(
        self,
        url: str,
        on_message: Callable[[dict | list | str], Coroutine[Any, Any, None]],
        on_connect: Callable[[aiohttp.ClientWebSocketResponse], Coroutine[Any, Any, None]] | None = None,
        name: str = "",
    ):
        self.url = url
        self._on_message = on_message
        self._on_connect = on_connect
        self.name = name or url

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._running = False
        self._send_lock = asyncio.Lock()
        self._reconnect_delay = WEBSOCKET_RECONNECT_DELAY
        self._max_reconnect_delay = WEBSOCKET_MAX_RECONNECT_DELAY

    async def connect(self) -> None:
        """Connect and start the receive loop with auto-reconnect."""
        self._running = True
        self._session = aiohttp.ClientSession()
        delay = self._reconnect_delay

        while self._running:
            try:
                logger.info("[%s] Connecting to %s", self.name, self.url)
                self._ws = await self._session.ws_connect(
                    self.url,
                    heartbeat=20.0,
                    autoping=True,
                    timeout=aiohttp.ClientWSTimeout(ws_close=10.0),
                )
                logger.info("[%s] Connected", self.name)
                delay = self._reconnect_delay  # reset backoff on success

                if self._on_connect is not None:
                    await self._on_connect(self._ws)

                await self._receive_loop()

            except (
                aiohttp.WSServerHandshakeError,
                aiohttp.ClientConnectionError,
                OSError,
            ) as exc:
                logger.warning("[%s] Connection error: %s", self.name, exc)

            except asyncio.CancelledError:
                break

            if not self._running:
                break

            logger.info("[%s] Reconnecting in %.1fs", self.name, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_reconnect_delay)

        await self._cleanup()

    async def _receive_loop(self) -> None:
        """Process incoming messages until the socket closes."""
        if self._ws is None:
            return

        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    data = msg.data
                try:
                    await self._on_message(data)
                except Exception:
                    logger.exception("[%s] Error in message handler", self.name)

            elif msg.type == aiohttp.WSMsgType.BINARY:
                try:
                    data = json.loads(msg.data)
                    await self._on_message(data)
                except Exception:
                    logger.exception("[%s] Error processing binary message", self.name)

            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                logger.info("[%s] WebSocket closed by server", self.name)
                break

            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error("[%s] WebSocket error: %s", self.name, self._ws.exception())
                break

    async def send(self, data: dict) -> None:
        """Send a JSON message (thread-safe via lock)."""
        async with self._send_lock:
            if self._ws is not None and not self._ws.closed:
                await self._ws.send_json(data)

    async def close(self) -> None:
        """Gracefully shut down the connection."""
        logger.info("[%s] Closing", self.name)
        self._running = False
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        await self._cleanup()

    async def _cleanup(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
        self._ws = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed
