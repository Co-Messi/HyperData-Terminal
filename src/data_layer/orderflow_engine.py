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
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Callable

import aiohttp

from config.settings import DEFAULT_SYMBOLS

logger = logging.getLogger(__name__)

WS_URL = "wss://api.hyperliquid.xyz/ws"

# Liquid majors trade many times per second; if no trade arrives from either
# venue for this long the order-flow feed is treated as stale rather than
# letting frozen CVD/OFI numbers read as live.
STALE_AFTER_SECONDS = 30.0

# A freshly (re)connected venue gets this long to deliver its first frame
# before it is reported as 'silent'. Same threshold as staleness on purpose:
# a liquid venue that has not sent a single frame in 30s is not "warming up".
CONNECT_GRACE_SECONDS = STALE_AFTER_SECONDS

# Parse failures are counted per venue and logged at most this often, so a
# schema change is visible without a warning per frame.
PARSE_ERROR_LOG_INTERVAL = 60.0

VENUES = ("hyperliquid", "binance")

# Upper bound on remembered trade IDs per venue (oldest evicted first).
MAX_SEEN_IDS = 100_000


@dataclass
class VenueState:
    """Per-venue liveness bookkeeping.

    Distinguishes the four ways a venue can be "not delivering": never
    connected, connected-but-silent (the confirmed-live Binance regional
    block: handshake succeeds, zero frames follow, forever), frames arriving
    that no longer parse into trades (a schema change), and a venue that
    had data and went quiet.
    """
    connected: bool = False
    connected_at: float = 0.0       # last (re)connect wall-clock, 0 = never
    disconnected_at: float = 0.0
    connects: int = 0
    frames: int = 0                 # any text frame, including acks/errors
    last_frame_at: float = 0.0
    trades: int = 0                 # frames that parsed into a Trade
    parse_errors: int = 0
    _last_parse_error_log_at: float = field(default=0.0, repr=False)

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
        """Remove trades whose timestamp is older than *now - window*.

        Note: trade.timestamp is exchange event time while the default ``now`` is
        local wall-clock. The two clock domains differ only by network/clock
        skew (sub-second in practice), which is negligible against the smallest
        60s window; callers needing exactness can pass an explicit ``now``.
        """
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

        # Running CVD that never resets (cumulative since start). `cumulative_cvd`
        # is the combined HL+Binance figure (kept for back-compat); the per-venue
        # series let consumers tell the two apart instead of reading a silent sum.
        self.cumulative_cvd: dict[str, float] = {s: 0.0 for s in self.symbols}
        self.cumulative_cvd_hl: dict[str, float] = {s: 0.0 for s in self.symbols}
        self.cumulative_cvd_binance: dict[str, float] = {s: 0.0 for s in self.symbols}

        # Per-venue dedup of trade IDs so a reconnect/resubscribe replay can't
        # double-count into the cumulative CVD (which never resets). Bounded
        # like the liquidation feed's _seen_tids.
        self._seen_hl_tids: OrderedDict = OrderedDict()
        self._seen_binance_ids: OrderedDict = OrderedDict()

        # Last N trades per symbol for display / inspection.
        self.recent_trades: dict[str, deque[Trade]] = {
            s: deque(maxlen=100) for s in self.symbols
        }

        # Wall-clock time of the last PARSED TRADE from each venue (not the
        # last frame — an ack or an unparseable frame must not read as
        # liveness). 0.0 means "no trade received yet".
        self.last_hl_message_at: float = 0.0
        self.last_binance_message_at: float = 0.0
        # Connection / frame / parse-error bookkeeping per venue.
        self.venues: dict[str, VenueState] = {v: VenueState() for v in VENUES}
        # Set by demo-mode callers that feed _process_trade() directly: the
        # renderers then label the CVD as synthetic instead of pretending to
        # know which venue it came from.
        self.synthetic: bool = False

        self._callbacks: list[Callable[[Trade], None]] = []
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running: bool = False
        self._task: asyncio.Task | None = None
        self._binance_task: asyncio.Task | None = None

    # -- public API ---------------------------------------------------------

    @property
    def last_message_at(self) -> float:
        """Most recent trade time across both venues (HL + Binance)."""
        return max(self.last_hl_message_at, self.last_binance_message_at)

    def data_age(self, now: float | None = None) -> float:
        """Seconds since the last trade from any venue (inf if none yet)."""
        if self.last_message_at <= 0:
            return float("inf")
        return (now if now is not None else time.time()) - self.last_message_at

    def is_stale(self, now: float | None = None) -> bool:
        return self.data_age(now) > STALE_AFTER_SECONDS

    def venue_data_age(self, venue: str, now: float | None = None) -> float:
        """Seconds since the last trade from ONE venue (inf if none yet)."""
        last = (self.last_hl_message_at if venue == "hyperliquid"
                else self.last_binance_message_at)
        if last <= 0:
            return float("inf")
        return (now if now is not None else time.time()) - last

    def venue_is_stale(self, venue: str, now: float | None = None) -> bool:
        return self.venue_data_age(venue, now) > STALE_AFTER_SECONDS

    # -- venue liveness bookkeeping -------------------------------------------

    def _venue_connected(self, venue: str) -> None:
        st = self.venues[venue]
        st.connected = True
        st.connected_at = time.time()
        st.connects += 1

    def _venue_disconnected(self, venue: str) -> None:
        st = self.venues[venue]
        if st.connected:
            st.disconnected_at = time.time()
        st.connected = False

    def _venue_frame(self, venue: str) -> None:
        """Any inbound text frame — including subscription acks and error
        envelopes — counts as a frame but NOT as a trade."""
        st = self.venues[venue]
        st.frames += 1
        st.last_frame_at = time.time()

    def _venue_trade(self, venue: str) -> None:
        """A frame parsed into a Trade: this is the only thing that stamps
        the per-venue liveness the staleness watchdog reads."""
        now = time.time()
        self.venues[venue].trades += 1
        if venue == "hyperliquid":
            self.last_hl_message_at = now
        else:
            self.last_binance_message_at = now

    def _venue_parse_error(self, venue: str, exc: BaseException, payload) -> None:
        st = self.venues[venue]
        st.parse_errors += 1
        now = time.time()
        if now - st._last_parse_error_log_at >= PARSE_ERROR_LOG_INTERVAL:
            st._last_parse_error_log_at = now
            logger.warning(
                "[%s] failed to parse trade frame (%d parse errors so far — a schema "
                "change here freezes this venue's CVD): %s: %s | payload=%.300r",
                venue, st.parse_errors, type(exc).__name__, exc, payload,
            )

    def venue_status(self, venue: str, now: float | None = None) -> tuple[str, str]:
        """(status, reason) for one venue.

        status is one of:
          ok            trades arriving within STALE_AFTER_SECONDS
          connecting    (re)connected < CONNECT_GRACE_SECONDS ago, no trade yet
          silent        connected, past the grace period, ZERO frames received
                        since this connection (regional block / dead stream)
          frozen        frames still arriving but nothing has parsed into a
                        trade for STALE_AFTER_SECONDS (schema change / acks only)
          stale         had trades, socket open, nothing for STALE_AFTER_SECONDS
          disconnected  socket not open (never connected, or between reconnects)
        """
        now = time.time() if now is None else now
        st = self.venues[venue]
        trade_age = self.venue_data_age(venue, now)
        frame_age = float("inf") if st.last_frame_at <= 0 else now - st.last_frame_at

        if not st.connected:
            if st.connected_at <= 0:
                return "disconnected", "never connected"
            down_for = now - st.disconnected_at if st.disconnected_at > 0 else 0.0
            return "disconnected", f"disconnected {down_for:.0f}s ago, reconnecting"

        connected_for = now - st.connected_at
        no_frame_this_connection = st.last_frame_at < st.connected_at
        if connected_for < CONNECT_GRACE_SECONDS and (no_frame_this_connection or trade_age == float("inf")):
            return "connecting", f"connected {connected_for:.0f}s ago, awaiting first trade"
        if no_frame_this_connection:
            return "silent", (
                f"connected {connected_for:.0f}s ago, 0 frames received "
                f"(handshake succeeded but the stream delivers nothing — regional block?)"
            )
        if trade_age > STALE_AFTER_SECONDS:
            if frame_age <= STALE_AFTER_SECONDS:
                last = "never" if trade_age == float("inf") else f"{trade_age:.0f}s ago"
                return "frozen", (
                    f"frames arriving ({st.frames} total) but last parsed trade {last}; "
                    f"{st.parse_errors} parse errors"
                )
            return "stale", f"last trade {trade_age:.0f}s ago, no frames for {frame_age:.0f}s"
        return "ok", f"last trade {trade_age:.1f}s ago"

    def venue_freshness(self, now: float | None = None) -> dict[str, dict]:
        """Per-venue freshness so a dead venue can't hide behind a live one.

        The combined is_stale() uses the freshest venue (intentional: the
        blended CVD is still moving), so this is the ONLY place a
        connected-but-silent venue is visible. Every consumer that presents
        order-flow data as multi-venue (health monitor, /v1/health,
        /v1/orderflow, both CVD renderers, the hub watchdog) reads it.
        """
        now = time.time() if now is None else now
        out: dict[str, dict] = {}
        for venue in VENUES:
            st = self.venues[venue]
            age = self.venue_data_age(venue, now)
            status, reason = self.venue_status(venue, now)
            out[venue] = {
                "status": status,
                "reason": reason,
                "data_age_seconds": None if age == float("inf") else round(age, 1),
                "stale": age > STALE_AFTER_SECONDS,
                "connected": st.connected,
                "connected_for_seconds": round(now - st.connected_at, 1) if st.connected else None,
                "connects": st.connects,
                "frames": st.frames,
                "trades": st.trades,
                "parse_errors": st.parse_errors,
            }
        return out

    def venue_coverage(self, now: float | None = None) -> dict[str, str]:
        """{venue: status} — the short form renderers put next to a CVD number."""
        return {v: self.venue_status(v, now)[0] for v in VENUES}

    def contributing_venues(self, now: float | None = None) -> list[str]:
        """Venues whose trades are currently flowing into the CVD/OFI figures."""
        return [v for v, s in self.venue_coverage(now).items() if s == "ok"]

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
        for task in [self._task, getattr(self, "_binance_task", None)]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._binance_task = None
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

    def get_cumulative_cvd(self, symbol: str) -> dict[str, float]:
        """Cumulative CVD broken out by venue so 'BTC CVD' isn't a silent sum.

        Returns the combined figure plus the per-venue Hyperliquid and Binance
        series.
        """
        return {
            "combined": self.cumulative_cvd.get(symbol, 0.0),
            "hyperliquid": self.cumulative_cvd_hl.get(symbol, 0.0),
            "binance": self.cumulative_cvd_binance.get(symbol, 0.0),
        }

    # -- internal: process a single trade -----------------------------------

    def _process_trade(self, trade: Trade, venue: str | None = None) -> None:
        """Route a trade to all timeframe buckets and bookkeeping.

        ``venue`` is 'hyperliquid' or 'binance' so the per-venue cumulative CVD
        can be tracked separately; None updates only the combined series.
        """
        sym = trade.symbol
        if sym not in self.buckets:
            return

        # Update every timeframe bucket for this symbol.
        for bucket in self.buckets[sym].values():
            bucket.add_trade(trade)

        # Update running cumulative CVD (combined + per-venue).
        delta = trade.size_usd if trade.side == "buy" else -trade.size_usd
        self.cumulative_cvd[sym] += delta
        if venue == "hyperliquid":
            self.cumulative_cvd_hl[sym] = self.cumulative_cvd_hl.get(sym, 0.0) + delta
        elif venue == "binance":
            self.cumulative_cvd_binance[sym] = self.cumulative_cvd_binance.get(sym, 0.0) + delta

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
            # heartbeat=20 so a half-open HL socket raises instead of silently
            # freezing the CVD buckets (the other venue/socket already does this).
            self._ws = await self._session.ws_connect(WS_URL, heartbeat=20)
            self._venue_connected("hyperliquid")
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
            self._venue_disconnected("hyperliquid")
            if self._ws and not self._ws.closed:
                await self._ws.close()
            if self._session and not self._session.closed:
                await self._session.close()

    def _handle_message(self, data: dict) -> None:
        """Parse a WebSocket JSON message and create Trade objects.

        Every frame counts toward the venue's frame counter; only a frame
        that parses into a Trade stamps liveness (see VenueState).
        """
        self._venue_frame("hyperliquid")
        if not isinstance(data, dict):
            return
        channel = data.get("channel")
        if channel != "trades":
            return

        trades_raw = data.get("data")
        if not trades_raw:
            return

        for t in trades_raw:
            try:
                # Skip trades already seen (a resubscribe on reconnect can replay
                # them, which would double-count into the never-resetting CVD).
                tid = t.get("tid")
                coin = t["coin"]
                if tid is not None:
                    key = (coin, tid)
                    if key in self._seen_hl_tids:
                        continue
                    self._seen_hl_tids[key] = None
                    while len(self._seen_hl_tids) > MAX_SEEN_IDS:
                        self._seen_hl_tids.popitem(last=False)

                price = float(t["px"])
                size = float(t["sz"])
                side = "buy" if t["side"] == "B" else "sell"
                trade = Trade(
                    timestamp=t["time"] / 1000.0,  # ms -> seconds
                    symbol=coin,
                    side=side,
                    price=price,
                    size=size,
                    size_usd=price * size,
                )
                self._process_trade(trade, venue="hyperliquid")
                self._venue_trade("hyperliquid")
            except (KeyError, ValueError, TypeError) as exc:
                self._venue_parse_error("hyperliquid", exc, t)

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
            close_code = None
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, heartbeat=20) as ws:
                        backoff = 1.0
                        self._venue_connected("binance")
                        # A successful handshake is NOT liveness: in some
                        # regions this socket connects and never delivers a
                        # frame. venue_status() reports that as 'silent'.
                        logger.info(
                            "[binance-trades] Connected, streaming %d symbols "
                            "(awaiting first frame)", len(streams),
                        )

                        async for msg in ws:
                            if not self._running:
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = _json.loads(msg.data)
                                self._handle_binance_trade(data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                        close_code = ws.close_code
                # A clean server-side close is normal churn, not an error —
                # log it as such so the two are distinguishable.
                if self._running:
                    logger.info(
                        "[binance-trades] stream closed by server (code %s), reconnecting in %.1fs",
                        close_code, backoff,
                    )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning(
                    "[binance-trades] %s: %s — reconnecting in %.1fs",
                    type(exc).__name__, exc, backoff,
                )
            finally:
                self._venue_disconnected("binance")

            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _handle_binance_trade(self, raw: dict) -> None:
        """Parse a Binance aggTrade message and process it.

        Frame accounting happens BEFORE the `data` guard so subscription
        acks / error envelopes are counted (a stream of them is not silence),
        and liveness is stamped only AFTER a successful parse so a schema
        change shows up as 'frozen' with a parse-error count, not as a
        healthy venue with a flat CVD.
        """
        self._venue_frame("binance")
        data = raw.get("data") if isinstance(raw, dict) else None
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

            # Skip aggTrades already seen (Binance aggTrade IDs are per-symbol),
            # so a reconnect replay can't double-count into the cumulative CVD.
            agg_id = data.get("a")
            if agg_id is not None:
                key = (symbol, agg_id)
                if key in self._seen_binance_ids:
                    return
                self._seen_binance_ids[key] = None
                while len(self._seen_binance_ids) > MAX_SEEN_IDS:
                    self._seen_binance_ids.popitem(last=False)

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
            self._process_trade(trade, venue="binance")
            self._venue_trade("binance")
        except (KeyError, ValueError, TypeError) as exc:
            self._venue_parse_error("binance", exc, data)

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
        self.cumulative_cvd_hl[symbol] = 0.0
        self.cumulative_cvd_binance[symbol] = 0.0
        self.recent_trades[symbol] = deque(maxlen=100)
