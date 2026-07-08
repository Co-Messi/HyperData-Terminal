from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Callable

import aiohttp

from config.settings import DEFAULT_SYMBOLS

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
            "note": "Real liquidation-orders feed across all SWAP instruments.",
        },
        "hyperliquid": {
            "method": "heuristic",
            "note": (
                f"Hyperliquid has no liquidation feed; events are inferred from "
                f"trades >= ${HL_LIQUIDATION_MIN_USD:,.0f} and may include "
                f"non-liquidation fills."
            ),
        },
    }


class ExchangeConnection:
    MAX_BACKOFF = 60.0
    # After this many consecutive failed connections, escalate once to ERROR
    # and demote further reconnect chatter to debug. We deliberately keep
    # retrying (at MAX_BACKOFF) rather than stopping: a market-data feed that
    # permanently kills itself during a long venue outage never recovers,
    # and one handshake per minute is negligible load.
    FAILURE_ESCALATION_THRESHOLD = 20

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

    async def start(self) -> None:
        self._running = True
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._run_loop(), name=f"ws-{self.name}")

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
            try:
                logger.info("[%s] connecting to %s", self.name, self.ws_url)
                async with self._session.ws_connect(self.ws_url, heartbeat=20) as ws:
                    self._ws = ws
                    self._backoff = 1.0
                    self.consecutive_failures = 0
                    logger.info("[%s] connected", self.name)
                    await self._on_connected(ws)
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._on_message(json.loads(msg.data))
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                logger.warning("[%s] connection closed, reconnecting...", self.name)
            except asyncio.CancelledError:
                return
            except Exception:
                self.consecutive_failures += 1
                if self.consecutive_failures == self.FAILURE_ESCALATION_THRESHOLD:
                    logger.error(
                        "[%s] %d consecutive connection failures — endpoint looks "
                        "dead/deprecated; will keep retrying every %.0fs quietly",
                        self.name, self.consecutive_failures, self.MAX_BACKOFF,
                    )
                elif self.consecutive_failures < self.FAILURE_ESCALATION_THRESHOLD:
                    logger.exception("[%s] connection error", self.name)
                else:
                    logger.debug("[%s] connection error (%d consecutive)",
                                 self.name, self.consecutive_failures)

            if self._running:
                if self.consecutive_failures < self.FAILURE_ESCALATION_THRESHOLD:
                    logger.info("[%s] reconnecting in %.1fs", self.name, self._backoff)
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, self.MAX_BACKOFF)

    async def _on_connected(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        pass

    async def _on_message(self, data: Any) -> None:
        pass


class BinanceConnection(ExchangeConnection):
    def __init__(self, feed: LiquidationFeed):
        super().__init__(
            name="binance",
            ws_url="wss://fstream.binance.com/ws/!forceOrder@arr",
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
        # Bybit v5 sends `data` as a list of records (older topics used a
        # single dict) — accept both, and drop malformed records individually
        # so schema drift can never raise out of the WS loop and reconnect.
        payload = data["data"]
        records = payload if isinstance(payload, list) else [payload]
        for d in records:
            try:
                # v5 allLiquidation uses short keys (p/v/S/s/T); the long
                # names cover the legacy `liquidation` topic shape.
                price = float(d.get("price") or d.get("p") or 0)
                qty = float(d.get("qty") or d.get("size") or d.get("v") or 0)
                side_raw = d.get("side") or d.get("S") or ""  # "Sell" = long liquidated
                symbol_raw = d.get("symbol") or d.get("s") or ""
                ts_ms = int(d.get("updatedTime") or d.get("T") or 0)
                event = LiquidationEvent(
                    timestamp=ts_ms / 1000.0,
                    exchange="bybit",
                    symbol=normalize_symbol(str(symbol_raw), "bybit"),
                    side="long" if side_raw == "Sell" else "short",
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
    def __init__(self, feed: LiquidationFeed):
        super().__init__(
            name="okx",
            ws_url="wss://ws.okx.com:8443/ws/v5/public",
            feed=feed,
        )

    async def _on_connected(self, ws: aiohttp.ClientWebSocketResponse) -> None:
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
                    qty = float(det.get("sz", 0) or 0)
                    side_raw = str(det.get("side", "")).lower()
                    ts_raw = det.get("ts", "0") or "0"
                    event = LiquidationEvent(
                        timestamp=int(ts_raw) / 1000.0,
                        exchange="okx",
                        symbol=normalize_symbol(inst_id, "okx"),
                        side="long" if side_raw == "sell" else "short",
                        size_usd=price * qty,
                        price=price,
                        quantity=qty,
                    )
                except (KeyError, TypeError, ValueError, AttributeError):
                    self.feed.record_parse_error("okx", det)
                    continue
                await self.feed.emit(event)


class HyperliquidConnection:
    """Hyperliquid doesn't have a dedicated liquidation feed.
    We detect liquidations by monitoring trades on the WebSocket and checking
    for trades where addresses close to liquidation (from position scanner)
    appear as counterparties. Also uses large trade heuristics.
    """
    API_URL = "https://api.hyperliquid.xyz/info"
    LARGE_TRADE_USD = HL_LIQUIDATION_MIN_USD  # Min size to flag as potential liquidation
    POLL_INTERVAL = 5.0

    def __init__(self, feed: LiquidationFeed):
        self.name = "hyperliquid"
        self.feed = feed
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._ws_task: asyncio.Task | None = None
        # Track mid prices to detect aggressive fills
        self._mid_prices: dict[str, float] = {}
        self._seen_tids: OrderedDict = OrderedDict()

    async def start(self) -> None:
        self._running = True
        self._session = aiohttp.ClientSession()
        # Run both: WS for trade monitoring + REST poll for price context
        self._ws_task = asyncio.create_task(self._ws_loop(), name="ws-hyperliquid")
        self._task = asyncio.create_task(self._price_poll(), name="poll-hl-prices")

    async def stop(self) -> None:
        self._running = False
        for t in [self._ws_task, self._task]:
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        if self._session and not self._session.closed:
            await self._session.close()

    async def _price_poll(self) -> None:
        """Poll mid prices to have context for liquidation detection."""
        consecutive_failures = 0
        while self._running:
            try:
                async with self._session.post(
                    self.API_URL, json={"type": "allMids"}, timeout=HTTP_TIMEOUT
                ) as resp:
                    if resp.status == 200:
                        self._mid_prices = {k: float(v) for k, v in (await resp.json()).items()}
                        consecutive_failures = 0
            except asyncio.CancelledError:
                return
            except Exception:
                consecutive_failures += 1
                # Silent-pass hid outages for hours; warn once it looks real.
                if consecutive_failures in (3, 10) or consecutive_failures % 100 == 0:
                    logger.warning(
                        "[hyperliquid] price poll failing (%d consecutive)",
                        consecutive_failures, exc_info=True,
                    )
            await asyncio.sleep(self.POLL_INTERVAL)

    async def _ws_loop(self) -> None:
        """Connect to trades WS and detect liquidation-like events."""
        coins = DEFAULT_SYMBOLS[:16]
        backoff = 1.0

        while self._running:
            try:
                async with self._session.ws_connect("wss://api.hyperliquid.xyz/ws", heartbeat=20) as ws:
                    backoff = 1.0
                    for coin in coins:
                        await ws.send_json({
                            "method": "subscribe",
                            "subscription": {"type": "trades", "coin": coin}
                        })
                    logger.info("[hyperliquid] subscribed to %d trade feeds", len(coins))

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("channel") == "trades":
                                await self._process_trades(data.get("data", []))
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[hyperliquid] WS error")
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _process_trades(self, trades: list[dict]) -> None:
        """Detect liquidation-like trades from the HL trade stream.
        Heuristic: Large trades that move price aggressively are likely liquidations.
        """
        for t in trades:
            try:
                tid = t.get("tid", 0)
                if tid in self._seen_tids:
                    continue
                self._seen_tids[tid] = None
                while len(self._seen_tids) > 100_000:
                    self._seen_tids.popitem(last=False)  # Remove oldest

                coin = t.get("coin", "")
                price = float(t.get("px", 0) or 0)
                qty = float(t.get("sz", 0) or 0)
                ts_ms = int(t.get("time", 0) or 0)
                side = t.get("side", "")  # "B" = buyer taker, "A" = seller taker
            except (TypeError, ValueError, AttributeError):
                self.feed.record_parse_error("hyperliquid", t)
                continue
            size_usd = price * qty

            # Only flag large trades as potential liquidations
            if size_usd < self.LARGE_TRADE_USD:
                continue

            # Side logic: "A" (ask/sell taker) = someone is aggressively selling = long liquidation
            # "B" (bid/buy taker) = someone aggressively buying = short liquidation
            event = LiquidationEvent(
                timestamp=ts_ms / 1000.0,
                exchange="hyperliquid",
                symbol=coin,
                side="long" if side == "A" else "short",
                size_usd=size_usd,
                price=price,
                quantity=qty,
                confirmed=False,
            )
            await self.feed.emit(event)


@dataclass
class _TimeWindow:
    count: int = 0
    volume_usd: float = 0.0
    long_count: int = 0
    short_count: int = 0
    long_volume: float = 0.0
    short_volume: float = 0.0


class LiquidationFeed:
    def __init__(self, max_events: int = 10_000):
        self.events: deque[LiquidationEvent] = deque(maxlen=max_events)
        self.callbacks: list[Callable[[LiquidationEvent], Any]] = []
        self._connections: list[ExchangeConnection | HyperliquidConnection] = []
        self._lock = asyncio.Lock()
        self._running = False
        # Per-exchange count of records dropped at the parse boundary.
        # Surfaced via get_stats() so schema drift is visible, not silent.
        self.parse_errors: dict[str, int] = {}

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
        self._connections = [
            BinanceConnection(self),       # Sampled: forceOrder feed throttled to ~1/symbol/sec by Binance
            BybitConnection(self),         # Confirmed: allLiquidation v5 feed, top-N symbols only
            OKXConnection(self),           # Confirmed: liquidation-orders feed, all SWAP
            HyperliquidConnection(self),   # Heuristic: inferred from large trades (not confirmed)
        ]
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

    async def emit(self, event: LiquidationEvent) -> None:
        """Public method to inject a liquidation event into the feed."""
        await self._dispatch(event)

    async def _dispatch(self, event: LiquidationEvent) -> None:
        async with self._lock:
            self.events.append(event)
        for cb in self.callbacks:
            try:
                result = cb(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("callback error")

    def get_stats(self, window_minutes: int = 60) -> dict[str, Any]:
        cutoff = time.time() - (window_minutes * 60)
        totals = _TimeWindow()
        by_exchange: dict[str, _TimeWindow] = {}
        by_symbol: dict[str, _TimeWindow] = {}
        confirmed_count = 0
        heuristic_count = 0
        confirmed_volume_usd = 0.0
        heuristic_volume_usd = 0.0
        _coverage = exchange_coverage()

        for ev in self.events:
            if ev.timestamp < cutoff:
                continue

            totals.count += 1
            totals.volume_usd += ev.size_usd
            if getattr(ev, "confirmed", True):
                confirmed_count += 1
                confirmed_volume_usd += ev.size_usd
            else:
                heuristic_count += 1
                heuristic_volume_usd += ev.size_usd
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
    ) -> list[LiquidationEvent]:
        cutoff = time.time() - (minutes * 60)
        results: list[LiquidationEvent] = []
        for ev in reversed(self.events):
            if ev.timestamp < cutoff:
                continue
            if symbol and ev.symbol != symbol.upper():
                continue
            if exchange and ev.exchange != exchange.lower():
                continue
            results.append(ev)
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
