"""How the hub, the front ends and the scanner use the Hyperliquid budget (H6)."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from hyperdata_terminal.data_layer import address_store, hl_rate


class _Stop(Exception):
    pass


def _recording_hub(monkeypatch, calls: list[dict]):
    from hyperdata_terminal.data_layer import hub as hub_mod

    class Recorder:
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)

        async def start(self):
            raise _Stop

        async def stop(self):
            pass

    monkeypatch.setattr(hub_mod, "HyperDataHub", Recorder)


def test_api_mcp_verify_and_single_dashboards_skip_smart_money(monkeypatch):
    from hyperdata_terminal import cli, mcp_server, terminal, verify_data

    calls: list[dict] = []
    _recording_hub(monkeypatch, calls)
    monkeypatch.setattr(verify_data, "HyperDataHub", __import__(
        "hyperdata_terminal.data_layer.hub", fromlist=["HyperDataHub"]).HyperDataHub)
    monkeypatch.setattr(terminal, "HyperDataHub", verify_data.HyperDataHub)
    monkeypatch.setattr(terminal, "_start_hub", AsyncMock(side_effect=_Stop))

    for coro in (cli._run_api(18421), verify_data.run_audit(1)):
        with pytest.raises(_Stop):
            asyncio.run(coro)
    mcp_server.default_hub()
    for key in ("heatmap", "all"):
        with pytest.raises(_Stop):
            asyncio.run(terminal.run_single(key, boot=False))
    assert [c.get("smart_money", True) for c in calls] == [False, False, False, False, True]


@pytest.mark.asyncio
async def test_live_hub_without_smart_money_never_starts_it(tmp_path, monkeypatch):
    from tests.test_launch_readiness import _isolated_hub

    hub = _isolated_hub(tmp_path, monkeypatch)
    hub.smart_money_enabled = False
    for comp in (hub.liquidations, hub.orderflow, hub.smart_money, hub.hlp, hub.funding, hub.lsr,
                 hub.orderbook, hub.deribit):
        monkeypatch.setattr(comp, "start", AsyncMock())
    monkeypatch.setattr(hub.spot, "start", AsyncMock())
    await hub._start_live()
    try:
        hub.smart_money.start.assert_not_called()
        assert hub_mod_status(hub) == "off"
        assert hl_rate.get_governor().active_elastic == {"position_scanner"}
        assert hl_rate.get_governor().instances == 1
    finally:
        await hl_rate.get_governor().stop()
        hub.store.close()


def hub_mod_status(hub) -> str:
    return hub.status.smart_money_status


def test_scanner_staleness_stretches_when_the_budget_is_shared(tmp_path, monkeypatch):
    from hyperdata_terminal.data_layer import position_scanner as ps

    monkeypatch.setattr(address_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(address_store, "DB_PATH", tmp_path / "a.db")
    monkeypatch.setattr(address_store, "LEGACY_JSON", tmp_path / "legacy.json")
    monkeypatch.setattr(address_store, "_initialized", False)
    scanner = ps.PositionScanner()
    gov = hl_rate.get_governor()
    assert scanner.stale_after_seconds() == ps.POSITION_STALE_AFTER_SECONDS
    gov._instances = 2  # another hyperdata process shares the IP
    shared = scanner.stale_after_seconds()
    assert shared > ps.POSITION_STALE_AFTER_SECONDS
    assert shared == ps.stale_after_seconds_for(gov.allocation("position_scanner"))
    # A position as old as the shared threshold allows is not stale...
    scanner.last_scan_at = time.time()
    assert scanner.is_stale() is False
    assert scanner.freshness()["stale_after_seconds"] == shared


@pytest.mark.asyncio
async def test_health_reports_the_hyperliquid_budget(monkeypatch):
    from unittest.mock import MagicMock

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from hyperdata_terminal.api_server import HyperDataAPI
    from hyperdata_terminal.data_layer.hub import HubStatus

    gov = hl_rate.get_governor()
    gov.on_429("3", component="position_scanner")
    hub = MagicMock()
    hub.status = HubStatus(mode="live")
    hub.health.latest.return_value = None
    hub.orderflow.venue_freshness.return_value = {}
    hub.positions.freshness.return_value = {}
    api = HyperDataAPI(hub=hub)
    app = web.Application()
    app.router.add_get("/v1/health", api.handle_health)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        body = await (await client.get("/v1/health")).json()
    finally:
        await client.close()
    rate = body["hyperliquid_rate"]
    assert rate["http_429_total"] == 1 and rate["budget_per_min"] == gov.budget_per_min
    assert rate["paused_for_seconds"] > 0
