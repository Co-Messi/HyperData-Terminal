"""
Orderbook depth streaming engine.

WebSocket-connects to Hyperliquid l2Book channel for top 10 symbols.
Maintains live 50-level orderbooks and computes bid/ask imbalance.

Usage:
    engine = OrderBookEngine(symbols=["BTC", "ETH"])
    await engine.start()
    snap = engine.get_snapshot("BTC")  # OrderBookSnapshot or None
    await engine.stop()
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

WS_URL = "wss://api.hyperliquid.xyz/ws"
SNAPSHOT_INTERVAL = 5.0
DEFAULT_DEPTH = 50
IMBALANCE_DEPTH = 10  # Use top 10 levels for imbalance calculation
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "DOGE", "XRP", "AVAX", "LINK", "ARB", "WIF", "SUI"]


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBookSnapshot:
    timestamp: float
    symbol: str
    bids: list[OrderBookLevel]  # Sorted best (highest) first
    asks: list[OrderBookLevel]  # Sorted best (lowest) first
    imbalance: float            # (bid_vol_top10 - ask_vol_top10) / total, [-1, 1]
    best_bid: float
    best_ask: float
    spread: float


def compute_imbalance(
    bids: list[OrderBookLevel],
    asks: list[OrderBookLevel],
    depth: int = IMBALANCE_DEPTH,
) -> float:
    """Bid/ask volume imbalance using top-N levels. Returns value in [-1, 1]."""
    bid_vol = sum(lvl.size for lvl in bids[:depth])
    ask_vol = sum(lvl.size for lvl in asks[:depth])
    total = bid_vol + ask_vol
    if total == 0:
        return 0.0
    return (bid_vol - ask_vol) / total


class OrderBookEngine:
    """Maintains live orderbooks via Hyperliquid l2Book WebSocket."""

    def __init__(self, symbols: list[str] | None = None, depth: int = DEFAULT_DEPTH) -> None:
        self.symbols = symbols or list(DEFAULT_SYMBOLS)
        self.depth = depth
        # books[symbol] = {"bids": [...], "asks": [...], "updated_at": float}
        self.books: dict[str, dict] = {
            sym: {"bids": [], "asks": [], "updated_at": 0.0}
            for sym in self.symbols
        }
        # Latest snapshot per symbol
        self.snapshots: dict[str, OrderBookSnapshot] = {}

        self._task: asyncio.Task | None = None
        self._snapshot_task: asyncio.Task | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False

    # ── Lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_forever(), name="orderbook-ws")
        self._snapshot_task = asyncio.create_task(self._snapshot_loop(), name="orderbook-snapshots")
        logger.info("OrderBookEngine started for %s", self.symbols)

    async def stop(self) -> None:
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        for task in [self._task, self._snapshot_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("OrderBookEngine stopped")

    # ── Public API ───────────────────────────────────────────────

    def get_snapshot(self, symbol: str) -> OrderBookSnapshot | None:
        return self.snapshots.get(symbol.upper())

    # ── Book update (public for testability) ─────────────────────

    def _update_book(self, symbol: str, data: dict) -> None:
        """Parse l2Book levels data and update the in-memory book."""
        if symbol not in self.books:
            return
        levels = data.get("levels", [[], []])
        bids_raw = levels[0] if len(levels) > 0 else []
        asks_raw = levels[1] if len(levels) > 1 else []

        bids = [
            OrderBookLevel(price=float(b["px"]), size=float(b["sz"]))
            for b in bids_raw[:self.depth]
        ]
        asks = [
            OrderBookLevel(price=float(a["px"]), size=float(a["sz"]))
            for a in asks_raw[:self.depth]
        ]

        self.books[symbol]["bids"] = bids
        self.books[symbol]["asks"] = asks
        self.books[symbol]["updated_at"] = time.time()
        self._build_snapshot(symbol)

    def _build_snapshot(self, symbol: str) -> None:
        book = self.books[symbol]
        bids = book["bids"]
        asks = book["asks"]
        imbalance = compute_imbalance(bids, asks)
        best_bid = bids[0].price if bids else 0.0
        best_ask = asks[0].price if asks else 0.0
        spread = best_ask - best_bid if best_bid > 0 and best_ask > 0 else 0.0

        self.snapshots[symbol] = OrderBookSnapshot(
            timestamp=book["updated_at"],
            symbol=symbol,
            bids=bids,
            asks=asks,
            imbalance=imbalance,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
        )

    def _handle_message(self, data: dict) -> None:
        if data.get("channel") != "l2Book":
            return
        book_data = data.get("data", {})
        symbol = book_data.get("coin", "")
        if symbol and symbol in self.books:
            self._update_book(symbol, book_data)

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
                logger.warning("OrderBookEngine WS error (%s), reconnecting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _connect_and_listen(self) -> None:
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(WS_URL)
            for sym in self.symbols:
                await self._ws.send_json({
                    "method": "subscribe",
                    "subscription": {"type": "l2Book", "coin": sym, "nSigFigs": 5},
                })
            logger.info("[orderbook] subscribed to %d l2Book feeds", len(self.symbols))

            async for msg in self._ws:
                if not self._running:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle_message(msg.json())
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            if self._ws and not self._ws.closed:
                await self._ws.close()
            if self._session and not self._session.closed:
                await self._session.close()

    async def _snapshot_loop(self) -> None:
        """Log imbalance snapshots every 5s (dashboards read from self.snapshots directly)."""
        while self._running:
            try:
                for symbol in self.symbols:
                    snap = self.snapshots.get(symbol)
                    if snap:
                        logger.debug(
                            "[orderbook] %s imbalance=%.3f spread=%.2f",
                            symbol, snap.imbalance, snap.spread,
                        )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("OrderBookEngine snapshot loop error")
            await asyncio.sleep(SNAPSHOT_INTERVAL)
