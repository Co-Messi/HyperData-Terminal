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
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import time
from typing import Any

from aiohttp import web, WSMsgType

logger = logging.getLogger(__name__)

# Event types clients can subscribe to
EVENT_TYPES = {"trade", "liquidation", "signal", "funding_update", "iv_update", "alert", "heartbeat"}

MAX_WS_CONNECTIONS = 10


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


_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
}


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(status=200, headers=_CORS_HEADERS)
    try:
        resp = await handler(request)
    except web.HTTPNotFound:
        return web.json_response(
            {"error": "Endpoint not found", "path": request.path},
            status=404,
            headers=_CORS_HEADERS,
        )
    except web.HTTPException as exc:
        exc.headers.update(_CORS_HEADERS)
        raise
    resp.headers.update(_CORS_HEADERS)
    return resp


# ── WebSocket client tracker ─────────────────────────────────────────────

class _WSClient:
    __slots__ = ("ws", "subscriptions", "ping_misses", "connected_at")

    def __init__(self, ws: web.WebSocketResponse, subscriptions: set[str] | None = None):
        self.ws = ws
        # Default to EMPTY — clients must opt in via subscribe message
        self.subscriptions: set[str] = subscriptions if subscriptions is not None else set()
        self.ping_misses: int = 0
        self.connected_at: float = time.time()


class HyperDataAPI:
    """REST API v1 + WebSocket streaming, backed by a live HyperDataHub."""

    def __init__(self, hub, host: str = "0.0.0.0", port: int = 8420) -> None:
        self.hub = hub
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._ws_clients: list[_WSClient] = []
        self._hooks_installed = False

    # ── Lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        app = web.Application(middlewares=[cors_middleware])

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
            ("GET", "/v1/copy-trading/signals", self.handle_copy_trading_signals),
            ("GET", "/v1/copy-trading/clusters", self.handle_copy_trading_clusters),
            ("GET", "/v1/copy-trading/wallets", self.handle_copy_trading_wallets),
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
        """Check if this liquidation is a duplicate within the 3-second dedup window."""
        self.__init_dedup()
        now = time.time()

        bypass_key = f"{ev.symbol}_{ev.side}"
        if bypass_key in self._cascade_bypass and now < self._cascade_bypass[bypass_key]:
            return False

        size_rounded = round(ev.size_usd, -2)
        h = f"{ev.symbol}_{ev.side}_{size_rounded}_{ev.exchange}_{int(now // self._DEDUP_WINDOW)}"

        if len(self._liq_seen) > self._DEDUP_MAX:
            cutoff = now - self._DEDUP_WINDOW * 2
            self._liq_seen = {k: v for k, v in self._liq_seen.items() if v > cutoff}

        if h in self._liq_seen:
            return True
        self._liq_seen[h] = now
        return False

    def _check_cascade(self, symbol: str, side: str, exchange: str, size_usd: float) -> str | None:
        """Track rapid successive liquidations. Returns cascade label if detected."""
        self.__init_dedup()
        now = time.time()
        key = f"{symbol}_{side}_{exchange}"

        if key not in self._cascade_tracker:
            self._cascade_tracker[key] = []

        self._cascade_tracker[key] = [
            (ts, sz) for ts, sz in self._cascade_tracker[key]
            if now - ts < self._CASCADE_WINDOW
        ]

        self._cascade_tracker[key].append((now, size_usd))

        entries = self._cascade_tracker[key]
        if len(entries) >= 3:
            bypass_key = f"{symbol}_{side}"
            self._cascade_bypass[bypass_key] = now + self._CASCADE_BYPASS_DURATION
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

        cascade = self._check_cascade(symbol, ev.side.upper(), ex_short, ev.size_usd)

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

        # Alert: large liquidation cascade check
        stats = self.hub.liquidations.get_stats(window_minutes=10)
        if stats.get("total_volume_usd", 0) > 5_000_000:
            self._broadcast("alert", {
                "type": "liq_cascade", "asset": ev.symbol,
                "message": f"Liquidation cascade: ${stats['total_volume_usd']:,.0f} in 10min",
                "severity": "HIGH", "action": "REVIEW_POSITIONS",
            })

    def _broadcast(self, event_type: str, data: dict) -> None:
        """Send event to all subscribed WebSocket clients. Bulletproof."""
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
                asyncio.ensure_future(self._safe_send(client, msg))
            except Exception:
                dead.append(client)
        for d in dead:
            if d in self._ws_clients:
                self._ws_clients.remove(d)

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
                    try:
                        await asyncio.wait_for(client.ws.send_str(msg), timeout=2.0)
                        client.ping_misses = 0
                    except Exception:
                        client.ping_misses += 1
                        if client.ping_misses >= 3:
                            dead.append(client)
                            logger.debug("[ws] Evicting client after 3 missed pings")

                for d in dead:
                    if d in self._ws_clients:
                        self._ws_clients.remove(d)
                    try:
                        await d.ws.close()
                    except Exception:
                        pass

            except asyncio.CancelledError:
                return
            except Exception:
                pass

    async def _safe_send(self, client: _WSClient, msg: str) -> None:
        """Send with timeout. Remove client on any failure."""
        try:
            await asyncio.wait_for(client.ws.send_str(msg), timeout=2.0)
        except Exception:
            if client in self._ws_clients:
                self._ws_clients.remove(client)
            try:
                await client.ws.close()
            except Exception:
                pass

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
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        client = _WSClient(ws, subscriptions=set())
        self._ws_clients.append(client)
        logger.info("[ws] Client connected (%d total)", len(self._ws_clients))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        subs = data.get("subscribe")
                        if isinstance(subs, list):
                            client.subscriptions = {s for s in subs if s in EVENT_TYPES}
                            await ws.send_json({
                                "type": "subscribed",
                                "channels": sorted(client.subscriptions),
                            })
                    except json.JSONDecodeError:
                        pass
                elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        finally:
            if client in self._ws_clients:
                self._ws_clients.remove(client)
            logger.info("[ws] Client disconnected (%d remaining)", len(self._ws_clients))

        return ws

    # ── REST Handlers ────────────────────────────────────────────

    async def handle_health(self, request: web.Request) -> web.Response:
        s = self.hub.status
        uptime = int(s.uptime_seconds)
        h, m = uptime // 3600, (uptime % 3600) // 60
        return web.json_response({
            "status": "ok",
            "version": "1.0.0",
            "mode": s.mode,
            "uptime": f"{h}h {m}m",
            "uptime_seconds": s.uptime_seconds,
            "total_liquidations": s.total_liquidations,
            "total_trades": s.total_trades_processed,
            "tracked_assets": s.tracked_assets,
            "tracked_positions": s.tracked_positions,
            "ws_clients": len(self._ws_clients),
            "docs": "https://github.com/siewbrayden/hyperdata-terminal",
        })

    async def handle_market(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", "50"))
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
        limit = int(request.query.get("limit", "100"))
        exchange = request.query.get("exchange")
        minutes = int(request.query.get("minutes", "60"))
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
        minutes = int(request.query.get("minutes", "60"))
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
                rates[ex_name] = {"hourly": snap.funding_rate_hourly, "annualized_pct": snap.funding_rate_annualized * 100}
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

    async def handle_smart_money_rankings(self, request: web.Request) -> web.Response:
        n = int(request.query.get("limit", "20"))
        smart = self.hub.get_smart_money(n)
        dumb = self.hub.get_dumb_money(n)
        stats = self.hub.smart_money.get_stats()

        def _fmt_wallet(w):
            d = _serialize(w)
            pnl = w.total_realized_pnl
            if w.total_trades == 0:
                d["pnl_display"] = "--"
            elif abs(pnl) >= 1_000_000:
                d["pnl_display"] = f"${pnl/1_000_000:+.1f}M"
            elif abs(pnl) >= 1_000:
                d["pnl_display"] = f"${pnl/1_000:+.1f}K"
            elif abs(pnl) >= 1:
                d["pnl_display"] = f"${pnl:+.0f}"
            else:
                d["pnl_display"] = "--"
            return d

        return web.json_response({
            "stats": stats,
            "smart": [_fmt_wallet(w) for w in smart],
            "dumb": [_fmt_wallet(w) for w in dumb],
        })

    async def handle_smart_money_signals(self, request: web.Request) -> web.Response:
        n = int(request.query.get("limit", "50"))
        signals = self.hub.get_smart_money_signals(n)
        return web.json_response({
            "count": len(signals),
            "signals": [_serialize(s) for s in signals],
        })

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
        min_size = float(request.query.get("min_size", "50000"))
        limit = int(request.query.get("limit", "20"))
        whales = self.hub.get_whale_positions(min_size_usd=min_size)[:limit]
        return web.json_response({
            "count": len(whales),
            "positions": [_serialize(p) for p in whales],
        })

    async def handle_danger_zone(self, request: web.Request) -> web.Response:
        threshold = float(request.query.get("threshold", "5.0"))
        positions = self.hub.positions.get_danger_zone(threshold_pct=threshold)
        return web.json_response({
            "threshold_pct": threshold,
            "count": len(positions),
            "positions": [_serialize(p) for p in positions],
        })

    # ── Copy-trading endpoints ────────────────────────────────────

    _ct_signals_cache: dict | None = None
    _ct_signals_cache_ts: float = 0

    async def handle_copy_trading_signals(self, request: web.Request) -> web.Response:
        """GET /v1/copy-trading/signals — recent copy/fade signals (10s cache)."""
        try:
            now = time.time()
            if (self._ct_signals_cache is not None
                    and now - self._ct_signals_cache_ts < 10.0):
                return web.json_response(self._ct_signals_cache)

            wc = getattr(self, '_wallet_cluster', None)
            if wc is None:
                return web.json_response({
                    "signals": [], "active_count": 0,
                    "suppressed_count": 0, "last_updated": now,
                })

            signals = wc.get_signals(limit=20)
            active = [s for s in signals if now - s.get("emitted_at", 0) < 300]
            result = {
                "signals": signals,
                "active_count": len(active),
                "suppressed_count": len(signals) - len(active),
                "last_updated": now,
            }
            self._ct_signals_cache = result
            self._ct_signals_cache_ts = now
            return web.json_response(result)
        except Exception as e:
            return web.json_response({
                "error": str(e), "signals": [], "active_count": 0,
            })

    _ct_clusters_cache: dict | None = None
    _ct_clusters_cache_ts: float = 0

    async def handle_copy_trading_clusters(self, request: web.Request) -> web.Response:
        """GET /v1/copy-trading/clusters — cluster breakdown (60s cache)."""
        try:
            now = time.time()
            if (self._ct_clusters_cache is not None
                    and now - self._ct_clusters_cache_ts < 60.0):
                return web.json_response(self._ct_clusters_cache)

            wc = getattr(self, '_wallet_cluster', None)
            if wc is None:
                return web.json_response({
                    "clusters": [], "last_clustered": 0,
                })

            result = {
                "clusters": wc.get_clusters(),
                "last_clustered": wc._last_clustered,
            }
            self._ct_clusters_cache = result
            self._ct_clusters_cache_ts = now
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"error": str(e), "clusters": []})

    _ct_wallets_cache: dict | None = None
    _ct_wallets_cache_ts: float = 0

    async def handle_copy_trading_wallets(self, request: web.Request) -> web.Response:
        """GET /v1/copy-trading/wallets — all tracked wallets (60s cache)."""
        try:
            now = time.time()
            if (self._ct_wallets_cache is not None
                    and now - self._ct_wallets_cache_ts < 60.0):
                return web.json_response(self._ct_wallets_cache)

            wc = getattr(self, '_wallet_cluster', None)
            if wc is None:
                return web.json_response({"wallets": [], "total": 0})

            wallets = wc.get_wallets()
            result = {"wallets": wallets, "total": len(wallets)}
            self._ct_wallets_cache = result
            self._ct_wallets_cache_ts = now
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"error": str(e), "wallets": []})

    # ── Public metrics (live from backtest files + DB, 5-min cache) ──

    async def handle_public_metrics(self, request: web.Request) -> web.Response:
        """GET /v1/public/metrics — server status and data component health."""
        return web.json_response({
            "status": "ok",
            "uptime_seconds": time.time() - self._start_time if hasattr(self, "_start_time") else 0,
            "components": {
                "liquidations": self.hub.liquidations is not None,
                "orderflow": self.hub.orderflow is not None,
                "positions": self.hub.positions is not None,
                "market": self.hub.market is not None,
            },
        })

