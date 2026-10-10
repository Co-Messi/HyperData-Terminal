"""REST liquidation endpoints: confirmed-only defaults (M2) and window
coverage reporting (H3)."""
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
async def test_liquidations_endpoint_defaults_to_confirmed_only():
    client = await _client(await _feed_with_one_of_each())
    try:
        body = await (await client.get("/v1/liquidations")).json()
        assert [e["exchange"] for e in body["events"]] == ["okx"]
        assert all(e["confirmed"] for e in body["events"])
        both = await (await client.get("/v1/liquidations?include_estimated=true")).json()
        assert sorted(e["exchange"] for e in both["events"]) == ["hyperliquid", "okx"]
        assert {e["exchange"]: e["confirmed"] for e in both["events"]} == {"okx": True, "hyperliquid": False}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_liquidation_stats_endpoint_defaults_to_confirmed_only():
    client = await _client(await _feed_with_one_of_each())
    try:
        body = await (await client.get("/v1/liquidations/stats")).json()
        assert body["total_volume_usd"] == 50_000
        assert body["include_estimated"] is False
        both = await (await client.get("/v1/liquidations/stats?include_estimated=true")).json()
        assert both["total_volume_usd"] == 300_000
        assert both["include_estimated"] is True
    finally:
        await client.close()


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


@pytest.mark.asyncio
async def test_include_estimated_rejects_unclear_values():
    client = await _client(await _feed_with_one_of_each())
    try:
        assert (await client.get("/v1/liquidations/stats?include_estimated=maybe")).status == 400
        assert (await client.get("/v1/liquidations?include_estimated=2")).status == 400
    finally:
        await client.close()
