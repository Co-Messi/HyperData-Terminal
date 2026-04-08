"""
CVD (Cumulative Volume Delta) and Order Flow Engine.

Real-time order flow analysis via Hyperliquid WebSocket trades feed.
Tracks buying vs selling pressure, computes CVD, OFI, and generates
multi-timeframe signals.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import aiohttp

from config.settings import DEFAULT_SYMBOLS

logger = logging.getLogger(__name__)

WS_URL = "wss://api.hyperliquid.xyz/ws"

TIMEFRAME_WINDOWS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "24h": 86400,
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Trade:
    timestamp: float
    symbol: str
    side: str        # 'buy' or 'sell'
    price: float
    size: float
    size_usd: float


@dataclass(slots=True)
class CVDSnapshot:
    timestamp: float
    symbol: str
    timeframe: str        # '1m', '5m', '15m', '1h', '4h', '24h'
    cvd: float            # Cumulative buy - sell volume (USD)
    buy_volume: float     # Total buy volume (USD) in window
    sell_volume: float    # Total sell volume (USD) in window
    trade_count: int
    ofi: float            # Order Flow Imbalance: (buy-sell)/(buy+sell), [-1,1]
    trades_per_sec: float
    signal: str           # STRONG_BULL / BULLISH / NEUTRAL / BEARISH / STRONG_BEAR


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------

def classify_signal(ofi: float) -> str:
    """Derive a directional signal from Order Flow Imbalance."""
    if ofi > 0.4:
        return "STRONG_BULL"
    elif ofi > 0.15:
        return "BULLISH"
    elif ofi > -0.15:
        return "NEUTRAL"
    elif ofi > -0.4:
        return "BEARISH"
    else:
        return "STRONG_BEAR"


# ---------------------------------------------------------------------------
# TimeframeBucket – rolling window for one symbol / one timeframe
# ---------------------------------------------------------------------------

class TimeframeBucket:
    """Rolling window of trades for a specific timeframe."""

    def __init__(self, symbol: str, timeframe: str, window_seconds: int) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.window = window_seconds
        self.trades: deque[Trade] = deque()
        self.buy_volume: float = 0.0
        self.sell_volume: float = 0.0
        self.trade_count: int = 0

    # -- mutators -----------------------------------------------------------

    def add_trade(self, trade: Trade) -> None:
        """Add a trade and evict any that fell outside the window."""
        self.trades.append(trade)
        if trade.side == "buy":
            self.buy_volume += trade.size_usd
        else:
            self.sell_volume += trade.size_usd
        self.trade_count += 1
        self._expire_old(trade.timestamp)

    def _expire_old(self, now: float | None = None) -> None:
        """Remove trades whose timestamp is older than *now - window*."""
        if now is None:
            now = time.time()
        cutoff = now - self.window
        while self.trades and self.trades[0].timestamp < cutoff:
            old = self.trades.popleft()
            if old.side == "buy":
                self.buy_volume -= old.size_usd
            else:
                self.sell_volume -= old.size_usd
            self.trade_count -= 1
        # Guard against floating-point drift going negative.
        self.buy_volume = max(self.buy_volume, 0.0)
        self.sell_volume = max(self.sell_volume, 0.0)
        self.trade_count = max(self.trade_count, 0)

    # -- queries ------------------------------------------------------------

    def get_snapshot(self, now: float | None = None) -> CVDSnapshot:
        """Return the current state of this bucket as a CVDSnapshot."""
        if now is None:
            now = time.time()
        self._expire_old(now)

        total = self.buy_volume + self.sell_volume
        ofi = (self.buy_volume - self.sell_volume) / total if total > 0 else 0.0
        tps = self.trade_count / self.window if self.window > 0 else 0.0

        return CVDSnapshot(
            timestamp=now,
            symbol=self.symbol,
            timeframe=self.timeframe,
            cvd=self.buy_volume - self.sell_volume,
            buy_volume=self.buy_volume,
            sell_volume=self.sell_volume,
            trade_count=self.trade_count,
            ofi=ofi,
            trades_per_sec=tps,
            signal=classify_signal(ofi),
        )


# ---------------------------------------------------------------------------
# OrderFlowEngine
# ---------------------------------------------------------------------------

class OrderFlowEngine:
    """Connects to Hyperliquid WS, ingests trades, and maintains per-symbol
    per-timeframe CVD / OFI buckets."""

    def __init__(self, symbols: list[str] | None = None) -> None:
        self.symbols: list[str] = symbols or list(DEFAULT_SYMBOLS)
        self.timeframes: dict[str, int] = dict(TIMEFRAME_WINDOWS)

        # symbol -> timeframe -> bucket
        self.buckets: dict[str, dict[str, TimeframeBucket]] = {
            sym: {
                tf: TimeframeBucket(sym, tf, secs)
                for tf, secs in self.timeframes.items()
            }
            for sym in self.symbols
        }

        # Running CVD that never resets (cumulative since start).
        self.cumulative_cvd: dict[str, float] = {s: 0.0 for s in self.symbols}

        # Last N trades per symbol for display / inspection.
        self.recent_trades: dict[str, deque[Trade]] = {
            s: deque(maxlen=100) for s in self.symbols
        }

        self._callbacks: list[Callable[[Trade], None]] = []
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running: bool = False
        self._task: asyncio.Task | None = None

    # -- public API ---------------------------------------------------------

    async def start(self) -> None:
        """Open WebSocket(s), subscribe, and begin processing in background."""
        if self._running:
            logger.warning("OrderFlowEngine already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_forever())
        self._binance_task = asyncio.create_task(self._binance_trade_loop())
        logger.info("OrderFlowEngine started for %s (HL + Binance)", self.symbols)

    async def stop(self) -> None:
        """Gracefully disconnect."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        for task in [self._task, getattr(self, '_binance_task', None)]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("OrderFlowEngine stopped")

    def on_trade(self, callback: Callable[[Trade], None]) -> None:
        """Register a callback invoked for every incoming trade."""
        self._callbacks.append(callback)

    def get_snapshot(self, symbol: str, timeframe: str) -> CVDSnapshot:
        """Current CVD snapshot for *symbol* at *timeframe*."""
        return self.buckets[symbol][timeframe].get_snapshot()

    def get_all_snapshots(self, symbol: str) -> dict[str, CVDSnapshot]:
        """All timeframe snapshots for a symbol."""
        return {
            tf: bucket.get_snapshot()
            for tf, bucket in self.buckets[symbol].items()
        }

    def get_multi_timeframe_signal(self, symbol: str) -> str:
        """Combine 1h and 4h signals into one aggregate signal.

        Rules:
        - Both STRONG_BULL / BULLISH  -> STRONG_BULL
        - Both STRONG_BEAR / BEARISH  -> STRONG_BEAR
        - Same direction, mild        -> that direction (BULLISH / BEARISH)
        - Mixed                       -> CONTESTED
        """
        snap_1h = self.get_snapshot(symbol, "1h")
        snap_4h = self.get_snapshot(symbol, "4h")

        bull = {"STRONG_BULL", "BULLISH"}
        bear = {"STRONG_BEAR", "BEARISH"}

        sig_1h = snap_1h.signal
        sig_4h = snap_4h.signal

        if sig_1h in bull and sig_4h in bull:
            if sig_1h == "STRONG_BULL" or sig_4h == "STRONG_BULL":
                return "STRONG_BULL"
            return "BULLISH"
        if sig_1h in bear and sig_4h in bear:
            if sig_1h == "STRONG_BEAR" or sig_4h == "STRONG_BEAR":
                return "STRONG_BEAR"
            return "BEARISH"
        if sig_1h == "NEUTRAL" and sig_4h == "NEUTRAL":
            return "NEUTRAL"
        return "CONTESTED"

    def detect_divergence(
        self, symbol: str, price_data: list[float]
    ) -> str | None:
        """Detect CVD vs price divergence.

        *price_data* should be a list of recent prices (oldest first).
        We compare the trend of recent prices against the trend of CVD
        snapshots at increasing timeframes (1m, 5m, 15m).

        Returns:
            'BULLISH_DIVERGENCE'  – price falling but CVD rising
            'BEARISH_DIVERGENCE'  – price rising but CVD falling
            None                  – no divergence detected
        """
        if len(price_data) < 2:
            return None

        price_rising = price_data[-1] > price_data[0]

        # Compare short vs medium CVD to detect CVD trend.
        snap_short = self.get_snapshot(symbol, "1m")
        snap_med = self.get_snapshot(symbol, "15m")

        # Use OFI as a proxy for CVD trend direction.
        cvd_rising = snap_short.ofi > 0 and snap_med.ofi > 0
        cvd_falling = snap_short.ofi < 0 and snap_med.ofi < 0

        if price_rising and cvd_falling:
            return "BEARISH_DIVERGENCE"
        if not price_rising and cvd_rising:
            return "BULLISH_DIVERGENCE"
        return None

    def get_trades_per_second(self, symbol: str) -> float:
        """Current trades-per-second derived from the 1m bucket."""
        return self.get_snapshot(symbol, "1m").trades_per_sec

    # -- internal: process a single trade -----------------------------------

    def _process_trade(self, trade: Trade) -> None:
        """Route a trade to all timeframe buckets and bookkeeping."""
        sym = trade.symbol
        if sym not in self.buckets:
            return

        # Update every timeframe bucket for this symbol.
        for bucket in self.buckets[sym].values():
            bucket.add_trade(trade)

        # Update running cumulative CVD.
        delta = trade.size_usd if trade.side == "buy" else -trade.size_usd
        self.cumulative_cvd[sym] += delta

        # Store in recent-trades ring buffer.
        self.recent_trades[sym].append(trade)

        # Fire callbacks.
        for cb in self._callbacks:
            try:
                cb(trade)
            except Exception:
                logger.exception("Trade callback error")

    # -- internal: WebSocket loop -------------------------------------------

    async def _run_forever(self) -> None:
        """Main loop with auto-reconnect and exponential backoff."""
        backoff = 1.0
        max_backoff = 60.0

        while self._running:
            try:
                await self._connect_and_listen()
                # If we get here cleanly the connection was closed normally.
                backoff = 1.0
            except (
                aiohttp.WSServerHandshakeError,
                aiohttp.ClientError,
                asyncio.TimeoutError,
                ConnectionError,
                OSError,
            ) as exc:
                logger.warning(
                    "WebSocket error (%s), reconnecting in %.1fs", exc, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unexpected error in WS loop, reconnecting in %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def _connect_and_listen(self) -> None:
        """Single connection lifecycle: connect, subscribe, read messages."""
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(WS_URL)
            logger.info("WebSocket connected to %s", WS_URL)

            # Subscribe to trades for every symbol.
            for sym in self.symbols:
                msg = {
                    "method": "subscribe",
                    "subscription": {"type": "trades", "coin": sym},
                }
                await self._ws.send_json(msg)
                logger.debug("Subscribed to trades for %s", sym)

            # Read loop.
            async for ws_msg in self._ws:
                if not self._running:
                    break
                if ws_msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle_message(ws_msg.json())
                elif ws_msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        finally:
            if self._ws and not self._ws.closed:
                await self._ws.close()
            if self._session and not self._session.closed:
                await self._session.close()

    def _handle_message(self, data: dict) -> None:
        """Parse a WebSocket JSON message and create Trade objects."""
        channel = data.get("channel")
        if channel != "trades":
            return

        trades_raw = data.get("data")
        if not trades_raw:
            return

        for t in trades_raw:
            try:
                price = float(t["px"])
                size = float(t["sz"])
                side = "buy" if t["side"] == "B" else "sell"
                trade = Trade(
                    timestamp=t["time"] / 1000.0,  # ms -> seconds
                    symbol=t["coin"],
                    side=side,
                    price=price,
                    size=size,
                    size_usd=price * size,
                )
                self._process_trade(trade)
            except (KeyError, ValueError, TypeError):
                logger.exception("Failed to parse trade message: %s", t)

    # -- Binance trade stream (adds 10x volume to CVD) ---------------------

    # Map Binance futures symbols back to our standard names
    _BINANCE_SYMBOL_MAP = {
        "BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL",
        "DOGEUSDT": "DOGE", "XRPUSDT": "XRP", "AVAXUSDT": "AVAX",
        "LINKUSDT": "LINK", "ARBUSDT": "ARB", "SUIUSDT": "SUI",
        "APTUSDT": "APT", "OPUSDT": "OP", "SEIUSDT": "SEI",
        "PEPEUSDT": "PEPE", "WIFUSDT": "WIF", "INJUSDT": "INJ",
    }

    async def _binance_trade_loop(self) -> None:
        """Connect to Binance Futures aggTrade stream and feed into CVD engine.

        Binance BTC alone does more volume than all of Hyperliquid.
        This massively improves CVD/OFI signal accuracy.
        """
        import json as _json

        # Build combined stream URL for top symbols
        streams = [f"{sym.lower()}@aggTrade" for sym in self._BINANCE_SYMBOL_MAP]
        url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"

        backoff = 1.0
        while self._running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, heartbeat=20) as ws:
                        backoff = 1.0
                        logger.info("[binance-trades] Connected, streaming %d symbols", len(streams))

                        async for msg in ws:
                            if not self._running:
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = _json.loads(msg.data)
                                self._handle_binance_trade(data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break

            except asyncio.CancelledError:
                return
            except Exception:
                logger.warning("[binance-trades] Error, reconnecting in %.1fs", backoff)

            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _handle_binance_trade(self, raw: dict) -> None:
        """Parse a Binance aggTrade message and process it."""
        data = raw.get("data")
        if not data:
            return

        try:
            binance_sym = data.get("s", "")
            symbol = self._BINANCE_SYMBOL_MAP.get(binance_sym)
            if not symbol:
                return

            # Ensure symbol has buckets
            if symbol not in self.buckets:
                self.add_symbol(symbol)

            price = float(data["p"])
            qty = float(data["q"])
            # m=True means buyer is maker → taker is SELLER
            side = "sell" if data.get("m", False) else "buy"

            trade = Trade(
                timestamp=data["T"] / 1000.0,
                symbol=symbol,
                side=side,
                price=price,
                size=qty,
                size_usd=price * qty,
            )
            self._process_trade(trade)
        except (KeyError, ValueError, TypeError):
            pass  # Silently skip malformed messages

    # -- add / remove symbols at runtime ------------------------------------

    def add_symbol(self, symbol: str) -> None:
        """Register a new symbol (buckets only; WS re-subscribe happens on
        next reconnect or can be done manually)."""
        if symbol in self.buckets:
            return
        self.symbols.append(symbol)
        self.buckets[symbol] = {
            tf: TimeframeBucket(symbol, tf, secs)
            for tf, secs in self.timeframes.items()
        }
        self.cumulative_cvd[symbol] = 0.0
        self.recent_trades[symbol] = deque(maxlen=100)
