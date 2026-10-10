"""Per-venue liquidation feed health (M1): a dead confirmed venue must make
the feed 'partial' and be named in /v1/health and the health checks, even
while Hyperliquid large prints keep flowing."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import aiohttp
import pytest

from hyperdata_terminal.data_layer.liquidation_feed import (
    VENUE_DOWN_AFTER_SECONDS,
    VENUE_SILENT_AFTER_SECONDS,
    BinanceConnection,
    BybitConnection,
    LiquidationEvent,
    LiquidationFeed,
    OKXConnection,
)


class _BlockedSession:
    """ws_connect fails the handshake the way a geoblocked venue does."""

    closed = False

    def ws_connect(self, *a, **k):
        raise aiohttp.WSServerHandshakeError(
            request_info=SimpleNamespace(real_url="wss://x"), history=(), status=451, message="Unavailable",
        )

    async def close(self):
        pass


def _feed_with_venues(now: float) -> LiquidationFeed:
    feed = LiquidationFeed()
    for cls in (BinanceConnection, BybitConnection, OKXConnection):
        conn = cls(feed)
        conn.started_at = now - 600
        conn.connected, conn.connected_at, conn.connects = True, now - 500, 1
        conn.last_frame_at = now - 5
        feed._connections.append(conn)
    return feed


@pytest.mark.asyncio
async def test_a_geoblocked_venue_reads_down_with_its_http_status(monkeypatch):
    feed = LiquidationFeed()
    conn = BinanceConnection(feed)
    conn._running = True
    conn.started_at = time.time()
    conn._session = _BlockedSession()

    async def stop_after_one(seconds):
        conn._running = False

    monkeypatch.setattr("hyperdata_terminal.data_layer.liquidation_feed.asyncio.sleep", stop_after_one)
    await conn._run_loop()
    assert conn.last_error == "HTTP 451" and conn.connects == 0
    assert conn.status(time.time())[0] == "connecting"  # inside the grace
    status, reason = conn.status(time.time() + VENUE_DOWN_AFTER_SECONDS + 1)
    assert status == "down" and "never connected" in reason and "HTTP 451" in reason


def test_feed_is_partial_while_a_confirmed_venue_is_down_and_prints_flow():
    now = time.time()
    feed = _feed_with_venues(now)
    binance = feed._connections[0]
    binance.connected = False
    binance.disconnected_at = now - VENUE_DOWN_AFTER_SECONDS - 10
    binance.last_error = "HTTP 451"
    asyncio.run(feed.emit(LiquidationEvent(now, "hyperliquid", "BTC", "long", 50_000, 80_000, 0.625,
                                           confirmed=False)))
    assert feed.feed_status(now) == "partial"
    assert feed.venues_down(now) == [f"binance (disconnected {VENUE_DOWN_AFTER_SECONDS + 10:.0f}s ago (HTTP 451))"]
    health = feed.venue_health(now)
    assert health["binance"]["status"] == "down" and health["okx"]["status"] == "ok"


def test_an_open_but_silent_socket_is_not_ok():
    now = time.time()
    feed = _feed_with_venues(now)
    bybit = feed._connections[1]
    bybit.connected_at = now - VENUE_SILENT_AFTER_SECONDS - 100
    bybit.last_frame_at = now - VENUE_SILENT_AFTER_SECONDS - 50
    assert bybit.status(now)[0] == "silent"
    assert feed.feed_status(now) == "partial"


def test_all_venues_up_is_connected_and_all_down_is_error():
    now = time.time()
    feed = _feed_with_venues(now)
    assert feed.feed_status(now) == "connected"
    for conn in feed._connections:
        conn.connected = False
        conn.disconnected_at = now - 1000
    assert feed.feed_status(now) == "error"


@pytest.mark.asyncio
async def test_hub_status_health_check_and_api_name_the_down_venue(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from hyperdata_terminal.api_server import HyperDataAPI
    from tests.test_launch_readiness import _isolated_hub

    hub = _isolated_hub(tmp_path, monkeypatch)
    try:
        now = time.time()
        hub.liquidations._connections = _feed_with_venues(now)._connections
        okx = hub.liquidations._connections[2]
        okx.feed = hub.liquidations
        okx.connected, okx.disconnected_at, okx.last_error = False, now - 400, "closed by server"
        hub.status.liquidation_feed = "connecting"
        hub.status.started_at = now - 600
        await hub._update_feed_staleness()
        assert hub.status.liquidation_feed == "partial"

        checks = {c.name: c for c in hub.health._check_freshness()}
        assert checks["liquidation_venues"].status == "warn"
        assert "okx: down" in checks["liquidation_venues"].detail

        app = web.Application()
        app.router.add_get("/v1/health", HyperDataAPI(hub=hub).handle_health)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            body = await (await client.get("/v1/health")).json()
        finally:
            await client.close()
        assert body["liquidation_venues"]["okx"]["status"] == "down"
        assert body["feeds"]["liquidation_feed"] == "partial"
    finally:
        hub.store.close()
