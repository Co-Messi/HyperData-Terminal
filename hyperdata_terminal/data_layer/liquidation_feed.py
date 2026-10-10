from __future__ import annotations

import asyncio
import itertools
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import aiohttp

from hyperdata_terminal.config.settings import DEFAULT_SYMBOLS

logger = logging.getLogger(__name__)


@dataclass
class LiquidationEvent:
    timestamp: float
    exchange: str
    symbol: str
    side: str
    size_usd: float
    price: float
    quantity: float
    confirmed: bool = True  # False for heuristic-based detection (Hyperliquid)


def normalize_symbol(raw: str, exchange: str) -> str:
    raw = raw.upper()
    if exchange == "okx":
        return raw.split("-")[0]
    for suffix in ("USDT", "USD", "PERP"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
    return raw


# Tracked symbols we subscribe to on Bybit's allLiquidation feed. Bybit caps the
# number of args per subscribe request, so we batch (see BYBIT_MAX_ARGS) rather
# than truncate the list.
BYBIT_SYMBOL_LIMIT = 50
BYBIT_MAX_ARGS = 10
# Heuristic threshold: HL has no liquidation feed, so we infer liquidations from
# trades at least this large (USD). These are estimates, not confirmed events.
HL_LIQUIDATION_MIN_USD = 10_000

# Deadline for REST polls (price context); a hung endpoint must not wedge the
# poll loop. Split connect/read so a slow handshake can't eat the budget.
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=3, sock_connect=3, sock_read=5)


def exchange_coverage() -> dict[str, dict[str, str]]:
    """Per-exchange description of HOW liquidations are collected, so consumers
    never mistake a throttled/heuristic sample for a complete census.

    method:
      - "confirmed": real exchange liquidation feed (may be scope-limited)
      - "sampled":   real feed, but throttled/undercounted at the source
      - "heuristic": inferred (not a real liquidation feed)
    """
    return {
        "binance": {
            "method": "sampled",
            "note": (
                "Binance !forceOrder stream is throttled to ~1 liquidation per "
                "symbol per second; large cascades are undercounted at the source."
            ),
        },
        "bybit": {
            "method": "confirmed",
            "note": (
                f"Real allLiquidation v5 feed across the {BYBIT_SYMBOL_LIMIT} "
                f"tracked symbols (those with a Bybit linear perp)."
            ),
        },
        "okx": {
            "method": "confirmed",
            "note": (
                "Real liquidation-orders feed across all SWAP instruments, sized "
                "by each swap's contract value; a swap missing from OKX's "
                "instrument list is dropped (see unsized_drops)."
            ),
        },
        "hyperliquid": {
            "method": "partial",
            "note": (
                "Hyperliquid has no public liquidation feed. Confirmed events are "
                "liquidations an HLP vault absorbed (Hyperliquid flags those fills; "
                "polled every ~2 min), so liquidations filled by other traders are "
                f"missed. Separately, trades >= ${HL_LIQUIDATION_MIN_USD:,.0f} on the "
                "streamed symbols are "
                "reported as estimated 'large prints' (confirmed=false): most are "
                "ordinary trades, so they are excluded from confirmed totals."
            ),
        },
    }


# A confirmed venue that has not been connected for this long (never since
# start, or since its socket dropped) counts as down: the liquidation feed
# reads 'partial' and the health checks name it.
VENUE_DOWN_AFTER_SECONDS = 120.0
# Each venue's all-market liquidation stream normally delivers several
# frames a minute; a socket that is open but has delivered nothing for this
# long is 'silent' (a dead route, as Binance's legacy /ws path became).
VENUE_SILENT_AFTER_SECONDS = 1800.0


class ExchangeConnection:
    MAX_BACKOFF = 60.0
    # After this many consecutive failed connections, escalate once to ERROR
    # and demote further reconnect chatter to debug. We deliberately keep
    # retrying (at MAX_BACKOFF) rather than stopping: a market-data feed that
    # permanently kills itself during a long venue outage never recovers,
    # and one handshake per minute is negligible load.
    FAILURE_ESCALATION_THRESHOLD = 20
    # A connection must stay up this long before the backoff resets. A venue
    # that accepts the handshake and then drops the socket (a rejected
    # subscription, a regional edge) used to reset it on every connect and
    # so reconnected every second forever, logging a warning each time.
    HEALTHY_AFTER_SECONDS = 30.0

    def __init__(self, name: str, ws_url: str, feed: LiquidationFeed):
        self.name = name
        self.ws_url = ws_url
        self.feed = feed
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._running = False
        self._backoff = 1.0
        self.consecutive_failures = 0
        # Liveness, read by LiquidationFeed.venue_health().
        self.started_at = 0.0
        self.connected = False
        self.connected_at = 0.0
        self.disconnected_at = 0.0
        self.last_frame_at = 0.0
        self.last_event_at = 0.0
        self.last_error = ""
        self.connects = 0
        self.frames = 0

    async def start(self) -> None:
        self._running = True
        self.started_at = time.time()
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._run_loop(), name=f"ws-{self.name}")

    def status(self, now: float | None = None) -> tuple[str, str]:
        """(status, reason): ok, connecting, silent or down."""
        now = time.time() if now is None else now
        if self.connected:
            quiet_since = max(self.last_frame_at, self.connected_at)
            if now - quiet_since > VENUE_SILENT_AFTER_SECONDS:
                return "silent", f"connected but no frame for {now - quiet_since:.0f}s"
            return "ok", f"connected {now - self.connected_at:.0f}s"
        down_since = self.disconnected_at or self.started_at
        err = f" ({self.last_error})" if self.last_error else ""
        if not down_since or now - down_since <= VENUE_DOWN_AFTER_SECONDS:
            return "connecting", f"not connected yet{err}"
        never = "never connected" if not self.connects else f"disconnected {now - down_since:.0f}s ago"
        return "down", f"{never}{err}"

    @staticmethod
    def _describe(exc: BaseException) -> str:
        status = getattr(exc, "status", None)
        if isinstance(status, int):
            return f"HTTP {status}"
        return type(exc).__name__

    async def stop(self) -> None:
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()

    async def _run_loop(self) -> None:
        while self._running:
            opened_at = 0.0
            error: Exception | None = None
            try:
                logger.debug("[%s] connecting to %s", self.name, self.ws_url)
                async with self._session.ws_connect(self.ws_url, heartbeat=20) as ws:
                    self._ws = ws
                    opened_at = time.time()
                    self.connected = True
                    self.connected_at = opened_at
                    self.connects += 1
                    self.last_error = ""
                    if self.consecutive_failures < self.FAILURE_ESCALATION_THRESHOLD:
                        logger.info("[%s] connected", self.name)
                    await self._on_connected(ws)
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self.frames += 1
                            self.last_frame_at = time.time()
                            # One malformed frame is counted and skipped; it
                            # must not tear down a healthy socket.
                            try:
                                payload = json.loads(msg.data)
                            except ValueError:
                                self.feed.record_parse_error(self.name, msg.data)
                                continue
                            await self._on_message(payload)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                self._mark_disconnected("closed by server")
            except asyncio.CancelledError:
                self._mark_disconnected("stopped")
                return
            except Exception as exc:
                error = exc
                self._mark_disconnected(self._describe(exc))

            if opened_at and time.time() - opened_at >= self.HEALTHY_AFTER_SECONDS:
                # It stayed up: server side churn, reconnect promptly.
                self._backoff = 1.0
                self.consecutive_failures = 0
            else:
                self.consecutive_failures += 1
            self._log_disconnect(error)

            if self._running:
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, self.MAX_BACKOFF)

    def _log_disconnect(self, error: Exception | None) -> None:
        """Warn while it might be transient, escalate once, then go quiet."""
        n = self.consecutive_failures
        what = f"connection error ({self._describe(error)})" if error else "connection closed"
        if n == self.FAILURE_ESCALATION_THRESHOLD:
            logger.error(
                "[%s] %d failed or short lived connections in a row; the endpoint looks "
                "dead or blocked. Retrying every %.0fs quietly", self.name, n, self.MAX_BACKOFF,
            )
        elif n < self.FAILURE_ESCALATION_THRESHOLD:
            logger.warning("[%s] %s, reconnecting in %.1fs", self.name, what, self._backoff)
        else:
            logger.debug("[%s] %s (%d in a row)", self.name, what, n)

    def _mark_disconnected(self, reason: str) -> None:
        if self.connected:
            self.disconnected_at = time.time()
        self.connected = False
        self.last_error = reason

    async def _on_connected(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        pass

    async def _on_message(self, data: Any) -> None:
        pass


# Binance USD-M market streams are served under /market ("Connect to
# wss://fstream.binance.com/market/stream", USD-M WebSocket Market Streams
# docs). The legacy /ws and /stream paths still accept the handshake but,
# probed on 2026-10-10, deliver no frames at all.
BINANCE_MARKET_WS = "wss://fstream.binance.com/market/ws"
BINANCE_MARKET_STREAM = "wss://fstream.binance.com/market/stream"


class BinanceConnection(ExchangeConnection):
    def __init__(self, feed: LiquidationFeed):
        super().__init__(
            name="binance",
            ws_url=f"{BINANCE_MARKET_WS}/!forceOrder@arr",
            feed=feed,
        )

    async def _on_message(self, data: Any) -> None:
        # `!forceOrder@arr` frames normally carry one object, but the `@arr`
        # family can batch events into a JSON array — handle both shapes so
        # an array frame is parsed instead of silently failing a dict check.
        records = data if isinstance(data, list) else [data]
        for rec in records:
            if not isinstance(rec, dict) or rec.get("e") != "forceOrder":
                continue
            # Exchange payloads are untrusted: one malformed record must not
            # raise out of the connection loop and trigger a reconnect.
            try:
                o = rec["o"]
                price = float(o["p"])
                qty = float(o["q"])
                side_raw = str(o["S"]).upper()
                event = LiquidationEvent(
                    timestamp=float(o["T"]) / 1000.0,
                    exchange="binance",
                    symbol=normalize_symbol(str(o["s"]), "binance"),
                    side="long" if side_raw == "SELL" else "short",
                    size_usd=price * qty,
                    price=price,
                    quantity=qty,
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                self.feed.record_parse_error("binance", rec)
                continue
            await self.feed.emit(event)


class BybitConnection(ExchangeConnection):
    """Bybit v5 allLiquidation feed — confirmed real liquidation events."""

    def __init__(self, feed: LiquidationFeed):
        super().__init__(
            name="bybit",
            ws_url="wss://stream.bybit.com/v5/public/linear",
            feed=feed,
        )

    async def _on_connected(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        topics = [f"allLiquidation.{s}USDT" for s in DEFAULT_SYMBOLS[:BYBIT_SYMBOL_LIMIT]]
        # Bybit v5 limits args per subscribe request — send in chunks so we can
        # cover the full tracked set instead of only the first handful. Topics
        # for symbols without a Bybit linear perp just get a harmless error reply.
        for i in range(0, len(topics), BYBIT_MAX_ARGS):
            await ws.send_json({"op": "subscribe", "args": topics[i:i + BYBIT_MAX_ARGS]})
        logger.info("[bybit] subscribed to %d allLiquidation topics", len(topics))

    async def _on_message(self, data: Any) -> None:
        if not isinstance(data, dict) or "data" not in data:
            return
        if not str(data.get("topic", "")).startswith("allLiquidation."):
            return
        # v5 sends `data` as a list of records; drop malformed records
        # individually so schema drift can never raise out of the WS loop.
        payload = data["data"]
        records = payload if isinstance(payload, list) else [payload]
        for d in records:
            try:
                # allLiquidation keys only (this connection subscribes to
                # nothing else): T time (ms), s symbol, S the side of the
                # POSITION that was liquidated ("When you receive a Buy
                # update, this means that a long position has been
                # liquidated", Bybit's docs), v executed size, p bankruptcy
                # price. Captured frames agree: every Buy executed below the
                # mark (a long closed by a sell), every Sell above it.
                side_raw = d["S"]
                if side_raw not in ("Buy", "Sell"):
                    raise ValueError(f"unknown side {side_raw!r}")
                price = float(d["p"])
                qty = float(d["v"])
                event = LiquidationEvent(
                    timestamp=int(d["T"]) / 1000.0,
                    exchange="bybit",
                    symbol=normalize_symbol(str(d["s"]), "bybit"),
                    side="long" if side_raw == "Buy" else "short",
                    size_usd=price * qty,
                    price=price,
                    quantity=qty,
                    confirmed=True,
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                self.feed.record_parse_error("bybit", d)
                continue
            await self.feed.emit(event)


class OKXConnection(ExchangeConnection):
    """OKX liquidation-orders for every SWAP.

    OKX reports liquidation size in contracts, not coins: BTC-USDT-SWAP is
    0.01 BTC a contract, DOGE-USDT-SWAP 1000 DOGE, and the inverse
    BTC-USD-SWAP $100. Sizes are converted with each swap's contract value
    from OKX's instrument list; a swap missing from it is dropped, never
    guessed (contracts times price was off by 100x for BTC).
    """

    INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    SPEC_RETRY_SECONDS = 60       # while no contract sizes are loaded
    SPEC_REFRESH_SECONDS = 600    # when a swap is missing (a new listing)

    def __init__(self, feed: LiquidationFeed):
        super().__init__(
            name="okx",
            ws_url="wss://ws.okx.com:8443/ws/v5/public",
            feed=feed,
        )
        # instId -> (ctType, contract value): base coin for linear, USD for inverse
        self._contracts: dict[str, tuple[str, float]] = {}
        self._specs_attempted_at = 0.0
        self._spec_task: asyncio.Task | None = None
        self._unsized_logged: set[str] = set()

    async def _load_contracts(self) -> None:
        self._specs_attempted_at = time.time()
        if self._session is None or self._session.closed:
            return
        try:
            async with self._session.get(
                self.INSTRUMENTS_URL, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    logger.warning("[okx] instrument list returned HTTP %d", resp.status)
                    return
                body = await resp.json()
        except Exception as exc:
            logger.warning("[okx] could not load contract sizes: %r", exc)
            return
        specs: dict[str, tuple[str, float]] = {}
        data = body.get("data") if isinstance(body, dict) else None
        for inst in data if isinstance(data, list) else []:
            try:
                value = float(inst["ctVal"]) * float(inst.get("ctMult") or 1)
                if value > 0:
                    specs[str(inst["instId"])] = (str(inst["ctType"]), value)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        if specs:
            self._contracts = specs
            logger.info("[okx] contract sizes loaded for %d swaps", len(specs))

    def _maybe_reload_contracts(self) -> None:
        wait = self.SPEC_REFRESH_SECONDS if self._contracts else self.SPEC_RETRY_SECONDS
        if time.time() - self._specs_attempted_at < wait:
            return
        if self._spec_task is not None and not self._spec_task.done():
            return
        self._specs_attempted_at = time.time()
        self._spec_task = asyncio.create_task(self._load_contracts(), name="okx-contracts")

    def _size(self, inst_id: str, price: float, contracts: float) -> tuple[float, float] | None:
        """(size_usd, coin quantity) for `contracts` of `inst_id`, or None if unknown."""
        spec = self._contracts.get(inst_id)
        if spec is None:
            return None
        ct_type, value = spec
        if ct_type == "inverse":
            size_usd = contracts * value
            return size_usd, (size_usd / price if price > 0 else 0.0)
        qty = contracts * value
        return qty * price, qty

    async def stop(self) -> None:
        if self._spec_task is not None and not self._spec_task.done():
            self._spec_task.cancel()
            await asyncio.gather(self._spec_task, return_exceptions=True)
        await super().stop()

    async def _on_connected(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        if not self._contracts:
            await self._load_contracts()
        await ws.send_json({
            "op": "subscribe",
            "args": [{"channel": "liquidation-orders", "instType": "SWAP"}],
        })
        logger.info("[okx] subscribed to liquidation-orders SWAP")

    async def _on_message(self, data: Any) -> None:
        if not isinstance(data, dict) or "data" not in data:
            return
        if not isinstance(data["data"], list):
            self.feed.record_parse_error("okx", data)
            return

        for d in data["data"]:
            # Drop malformed records individually — one bad detail must not
            # kill the whole message or the connection loop.
            try:
                details = d.get("details", [])
                inst_id = d.get("instId", "")
                if not isinstance(details, list):
                    self.feed.record_parse_error("okx", d)
                    continue
            except AttributeError:
                self.feed.record_parse_error("okx", d)
                continue
            for det in details:
                try:
                    price = float(det.get("bkPx", 0) or 0)
                    contracts = float(det.get("sz", 0) or 0)
                    side_raw = str(det.get("side", "")).lower()
                    ts_raw = det.get("ts", "0") or "0"
                    sized = self._size(str(inst_id), price, contracts)
                    if sized is None:
                        self._drop_unsized(str(inst_id))
                        continue
                    size_usd, qty = sized
                    event = LiquidationEvent(
                        timestamp=int(ts_raw) / 1000.0,
                        exchange="okx",
                        symbol=normalize_symbol(inst_id, "okx"),
                        side="long" if side_raw == "sell" else "short",
                        size_usd=size_usd,
                        price=price,
                        quantity=qty,
                    )
                except (KeyError, TypeError, ValueError, AttributeError):
                    self.feed.record_parse_error("okx", det)
                    continue
                await self.feed.emit(event)

    def _drop_unsized(self, inst_id: str) -> None:
        self.feed.record_unsized_drop("okx")
        if inst_id not in self._unsized_logged:
            self._unsized_logged.add(inst_id)
            logger.warning("[okx] no contract size for %s; its liquidations are dropped "
                           "until the instrument list has it", inst_id)
        self._maybe_reload_contracts()


@dataclass
class _TimeWindow:
    count: int = 0
    volume_usd: float = 0.0
    long_count: int = 0
    short_count: int = 0
    long_volume: float = 0.0
    short_volume: float = 0.0


class LiquidationFeed:
    # Every window is computed from these in-memory buffers, so the longest
    # window anyone may ask for is one day; get_stats() reports how much of
    # it is actually covered (process uptime, buffer evictions).
    MAX_WINDOW_MINUTES = 1440

    def __init__(self, max_events: int = 50_000):
        # Confirmed liquidations. Estimated events (Hyperliquid large prints)
        # get their own buffer: they arrive far faster, and sharing one deque
        # let them evict confirmed liquidations, silently truncating every
        # long window.
        self.events: deque[LiquidationEvent] = deque(maxlen=max_events)
        self.estimated_events: deque[LiquidationEvent] = deque(maxlen=max_events)
        # Nothing before this moment was collected. Reset by start().
        self.started_at: float = time.time()
        # Newest timestamp among events a full buffer has evicted, per buffer.
        # Arrival order is not time order (Hyperliquid confirmed events come
        # in about two minutes late), so this is a running max, not the
        # timestamp of whatever sits at the front of the deque.
        self.evicted_through: float = 0.0
        self.estimated_evicted_through: float = 0.0
        self.evicted_count: int = 0
        self.callbacks: list[Callable[[LiquidationEvent], Any]] = []
        self._connections: list[ExchangeConnection] = []
        self._lock = asyncio.Lock()
        self._running = False
        # Per-exchange count of records dropped at the parse boundary.
        # Surfaced via get_stats() so schema drift is visible, not silent.
        self.parse_errors: dict[str, int] = {}
        # Well formed records that could not be sized (an OKX swap missing
        # from the instrument list), also surfaced via get_stats().
        self.unsized_drops: dict[str, int] = {}

    # Venues whose real liquidation feeds this object runs (Hyperliquid's
    # confirmed events come from the HLP tracker, tracked as hlp_status).
    CONFIRMED_VENUES = ("binance", "bybit", "okx")

    def venue_health(self, now: float | None = None) -> dict[str, dict]:
        """Per-venue connection state of the confirmed liquidation feeds."""
        now = time.time() if now is None else now
        out: dict[str, dict] = {}
        for conn in self._connections:
            status, reason = conn.status(now)
            out[conn.name] = {
                "status": status,
                "reason": reason,
                "connected": conn.connected,
                "connects": conn.connects,
                "frames": conn.frames,
                "last_frame_age_seconds": round(now - conn.last_frame_at, 1) if conn.last_frame_at else None,
                "last_event_age_seconds": round(now - conn.last_event_at, 1) if conn.last_event_at else None,
                "last_error": conn.last_error or None,
            }
        return out

    def venues_down(self, now: float | None = None) -> list[str]:
        """Confirmed venues that are down or silent, as 'name (reason)'."""
        return [
            f"{name} ({info['reason']})"
            for name, info in self.venue_health(now).items()
            if info["status"] in ("down", "silent")
        ]

    def feed_status(self, now: float | None = None) -> str | None:
        """connecting / connected / partial / error, from the venues alone
        (None before start(): no venue connections exist yet)."""
        health = self.venue_health(now)
        if not health:
            return None
        statuses = [info["status"] for info in health.values()]
        if all(s == "ok" for s in statuses):
            return "connected"
        if any(s == "ok" for s in statuses):
            return "connecting" if all(s in ("ok", "connecting") for s in statuses) else "partial"
        if all(s == "connecting" for s in statuses):
            return "connecting"
        return "error" if all(s in ("down", "silent") for s in statuses) else "connecting"

    def record_unsized_drop(self, exchange: str) -> None:
        self.unsized_drops[exchange] = self.unsized_drops.get(exchange, 0) + 1

    def record_parse_error(self, exchange: str, payload: Any = None) -> None:
        """Count a malformed record dropped at the parse boundary."""
        count = self.parse_errors.get(exchange, 0) + 1
        self.parse_errors[exchange] = count
        # Log the first few and then sample, so a schema change is visible
        # without a malformed-message flood drowning the logs.
        if count <= 3 or count % 1000 == 0:
            logger.warning(
                "[%s] dropped malformed record (%d total): %.300s",
                exchange, count, payload,
            )

    async def start(self) -> None:
        logger.info("starting liquidation feed")
        self._running = True
        self.started_at = time.time()
        self._connections = [
            BinanceConnection(self),       # Sampled: forceOrder feed throttled to ~1/symbol/sec by Binance
            BybitConnection(self),         # Confirmed: allLiquidation v5 feed, top-N symbols only
            OKXConnection(self),           # Confirmed: liquidation-orders feed, all SWAP
        ]
        # Hyperliquid: confirmed events come from the HLP tracker and
        # estimated large prints from the order flow engine's trade stream
        # (hyperliquid_large_print), both wired by the hub.
        for conn in self._connections:
            await conn.start()
        logger.info("all exchange connections started")

    async def stop(self) -> None:
        logger.info("stopping liquidation feed")
        self._running = False
        for conn in self._connections:
            await conn.stop()
        self._connections.clear()
        logger.info("liquidation feed stopped")

    def on_liquidation(self, callback: Callable[[LiquidationEvent], Any]) -> None:
        self.callbacks.append(callback)

    @staticmethod
    def hyperliquid_large_print(trade) -> LiquidationEvent | None:
        """An ESTIMATED event for a Hyperliquid trade of at least
        HL_LIQUIDATION_MIN_USD (Hyperliquid has no liquidation feed; most
        such trades are ordinary). `trade` is the order flow engine's parsed
        Trade, already deduplicated by (coin, tid). A taker sell is read as a
        long being closed, a taker buy as a short."""
        if trade.size_usd < HL_LIQUIDATION_MIN_USD:
            return None
        return LiquidationEvent(
            timestamp=trade.timestamp,
            exchange="hyperliquid",
            symbol=trade.symbol,
            side="long" if trade.side == "sell" else "short",
            size_usd=trade.size_usd,
            price=trade.price,
            quantity=trade.size,
            confirmed=False,
        )

    async def emit(self, event: LiquidationEvent) -> None:
        """Public method to inject a liquidation event into the feed."""
        # float() accepts "nan", "inf" and negatives; one such event would
        # poison every total in its window.
        if not (math.isfinite(event.size_usd) and event.size_usd > 0
                and math.isfinite(event.price) and event.price > 0
                and math.isfinite(event.quantity)):
            self.record_parse_error(event.exchange, event)
            return
        await self._dispatch(event)

    async def _dispatch(self, event: LiquidationEvent) -> None:
        for conn in self._connections:
            if conn.name == event.exchange:
                conn.last_event_at = time.time()
        async with self._lock:
            if getattr(event, "confirmed", True):
                if len(self.events) == self.events.maxlen:
                    self.evicted_through = max(self.evicted_through, self.events[0].timestamp)
                    self.evicted_count += 1
                self.events.append(event)
            else:
                if len(self.estimated_events) == self.estimated_events.maxlen:
                    self.estimated_evicted_through = max(
                        self.estimated_evicted_through, self.estimated_events[0].timestamp,
                    )
                self.estimated_events.append(event)
        for cb in self.callbacks:
            try:
                result = cb(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("callback error")

    def window_coverage(
        self, window_minutes: float, include_estimated: bool = False, now: float | None = None,
    ) -> dict[str, Any]:
        """How much of the last ``window_minutes`` the in-memory buffers cover.

        Two things make a window partial: the process has not been running
        for the whole window, and a full buffer has evicted events inside it.
        ``covered_since`` is the earliest moment from which every collected
        event is still held; ``window_coverage`` is the share of the window
        after it (1.0 means the totals are a full window).
        """
        now = time.time() if now is None else now
        window_s = max(float(window_minutes) * 60, 1e-9)
        cutoff = now - window_s
        evicted = self.evicted_through
        if include_estimated:
            evicted = max(evicted, self.estimated_evicted_through)
        truncated = evicted >= cutoff
        covered_since = max(self.started_at, evicted if truncated else 0.0)
        coverage = 1.0 if covered_since <= cutoff else max(0.0, (now - covered_since) / window_s)
        return {
            "window_coverage": round(min(1.0, coverage), 6),
            "covered_since": max(covered_since, cutoff),
            "truncated": truncated,
        }

    def get_stats(
        self, window_minutes: int = 60, include_estimated: bool = True, symbol: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate liquidations in the window, optionally for one symbol.

        include_estimated=False drops heuristic events (Hyperliquid large
        prints) from every total, side and breakdown; confirmed_* and
        heuristic_* are reported either way. Every display, alert and
        strategy passes False; the API and MCP default to False too.

        ``window_coverage``, ``covered_since`` and ``truncated`` say whether
        the totals really span the window (see window_coverage()).
        """
        now = time.time()
        cutoff = now - (window_minutes * 60)
        only = symbol.upper() if symbol else None
        totals = _TimeWindow()
        by_exchange: dict[str, _TimeWindow] = {}
        by_symbol: dict[str, _TimeWindow] = {}
        confirmed_count = 0
        heuristic_count = 0
        confirmed_volume_usd = 0.0
        heuristic_volume_usd = 0.0
        _coverage = exchange_coverage()

        for ev in itertools.chain(self.events, self.estimated_events):
            if ev.timestamp < cutoff:
                continue
            if only and ev.symbol != only:
                continue

            if getattr(ev, "confirmed", True):
                confirmed_count += 1
                confirmed_volume_usd += ev.size_usd
            else:
                heuristic_count += 1
                heuristic_volume_usd += ev.size_usd
                if not include_estimated:
                    continue
            totals.count += 1
            totals.volume_usd += ev.size_usd
            if ev.side == "long":
                totals.long_count += 1
                totals.long_volume += ev.size_usd
            else:
                totals.short_count += 1
                totals.short_volume += ev.size_usd

            ex_w = by_exchange.setdefault(ev.exchange, _TimeWindow())
            ex_w.count += 1
            ex_w.volume_usd += ev.size_usd

            sym_w = by_symbol.setdefault(ev.symbol, _TimeWindow())
            sym_w.count += 1
            sym_w.volume_usd += ev.size_usd

        return {
            "window_minutes": window_minutes,
            "include_estimated": include_estimated,
            **self.window_coverage(window_minutes, include_estimated, now),
            "total_count": totals.count,
            "total_volume_usd": totals.volume_usd,
            "long_count": totals.long_count,
            "short_count": totals.short_count,
            "long_volume_usd": totals.long_volume,
            "short_volume_usd": totals.short_volume,
            # Liquidation counts/volume are NOT a complete census — see `coverage`.
            # confirmed = from real exchange feeds; heuristic = inferred (HL).
            # Use the confirmed_* figures for anything that must not be inflated
            # by ordinary large HL trades (e.g. cascade alerts).
            "confirmed_count": confirmed_count,
            "heuristic_count": heuristic_count,
            "confirmed_volume_usd": confirmed_volume_usd,
            "heuristic_volume_usd": heuristic_volume_usd,
            "parse_errors": dict(self.parse_errors),
            "unsized_drops": dict(self.unsized_drops),
            "coverage": _coverage,
            "by_exchange": {
                k: {
                    "count": v.count,
                    "volume_usd": v.volume_usd,
                    "method": _coverage.get(k, {}).get("method", "unknown"),
                }
                for k, v in sorted(by_exchange.items())
            },
            "by_symbol": {
                k: {"count": v.count, "volume_usd": v.volume_usd}
                for k, v in sorted(by_symbol.items(), key=lambda x: -x[1].volume_usd)
            },
        }

    def get_recent(
        self,
        minutes: int = 5,
        symbol: str | None = None,
        exchange: str | None = None,
        include_estimated: bool = True,
    ) -> list[LiquidationEvent]:
        """Events in the window, newest first."""
        cutoff = time.time() - (minutes * 60)
        sources = (self.events, self.estimated_events) if include_estimated else (self.events,)
        results: list[LiquidationEvent] = []
        for ev in itertools.chain(*sources):
            if ev.timestamp < cutoff:
                continue
            if symbol and ev.symbol != symbol.upper():
                continue
            if exchange and ev.exchange != exchange.lower():
                continue
            if not include_estimated and not getattr(ev, "confirmed", True):
                continue
            results.append(ev)
        results.sort(key=lambda e: e.timestamp, reverse=True)
        return results


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    feed = LiquidationFeed()

    def on_liq(event: LiquidationEvent) -> None:
        direction = "LONG LIQ" if event.side == "long" else "SHORT LIQ"
        logger.info(
            "%s | %-12s | %-6s | %s | $%,.0f | px=%.2f | qty=%.4f",
            event.exchange.upper(),
            direction,
            event.symbol,
            time.strftime("%H:%M:%S", time.localtime(event.timestamp)),
            event.size_usd,
            event.price,
            event.quantity,
        )

    feed.on_liquidation(on_liq)
    await feed.start()

    try:
        while True:
            await asyncio.sleep(30)
            stats = feed.get_stats(window_minutes=5)
            logger.info(
                "--- 5min stats: %d liqs, $%,.0f volume, %d long / %d short ---",
                stats["total_count"],
                stats["total_volume_usd"],
                stats["long_count"],
                stats["short_count"],
            )
            if stats["by_exchange"]:
                for ex, ex_stats in stats["by_exchange"].items():
                    logger.info(
                        "  %s: %d liqs, $%,.0f",
                        ex, ex_stats["count"], ex_stats["volume_usd"],
                    )
    except asyncio.CancelledError:
        pass
    finally:
        await feed.stop()


if __name__ == "__main__":
    asyncio.run(main())
