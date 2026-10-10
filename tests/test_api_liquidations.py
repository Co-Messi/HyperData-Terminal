"""REST liquidation endpoints: window coverage reporting."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hyperdata_terminal.api_server import HyperDataAPI
from hyperdata_terminal.data_layer.liquidation_feed import LiquidationEvent, LiquidationFeed


async def _feed_with_one_of_each() -> LiquidationFeed:
    feed = LiquidationFeed(max_events=100)
    feed.started_at = time.time() - 7200
    now = time.time()
    await feed.emit(LiquidationEvent(now - 10, "okx", "BTC", "long", 50_000, 80_000, 0.625))
    await feed.emit(LiquidationEvent(now - 5, "hyperliquid", "BTC", "short", 250_000, 80_000, 3.125, confirmed=False))
    return feed


async def _client(feed: LiquidationFeed) -> TestClient:
    api = HyperDataAPI(hub=SimpleNamespace(liquidations=feed))
    app = web.Application()
    app.router.add_get("/v1/liquidations", api.handle_liquidations)
    app.router.add_get("/v1/liquidations/stats", api.handle_liquidation_stats)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_liquidation_endpoints_report_window_coverage_and_cap_the_window():
    feed = await _feed_with_one_of_each()
    feed.started_at = time.time() - 600  # ten minutes of uptime
    client = await _client(feed)
    try:
        stats = await (await client.get("/v1/liquidations/stats?minutes=10080")).json()
        # Memory holds this process's events only; a week cannot be claimed.
        assert stats["window_minutes"] == 1440
        assert stats["window_coverage"] == pytest.approx(600 / 86_400, rel=0.02)
        assert stats["truncated"] is False
        events = await (await client.get("/v1/liquidations?minutes=1440")).json()
        assert events["window_coverage"] == pytest.approx(600 / 86_400, rel=0.02)
        assert events["covered_since"] == pytest.approx(feed.started_at, abs=1)
    finally:
        await client.close()
