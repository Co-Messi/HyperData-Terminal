"""
HyperData REST API v1 + WebSocket streaming.

All REST endpoints are under /v1/. Legacy paths redirect to /v1/.
WebSocket at /v1/ws streams real-time events to subscribed clients.

Usage:
    hub = HyperDataHub(api_port=8420)
    await hub.start()

    # REST:  GET http://localhost:8420/v1/health
    # WS:    ws://localhost:8420/v1/ws
    #        Send: {"subscribe": ["trade", "liquidation"]}
    #        Recv: {"type": "trade", "data": {...}, "ts": 1234567890.123}

Security model:
    - Binds to loopback by default; loopback needs no credentials.
    - A non-loopback bind (HYPERDATA_API_HOST) is refused unless either
      HYPERDATA_API_KEY is set (all non-health routes then require it) or
      HYPERDATA_UNSAFE_PUBLIC_API=1 explicitly acknowledges the risk.
    - CORS is wildcard only on loopback; non-loopback binds must allowlist
      origins via HYPERDATA_CORS_ORIGINS (comma-separated), else no CORS.
    - Per-IP REST rate limit + WebSocket per-client send queues.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hmac
import ipaddress
import json
import logging
import math
import os
import time
from collections import deque
from typing import Any

from aiohttp import WSMsgType, web

logger = logging.getLogger(__name__)

# Event types clients can subscribe to
EVENT_TYPES = {"trade", "liquidation", "signal", "funding_update", "iv_update", "alert", "heartbeat"}

MAX_WS_CONNECTIONS = 10
# Per-client outbound queue depth. When a slow client's queue is full, new
# events are dropped for that client (counted) instead of spawning unbounded
# send tasks that compete with ingestion.
WS_SEND_QUEUE_SIZE = 200
# Inbound WebSocket message limits: size cap, rate cap, and how many bad
# (non-JSON / oversized / too-fast) messages we tolerate before disconnecting.
WS_MAX_MSG_BYTES = 4096
WS_MAX_MSGS_PER_10S = 20
WS_BAD_MSG_LIMIT = 5

# Per-IP REST rate limit (sliding window).
RATE_LIMIT_REQUESTS = 300
RATE_LIMIT_WINDOW_S = 60.0


def _is_loopback_host(host: str) -> bool:
    """True if the bind host is loopback-only ('localhost', 127.x, ::1)."""
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _serialize(obj: Any) -> Any:
    """Recursively serialize dataclass objects to JSON-safe dicts."""
    if obj is None:
        return None
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _serialize(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    if isinstance(obj, float):
        if math.isinf(obj) or math.isnan(obj):
            return None
    return obj


def _make_cors_middleware(allowed_origins: set[str] | None):
    """CORS middleware factory.

    allowed_origins=None means wildcard (loopback binds only); otherwise the
    request Origin must be in the allowlist to receive CORS headers.
    """

    def _cors_headers(request: web.Request) -> dict[str, str]:
        if allowed_origins is None:
            origin = "*"
        else:
            req_origin = request.headers.get("Origin", "")
            if req_origin not in allowed_origins:
                return {}
            origin = req_origin
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, X-API-Key",
            **({"Vary": "Origin"} if origin != "*" else {}),
        }

    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        headers = _cors_headers(request)
        if request.method == "OPTIONS":
            return web.Response(status=200, headers=headers)
        try:
            resp = await handler(request)
        except web.HTTPNotFound:
            return web.json_response(
                {"error": "Endpoint not found", "path": request.path},
                status=404,
                headers=headers,
            )
        except web.HTTPException as exc:
            exc.headers.update(headers)
            raise
        resp.headers.update(headers)
        return resp

    return cors_middleware


# Paths reachable without an API key (liveness checks must not need secrets).
_UNAUTHENTICATED_PATHS = {"/v1/health", "/health"}


def _make_auth_middleware(api_key: str):
    """Require the API key on every route except health checks.

    Accepts either ``Authorization: Bearer <key>`` or ``X-API-Key: <key>``.
    """

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        if request.method == "OPTIONS" or request.path in _UNAUTHENTICATED_PATHS:
            return await handler(request)
        supplied = request.headers.get("X-API-Key", "")
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = supplied or auth[len("Bearer "):]
        if not supplied or not hmac.compare_digest(supplied, api_key):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return await handler(request)

    return auth_middleware


class _RateLimiter:
    """Sliding-window per-IP request limiter for the REST surface."""

    def __init__(self, max_requests: int = RATE_LIMIT_REQUESTS,
                 window_s: float = RATE_LIMIT_WINDOW_S) -> None:
        self.max_requests = max_requests
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        dq = self._hits.get(key)
        if dq is None:
            dq = self._hits.setdefault(key, deque())
        cutoff = now - self.window_s
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= self.max_requests:
            return False
        dq.append(now)
        # Bound tracked IPs so a scan can't grow this dict forever.
        if len(self._hits) > 10_000:
            stale = [k for k, v in self._hits.items() if not v or v[-1] < cutoff]
            for k in stale:
                del self._hits[k]
        return True


def _make_rate_limit_middleware(limiter: _RateLimiter):
    @web.middleware
    async def rate_limit_middleware(request: web.Request, handler):
        remote = request.remote or "unknown"
        if not limiter.allow(remote):
            return web.json_response({"error": "Rate limit exceeded"}, status=429)
        return await handler(request)

    return rate_limit_middleware


def _int_param(request: web.Request, name: str, default: int,
               minimum: int | None = None, maximum: int | None = None) -> int:
    """Parse an int query param → 400 on garbage (not an uncaught 500), clamped."""
    raw = request.query.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(reason=f"'{name}' must be an integer")
    if minimum is not None:
        val = max(val, minimum)
    if maximum is not None:
        val = min(val, maximum)
    return val


def _float_param(request: web.Request, name: str, default: float,
                 minimum: float | None = None, maximum: float | None = None) -> float:
    """Parse a float query param → 400 on garbage, clamped."""
    raw = request.query.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(reason=f"'{name}' must be a number")
    if minimum is not None:
        val = max(val, minimum)
    if maximum is not None:
        val = min(val, maximum)
    return val


# ── WebSocket client tracker ─────────────────────────────────────────────

class _WSClient:
    __slots__ = ("ws", "subscriptions", "ping_misses", "connected_at",
                 "queue", "writer_task", "dropped_msgs", "msg_times", "bad_msgs")

    def __init__(self, ws: web.WebSocketResponse, subscriptions: set[str] | None = None):
        self.ws = ws
        # Default to EMPTY — clients must opt in via subscribe message
        self.subscriptions: set[str] = subscriptions if subscriptions is not None else set()
        self.ping_misses: int = 0
        self.connected_at: float = time.time()
        # Bounded outbound queue drained by a single writer task per client.
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=WS_SEND_QUEUE_SIZE)
        self.writer_task: asyncio.Task | None = None
        self.dropped_msgs: int = 0
        # Inbound abuse tracking (message rate + malformed messages).
        self.msg_times: deque[float] = deque(maxlen=WS_MAX_MSGS_PER_10S)
        self.bad_msgs: int = 0


class HyperDataAPI:
    """REST API v1 + WebSocket streaming, backed by a live HyperDataHub."""

    def __init__(self, hub, host: str = "127.0.0.1", port: int = 8420) -> None:
        # Bind to loopback by default. Non-loopback binds are refused in
        # start() unless HYPERDATA_API_KEY is set (auth enforced) or
        # HYPERDATA_UNSAFE_PUBLIC_API=1 explicitly acknowledges the risk.
        self.hub = hub
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._ws_clients: list[_WSClient] = []
        self._hooks_installed = False
        self._rate_limiter = _RateLimiter()

    # ── Lifecycle ────────────────────────────────────────────────

    def _resolve_security(self) -> tuple[str, set[str] | None]:
        """Validate bind/auth/CORS config. Returns (api_key, cors_allowlist).

        Raises RuntimeError for a non-loopback bind with neither an API key
        nor an explicit unsafe acknowledgment.
        """
        api_key = os.environ.get("HYPERDATA_API_KEY", "").strip()
        unsafe_ack = os.environ.get("HYPERDATA_UNSAFE_PUBLIC_API", "") == "1"
        origins_raw = os.environ.get("HYPERDATA_CORS_ORIGINS", "").strip()
        origins: set[str] | None = (
            {o.strip() for o in origins_raw.split(",") if o.strip()}
            if origins_raw else None
        )

        if _is_loopback_host(self.host):
            # Loopback: wildcard CORS unless an allowlist was configured.
            return api_key, origins

        if not api_key and not unsafe_ack:
            raise RuntimeError(
                f"Refusing to bind API to non-loopback host {self.host!r}: the "
                "API would expose trading intelligence to the network. Set "
                "HYPERDATA_API_KEY to require authentication, or set "
                "HYPERDATA_UNSAFE_PUBLIC_API=1 to explicitly accept the risk."
            )
        if not api_key:
            logger.warning(
                "SECURITY: API bound to %s WITHOUT authentication "
                "(HYPERDATA_UNSAFE_PUBLIC_API=1). Anyone on the network can "
                "read wallet/trading intelligence.", self.host,
            )
        # Non-loopback: never wildcard CORS. No allowlist -> no CORS headers.
        return api_key, (origins or set())

    async def start(self) -> None:
        api_key, cors_origins = self._resolve_security()
        self._api_key = api_key

        middlewares = [_make_rate_limit_middleware(self._rate_limiter)]
        if api_key:
            middlewares.append(_make_auth_middleware(api_key))
        middlewares.append(_make_cors_middleware(cors_origins))
        app = web.Application(middlewares=middlewares)

        # v1 routes
        v1 = [
            ("GET", "/v1/health", self.handle_health),
            ("GET", "/v1/market", self.handle_market),
            ("GET", "/v1/market/{symbol}", self.handle_market_symbol),
            ("GET", "/v1/orderflow/{symbol}", self.handle_orderflow),
            ("GET", "/v1/liquidations", self.handle_liquidations),
            ("GET", "/v1/liquidations/stats", self.handle_liquidation_stats),
            ("GET", "/v1/funding-rates", self.handle_funding_rates),
            ("GET", "/v1/funding-rates/{symbol}", self.handle_funding_symbol),
            ("GET", "/v1/long-short-ratio", self.handle_lsr),
            ("GET", "/v1/basis", self.handle_basis),
            ("GET", "/v1/deribit/iv", self.handle_deribit_iv),
            # Smart-money endpoints removed from OSS build (derived trader intelligence)
            ("GET", "/v1/orderbook/{symbol}", self.handle_orderbook),
            ("GET", "/v1/whales", self.handle_whales),
            ("GET", "/v1/positions/danger-zone", self.handle_danger_zone),
            # Copy-trading endpoints removed from OSS build (derived trader intelligence)
            ("GET", "/v1/public/metrics", self.handle_public_metrics),
        ]
        for method, path, handler in v1:
            app.router.add_route(method, path, handler)

        # WebSocket
        app.router.add_get("/v1/ws", self.handle_ws)

        # Backward-compat redirects: /health -> /v1/health etc.
        legacy_paths = [
            "/health", "/market", "/liquidations", "/liquidations/stats",
            "/funding-rates", "/long-short-ratio", "/basis", "/deribit/iv",
            "/whales", "/positions/danger-zone",
        ]
        for path in legacy_paths:
            app.router.add_get(path, self._make_redirect(f"/v1{path}"))

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

        # Install hub hooks for WebSocket broadcasting
        self._install_hooks()

        # Start heartbeat task for WebSocket clients
        self.__init_dedup()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="ws-heartbeat")

        logger.info("API v1 server started on http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if hasattr(self, '_heartbeat_task') and self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        for client in list(self._ws_clients):
            if client.writer_task:
                client.writer_task.cancel()
            if not client.ws.closed:
                await client.ws.close()
        self._ws_clients.clear()

        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("API server stopped")

    # ── Hub hooks for WebSocket broadcast ────────────────────────

    def _install_hooks(self) -> None:
        if self._hooks_installed:
            return
        self._hooks_installed = True

        # Trade events from orderflow engine
        self.hub.orderflow.on_trade(self._on_trade)

        # Liquidation events
        self.hub.liquidations.on_liquidation(self._on_liquidation)

    def _on_trade(self, trade) -> None:
        self._broadcast("trade", {
            "symbol": trade.symbol,
            "price": trade.price,
            "size_usd": trade.size_usd,
            "side": trade.side,
            "timestamp": trade.timestamp,
        })

    # Symbol cleanup: remove numeric prefixes, map weird names
    _SYM_MAP = {
        "PLAY": "PLAYAI", "1000CHEE": "CHEE", "1000PEPE": "PEPE",
        "1000SHIB": "SHIB", "1000FLOKI": "FLOKI", "1000BONK": "BONK",
        "1000LUNC": "LUNC", "1000X": "X", "1000CAT": "CAT",
        "1000SATS": "SATS", "1000RATS": "RATS",
        "\u9f99\u867e": "LOBSTER", "BSB": "BSB", "PTB": "PTB",
        "ON": "ON", "NOM": "NOM",
    }
    _MIN_LIQ_SIZE_USD = 500
    _DEDUP_WINDOW = 3
    _DEDUP_MAX = 500
    _CASCADE_WINDOW = 30
    _CASCADE_BYPASS_DURATION = 30

    def __init_dedup(self):
        if not hasattr(self, '_liq_seen'):
            self._liq_seen: dict[str, float] = {}
            self._liq_count = {"hyperliquid": 0, "binance": 0, "okx": 0, "bybit": 0}
            self._heartbeat_task: asyncio.Task | None = None
            self._cascade_tracker: dict[str, list] = {}
            self._cascade_bypass: dict[str, float] = {}
            self._liq_stats = {"received": 0, "broadcast": 0, "deduped": 0, "filtered": 0}
            self._liq_stats_ts = time.time()

    def _is_duplicate_liq(self, ev) -> bool:
        """Duplicate check within the dedup window, keyed per exchange.

        Buckets on the EXCHANGE event timestamp (not local receive time) so
        two records of the same event dedup identically regardless of local
        delivery jitter. The cascade bypass is also per-exchange: a Binance
        cascade must not let Hyperliquid's heuristic events skip dedup.
        """
        self.__init_dedup()
        now = time.time()

        bypass_key = f"{ev.symbol}_{ev.side}_{ev.exchange}"
        if bypass_key in self._cascade_bypass and now < self._cascade_bypass[bypass_key]:
            return False

        ev_ts = ev.timestamp if ev.timestamp > 0 else now
        size_rounded = round(ev.size_usd, -2)
        h = f"{ev.symbol}_{ev.side}_{size_rounded}_{ev.exchange}_{int(ev_ts // self._DEDUP_WINDOW)}"

        if len(self._liq_seen) > self._DEDUP_MAX:
            cutoff = now - self._DEDUP_WINDOW * 2
            self._liq_seen = {k: v for k, v in self._liq_seen.items() if v > cutoff}

        if h in self._liq_seen:
            return True
        self._liq_seen[h] = now
        return False

    def _check_cascade(self, ev) -> str | None:
        """Track rapid successive liquidations. Returns cascade label if detected.

        Keys on the RAW event fields (symbol/side/exchange) — the same domain
        _is_duplicate_liq reads its bypass with — so a detected cascade
        actually lifts dedup for the venue that is cascading.
        """
        self.__init_dedup()
        now = time.time()
        key = f"{ev.symbol}_{ev.side}_{ev.exchange}"

        if key not in self._cascade_tracker:
            self._cascade_tracker[key] = []

        self._cascade_tracker[key] = [
            (ts, sz) for ts, sz in self._cascade_tracker[key]
            if now - ts < self._CASCADE_WINDOW
        ]

        self._cascade_tracker[key].append((now, ev.size_usd))

        entries = self._cascade_tracker[key]
        if len(entries) >= 3:
            # Bypass dedup only for this exchange's stream: cascades on one
            # venue say nothing about duplicates on another.
            self._cascade_bypass[key] = now + self._CASCADE_BYPASS_DURATION
            total = sum(sz for _, sz in entries)
            return f"cascade ${total:,.0f} ({len(entries)}x in {self._CASCADE_WINDOW}s)"

        return None

    def _log_liq_stats(self) -> None:
        """Log 60-second liquidation throughput stats."""
        self.__init_dedup()
        now = time.time()
        if now - self._liq_stats_ts >= 60:
            s = self._liq_stats
            total = s["received"]
            if total > 0:
                drop_pct = s["deduped"] / total * 100
                logger.info(
                    "[LIQ] 60s: received=%d broadcast=%d deduped=%d filtered=%d (%.0f%% drop)",
                    s["received"], s["broadcast"], s["deduped"], s["filtered"], drop_pct,
                )
            self._liq_stats = {"received": 0, "broadcast": 0, "deduped": 0, "filtered": 0}
            self._liq_stats_ts = now

    def _clean_symbol(self, sym: str) -> str:
        sym = sym.upper()
        if sym in self._SYM_MAP:
            return self._SYM_MAP[sym]
        if sym.startswith("1000") and len(sym) > 4:
            return sym[4:]
        return sym

    def _on_liquidation(self, ev) -> None:
        self.__init_dedup()
        self._liq_stats["received"] += 1
        self._log_liq_stats()

        if ev.size_usd < self._MIN_LIQ_SIZE_USD:
            self._liq_stats["filtered"] += 1
            return

        if self._is_duplicate_liq(ev):
            self._liq_stats["deduped"] += 1
            return

        self._liq_stats["broadcast"] += 1

        ex = ev.exchange.lower()
        if ex in self._liq_count:
            self._liq_count[ex] += 1

        # Clean symbol
        symbol = self._clean_symbol(ev.symbol)

        # Estimate leverage
        leverage = None
        if ev.price > 0 and ev.quantity > 0:
            notional = ev.price * ev.quantity
            if notional > 0 and ev.size_usd > 0:
                est_lev = round(notional / max(ev.size_usd, 1))
                if 2 <= est_lev <= 200:
                    leverage = est_lev

        ex_map = {"binance": "BIN", "bybit": "BYB", "okx": "OKX", "hyperliquid": "HYP"}
        ex_short = ex_map.get(ev.exchange, ev.exchange[:3].upper())

        cascade = self._check_cascade(ev)

        self._broadcast("liquidation", {
            "exchange": ex_short,
            "symbol": symbol,
            "side": ev.side.upper(),
            "size_usd": ev.size_usd,
            "price": ev.price,
            "leverage": f"{leverage}x" if leverage else None,
            "confirmed": ev.confirmed,
            "cascade": cascade,
        })

        # Alert: large liquidation cascade check. Use CONFIRMED volume only —
        # blended total_volume_usd is inflated by Hyperliquid's large-trade
        # heuristic, which would fire false cascade alerts.
        stats = self.hub.liquidations.get_stats(window_minutes=10)
        confirmed_vol = stats.get("confirmed_volume_usd", 0)
        if confirmed_vol > 5_000_000:
            self._broadcast("alert", {
                "type": "liq_cascade", "asset": ev.symbol,
                "message": f"Liquidation cascade: ${confirmed_vol:,.0f} in 10min (confirmed)",
                "severity": "HIGH", "action": "REVIEW_POSITIONS",
            })

    def _broadcast(self, event_type: str, data: dict) -> None:
        """Enqueue event for all subscribed WebSocket clients.

        Each client has a bounded queue drained by its own writer task, so a
        slow client drops ITS events (counted) instead of accumulating one
        send task per client per event on the shared loop.
        """
        if not self._ws_clients:
            return
        msg = json.dumps({"type": event_type, "data": data, "ts": time.time()})
        dead: list[_WSClient] = []
        for client in list(self._ws_clients):
            if client.ws.closed:
                dead.append(client)
                continue
            if event_type not in client.subscriptions:
                continue
            try:
                client.queue.put_nowait(msg)
            except asyncio.QueueFull:
                client.dropped_msgs += 1
                if client.dropped_msgs % 100 == 1:
                    logger.warning(
                        "[ws] Slow client: %d events dropped (queue full)",
                        client.dropped_msgs,
                    )
        for d in dead:
            self._remove_client(d)

    def _remove_client(self, client: _WSClient) -> None:
        if client in self._ws_clients:
            self._ws_clients.remove(client)
        if client.writer_task and not client.writer_task.done():
            client.writer_task.cancel()

    async def _writer_loop(self, client: _WSClient) -> None:
        """Single writer per client: drain the queue with a send timeout."""
        try:
            while not client.ws.closed:
                msg = await client.queue.get()
                try:
                    await asyncio.wait_for(client.ws.send_str(msg), timeout=2.0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Send failed or timed out — this client is done.
                    self._remove_client(client)
                    try:
                        await client.ws.close()
                    except Exception:
                        pass
                    return
        except asyncio.CancelledError:
            pass

    async def _heartbeat_loop(self) -> None:
        """Push heartbeat every 10s. Evict clients that miss 3 consecutive pings."""
        while True:
            try:
                await asyncio.sleep(10)
                if not self._ws_clients:
                    continue

                self.__init_dedup()
                stats = self.hub.liquidations.get_stats(window_minutes=60)
                msg = json.dumps({"type": "heartbeat", "data": {
                    "ws_clients": len(self._ws_clients),
                    "liq_total_1h": stats.get("total_count", 0),
                    "liq_volume_1h": stats.get("total_volume_usd", 0),
                    "by_exchange": self._liq_count,
                }, "ts": time.time()})

                dead: list[_WSClient] = []
                for client in list(self._ws_clients):
                    if client.ws.closed:
                        dead.append(client)
                        continue
                    if "heartbeat" not in client.subscriptions:
                        continue
                    # Enqueue through the same bounded queue as broadcasts. A
                    # full queue means the writer is stuck/slow — count it as
                    # a missed ping and evict after 3 in a row.
                    try:
                        client.queue.put_nowait(msg)
                        client.ping_misses = 0
                    except asyncio.QueueFull:
                        client.ping_misses += 1
                        if client.ping_misses >= 3:
                            dead.append(client)
                            logger.info("[ws] Evicting client after 3 missed heartbeats")

                for d in dead:
                    self._remove_client(d)
                    try:
                        await d.ws.close()
                    except Exception:
                        pass

            except asyncio.CancelledError:
                return
            except Exception:
                # Never die silently: the heartbeat loop is also the WS
                # liveness janitor, so log and keep going.
                logger.exception("[ws] Heartbeat loop error")

    # ── Redirect helper ──────────────────────────────────────────

    @staticmethod
    def _make_redirect(target: str):
        async def redirect(request: web.Request) -> web.Response:
            raise web.HTTPFound(target)
        return redirect

    # ── WebSocket handler ────────────────────────────────────────

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        if len(self._ws_clients) >= MAX_WS_CONNECTIONS:
            return web.json_response({"error": "Too many connections"}, status=429)
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=WS_MAX_MSG_BYTES)
        await ws.prepare(request)
        client = _WSClient(ws, subscriptions=set())
        client.writer_task = asyncio.create_task(
            self._writer_loop(client), name="ws-writer"
        )
        self._ws_clients.append(client)
        logger.info("[ws] Client connected (%d total)", len(self._ws_clients))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if self._ws_msg_violates_limits(client, msg.data):
                        break
                elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        finally:
            self._remove_client(client)
            logger.info("[ws] Client disconnected (%d remaining)", len(self._ws_clients))

        return ws

    def _ws_msg_violates_limits(self, client: _WSClient, raw: str) -> bool:
        """Process one inbound message. Returns True if the client should be
        disconnected (message flood or too many malformed messages)."""
        now = time.time()
        client.msg_times.append(now)
        if (len(client.msg_times) == client.msg_times.maxlen
                and now - client.msg_times[0] < 10.0):
            logger.info("[ws] Disconnecting client: message rate limit exceeded")
            return True

        if len(raw) > WS_MAX_MSG_BYTES:
            client.bad_msgs += 1
        else:
            try:
                data = json.loads(raw)
                subs = data.get("subscribe") if isinstance(data, dict) else None
                if isinstance(subs, list):
                    client.subscriptions = {s for s in subs if s in EVENT_TYPES}
                    client.queue.put_nowait(json.dumps({
                        "type": "subscribed",
                        "channels": sorted(client.subscriptions),
                    }))
            except (json.JSONDecodeError, asyncio.QueueFull):
                client.bad_msgs += 1

        if client.bad_msgs >= WS_BAD_MSG_LIMIT:
            logger.info("[ws] Disconnecting client after %d bad messages", client.bad_msgs)
            return True
        return False

    # ── REST Handlers ────────────────────────────────────────────

    async def handle_health(self, request: web.Request) -> web.Response:
        s = self.hub.status
        uptime = int(s.uptime_seconds)
        h, m = uptime // 3600, (uptime % 3600) // 60

        # Continuous self-verification result (None until the first run).
        data_health = None
        monitor = getattr(self.hub, "health", None)
        if monitor is not None:
            data_health = monitor.latest()

        # Per-feed connection/staleness state (see the staleness watchdog).
        feeds = {
            "liquidation_feed": s.liquidation_feed,
            "orderflow_engine": s.orderflow_engine,
            "orderbook_feed": s.orderbook_feed,
            "market_data": s.market_data,
            "hlp": s.hlp_status,
        }

        # Per-venue orderflow freshness: the combined status above follows the
        # freshest venue, so a dead venue is only visible here.
        orderflow_venues = None
        try:
            orderflow_venues = self.hub.orderflow.venue_freshness()
        except Exception:
            pass

        # Top-level status reflects data health when available: 'ok' only when
        # nothing is stale/drifting. 'degraded' otherwise (server is still up).
        # Components that failed to start also force 'degraded'.
        overall = data_health.get("overall") if data_health else None
        status = "ok" if overall in (None, "ok", "warn") else "degraded"
        if s.failed_components:
            status = "degraded"

        return web.json_response({
            "status": status,
            "failed_components": list(s.failed_components),
            "orderflow_venues": orderflow_venues,
            "version": "1.0.0",
            "mode": s.mode,
            "uptime": f"{h}h {m}m",
            "uptime_seconds": s.uptime_seconds,
            "total_liquidations": s.total_liquidations,
            "total_trades": s.total_trades_processed,
            "tracked_assets": s.tracked_assets,
            "tracked_positions": s.tracked_positions,
            "ws_clients": len(self._ws_clients),
            "feeds": feeds,
            "data_health": data_health,
            "docs": "https://github.com/siewbrayden/hyperdata-terminal",
        })

    async def handle_market(self, request: web.Request) -> web.Response:
        limit = _int_param(request, "limit", 50, minimum=1, maximum=1000)
        assets = self.hub.get_all_assets()[:limit]
        data = []
        for a in assets:
            data.append({
                "symbol": a.symbol, "price": a.price,
                "funding_rate": a.funding_rate,
                "open_interest": a.open_interest,
                "volume_24h": a.volume_24h,
                "price_change_24h_pct": a.price_change_24h_pct,
                "mark_price": a.mark_price,
                "index_price": a.index_price,
                "premium_pct": getattr(a, "premium_pct", 0.0),
            })
        return web.json_response({"count": len(data), "assets": data})

    async def handle_market_symbol(self, request: web.Request) -> web.Response:
        sym = request.match_info["symbol"].upper()
        asset = self.hub.market.assets.get(sym)
        if not asset:
            return web.json_response({"error": f"Unknown symbol: {sym}"}, status=404)
        return web.json_response(_serialize(asset))

    async def handle_orderflow(self, request: web.Request) -> web.Response:
        sym = request.match_info["symbol"].upper()
        try:
            snapshots = self.hub.orderflow.get_all_snapshots(sym)
        except KeyError:
            return web.json_response({"error": f"No orderflow data for {sym}"}, status=404)
        result = {}
        for tf, snap in snapshots.items():
            if snap:
                result[tf] = {
                    "buy_volume": snap.buy_volume, "sell_volume": snap.sell_volume,
                    "net_volume": snap.buy_volume - snap.sell_volume,
                    "trade_count": snap.trade_count,
                    "ofi": snap.ofi, "signal": snap.signal,
                }
        cvd = self.hub.orderflow.cumulative_cvd.get(sym, 0.0)
        tps = self.hub.orderflow.get_trades_per_second(sym)
        agg = self.hub.orderflow.get_multi_timeframe_signal(sym)
        return web.json_response({
            "symbol": sym, "cumulative_cvd": cvd,
            "trades_per_second": tps, "aggregate_signal": agg,
            "timeframes": result,
        })

    async def handle_liquidations(self, request: web.Request) -> web.Response:
        limit = _int_param(request, "limit", 100, minimum=1, maximum=1000)
        exchange = request.query.get("exchange")
        minutes = _int_param(request, "minutes", 60, minimum=1, maximum=10080)
        events = self.hub.liquidations.get_recent(minutes=minutes, exchange=exchange)[:limit]
        data = []
        for ev in events:
            data.append({
                "timestamp": ev.timestamp, "exchange": ev.exchange,
                "symbol": ev.symbol, "side": ev.side,
                "size_usd": ev.size_usd, "price": ev.price,
                "quantity": ev.quantity, "confirmed": ev.confirmed,
            })
        return web.json_response({"count": len(data), "events": data})

    async def handle_liquidation_stats(self, request: web.Request) -> web.Response:
        minutes = _int_param(request, "minutes", 60, minimum=1, maximum=10080)
        stats = self.hub.liquidations.get_stats(window_minutes=minutes)
        return web.json_response(stats)

    async def handle_funding_rates(self, request: web.Request) -> web.Response:
        result: dict[str, dict] = {}
        for sym, asset in self.hub.market.assets.items():
            result[sym] = {"hl": asset.funding_rate * 8760 * 100}
        for ex_name, ex_rates in self.hub.funding.rates.items():
            for sym, snap in ex_rates.items():
                if sym not in result:
                    result[sym] = {}
                result[sym][ex_name] = snap.funding_rate_annualized * 100
        return web.json_response(result)

    async def handle_funding_symbol(self, request: web.Request) -> web.Response:
        sym = request.match_info["symbol"].upper()
        rates = {}
        asset = self.hub.market.assets.get(sym)
        if asset:
            rates["hl"] = {"hourly": asset.funding_rate, "annualized_pct": asset.funding_rate * 8760 * 100}
        for ex_name, ex_rates in self.hub.funding.rates.items():
            snap = ex_rates.get(sym)
            if snap:
                rates[ex_name] = {
                    "hourly": snap.funding_rate_hourly,
                    "annualized_pct": snap.funding_rate_annualized * 100,
                }
        if not rates:
            return web.json_response({"error": f"No funding data for {sym}"}, status=404)
        return web.json_response({"symbol": sym, "rates": rates})

    async def handle_lsr(self, request: web.Request) -> web.Response:
        data = {}
        for sym in ["BTC", "ETH", "SOL"]:
            snap = self.hub.lsr.get_latest(sym)
            if snap:
                data[sym] = {
                    "long_ratio": snap.long_ratio, "short_ratio": snap.short_ratio,
                    "long_short_ratio": snap.long_short_ratio, "timestamp": snap.timestamp,
                }
        return web.json_response(data)

    async def handle_basis(self, request: web.Request) -> web.Response:
        data = {}
        for sym in ["BTC", "ETH", "SOL"]:
            snap = self.hub.spot.get_latest(sym)
            if snap:
                data[sym] = {
                    "spot_price": snap.spot_price, "perp_price": snap.perp_price,
                    "basis_pct": snap.basis_pct, "timestamp": snap.timestamp,
                }
        return web.json_response(data)

    async def handle_deribit_iv(self, request: web.Request) -> web.Response:
        data = {}
        for sym in ["BTC", "ETH"]:
            snap = self.hub.deribit.get_latest(sym)
            if snap:
                data[sym] = {
                    "mark_iv": snap.mark_iv, "index_price": snap.index_price,
                    "timestamp": snap.timestamp,
                }
        return web.json_response(data)

    async def handle_orderbook(self, request: web.Request) -> web.Response:
        sym = request.match_info["symbol"].upper()
        snap = self.hub.get_orderbook(sym)
        if not snap:
            return web.json_response({"error": f"No orderbook for {sym}"}, status=404)
        return web.json_response({
            "symbol": snap.symbol, "imbalance": snap.imbalance,
            "best_bid": snap.best_bid, "best_ask": snap.best_ask,
            "spread": snap.spread, "timestamp": snap.timestamp,
            "bid_levels": len(snap.bids), "ask_levels": len(snap.asks),
            "bids_top5": [{"price": b.price, "size": b.size} for b in snap.bids[:5]],
            "asks_top5": [{"price": a.price, "size": a.size} for a in snap.asks[:5]],
        })

    async def handle_whales(self, request: web.Request) -> web.Response:
        min_size = _float_param(request, "min_size", 50000.0, minimum=0.0)
        limit = _int_param(request, "limit", 20, minimum=1, maximum=500)
        whales = self.hub.get_whale_positions(min_size_usd=min_size)[:limit]
        return web.json_response({
            "count": len(whales),
            "positions": [_serialize(p) for p in whales],
        })

    async def handle_danger_zone(self, request: web.Request) -> web.Response:
        threshold = _float_param(request, "threshold", 5.0, minimum=0.0, maximum=100.0)
        positions = self.hub.positions.get_danger_zone(threshold_pct=threshold)
        return web.json_response({
            "threshold_pct": threshold,
            "count": len(positions),
            "positions": [_serialize(p) for p in positions],
        })

    # ── Public metrics ────────────────────────────────────────────

    async def handle_public_metrics(self, request: web.Request) -> web.Response:
        """GET /v1/public/metrics — server status and data component health."""
        return web.json_response({
            "status": "ok",
            # self._start_time was never set — use the hub's tracked uptime.
            "uptime_seconds": self.hub.status.uptime_seconds,
            "components": {
                "liquidations": self.hub.liquidations is not None,
                "orderflow": self.hub.orderflow is not None,
                "positions": self.hub.positions is not None,
                "market": self.hub.market is not None,
            },
        })

