"""Fewer Hyperliquid websockets and requests per process (H6): Hyperliquid
allows 10 websockets and 1200 request weight a minute per IP."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hyperdata_terminal.data_layer.orderflow_engine import OrderFlowEngine


def _hl_frame(coin: str, px: str, sz: str, side: str, tid: int, users=None) -> dict:
    return {"channel": "trades", "data": [{
        "coin": coin, "px": px, "sz": sz, "side": side, "time": int(time.time() * 1000), "tid": tid,
        "users": users or ["0x" + "a" * 40, "0x" + "b" * 40],
    }]}


def test_default_symbols_need_two_order_flow_sockets():
    """With the orderbook socket that makes 3 per process, so three
    processes on one IP stay under Hyperliquid's 10 (it was 10 for one)."""
    assert len(OrderFlowEngine()._hl_shards()) == 2


@pytest.mark.asyncio
async def test_large_prints_and_discovery_come_from_the_order_flow_stream(tmp_path, monkeypatch):
    from tests.test_launch_readiness import _isolated_hub

    hub = _isolated_hub(tmp_path, monkeypatch)
    try:
        hub.orderflow._handle_message(_hl_frame("BTC", "80000", "0.5", "A", 1))   # $40k taker sell
        hub.orderflow._handle_message(_hl_frame("BTC", "80000", "0.01", "B", 2))  # $800: not a print
        await asyncio.gather(*list(hub._emit_tasks))
        (ev,) = hub.liquidations.estimated_events
        assert (ev.exchange, ev.side, ev.size_usd, ev.confirmed) == ("hyperliquid", "long", 40_000.0, False)
        assert {"0x" + "a" * 40, "0x" + "b" * 40} <= set(hub.smart_money.wallets)
    finally:
        hub.store.close()


def test_smart_money_off_means_no_discovery(tmp_path, monkeypatch):
    from tests.test_launch_readiness import _isolated_hub

    hub = _isolated_hub(tmp_path, monkeypatch)
    hub.smart_money_enabled = False
    try:
        hub.orderflow._handle_message(_hl_frame("ETH", "2000", "1", "B", 3))
        assert hub.smart_money.wallets == {}
    finally:
        hub.store.close()


def test_smart_money_engine_opens_no_socket():
    from hyperdata_terminal.data_layer.smart_money import SmartMoneyEngine

    assert not hasattr(SmartMoneyEngine, "_discovery_loop")
    assert not hasattr(SmartMoneyEngine, "discover_from_trades")


def test_market_prices_come_live_from_all_mids(tmp_path, monkeypatch):
    from tests.test_launch_readiness import _asset, _isolated_hub

    hub = _isolated_hub(tmp_path, monkeypatch)
    try:
        btc = _asset("BTC", price=80_000.0)
        btc.prev_day_price = 78_000.0
        hub.market.assets = {"BTC": btc}
        hub.orderbook._handle_message({"channel": "allMids", "data": {"mids": {"BTC": "81000.5", "ETH": "x"}}})
        asset = hub.market.assets["BTC"]
        assert asset.price == 81_000.5 and asset.price_source == "mid"
        assert asset.mark_price == 80_000.0
        assert asset.price_change_24h_pct == pytest.approx((81_000.5 - 78_000) / 78_000)
        assert hub.orderbook.mids == {"BTC": 81_000.5}
    finally:
        hub.store.close()


def test_market_rest_refresh_is_every_30_seconds():
    import inspect

    from hyperdata_terminal.data_layer import hub as hub_mod
    from hyperdata_terminal.data_layer.market_data import REFRESH_SECONDS

    assert REFRESH_SECONDS == 30.0
    assert inspect.signature(hub_mod.HyperDataHub.__init__).parameters["market_refresh_interval"].default == 30.0


@pytest.mark.asyncio
async def test_hub_dashboards_read_state_instead_of_fetching():
    from hyperdata_terminal.dashboards.cvd_dashboard import CVDDashboard
    from hyperdata_terminal.dashboards.liquidation_watch import LiquidationWatchDashboard
    from hyperdata_terminal.dashboards.market_overview import MarketOverviewDashboard
    from hyperdata_terminal.dashboards.whale_tracker import WhaleTrackerDashboard

    scanner = MagicMock()
    scanner.scan = AsyncMock(return_value=[])
    scanner.positions = []
    scanner.market_prices = {"BTC": 1.0}
    market = MagicMock()
    market.get_all = AsyncMock(return_value=[])
    market.get_asset = AsyncMock()
    market.assets = {"BTC": SimpleNamespace(price=1.0, price_change_24h_pct=0.0, volume_24h=1.0)}

    await LiquidationWatchDashboard(scanner=scanner).update_data()
    await WhaleTrackerDashboard(scanner=scanner).update_data()
    await MarketOverviewDashboard(market_data=market).update_data()
    await CVDDashboard(engine=OrderFlowEngine(symbols=["BTC"]), market_data=market)._refresh_market_data()
    scanner.scan.assert_not_called()
    market.get_all.assert_not_called()
    market.get_asset.assert_not_called()

    # A standalone run (python -m ...) still drives its own feed.
    await LiquidationWatchDashboard(scanner=scanner, owns_feed=True).update_data()
    scanner.scan.assert_called_once()


@pytest.mark.asyncio
async def test_quiet_hlp_vaults_are_polled_less_often(monkeypatch):
    from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

    tracker = HLPTracker()
    tracker._running = True
    tracker.child_vaults = ["0x" + "1" * 40]
    polls = []

    async def no_fills(address):
        polls.append(time.time())

    monkeypatch.setattr(tracker, "_fetch_fills", no_fills)
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            tracker._running = False

    monkeypatch.setattr("hyperdata_terminal.data_layer.hlp_tracker.asyncio.sleep", fake_sleep)
    await tracker._fills_loop()
    vault = tracker.child_vaults[0]
    assert tracker._vault_interval[vault] == 2 * HLPTracker.FILLS_INTERVAL
    assert tracker._vault_next_poll[vault] > time.time() + HLPTracker.FILLS_INTERVAL
    assert len(polls) == 1  # the next two loop passes skipped the quiet vault
