"""MCP results must not give agents false confidence (M8): meta.warnings
names what is degraded, position tools say how old a scan is, and NaN
inputs are never echoed back."""
from __future__ import annotations

import json
import math
import time

import pytest

from hyperdata_terminal.data_layer import hl_rate
from hyperdata_terminal.data_layer.liquidation_feed import (
    VENUE_DOWN_AFTER_SECONDS,
    BinanceConnection,
    BybitConnection,
    OKXConnection,
)
from tests.test_launch_readiness import _asset, _isolated_hub, _position


@pytest.fixture
def tools(tmp_path, monkeypatch):
    from hyperdata_terminal.mcp_server import HubTools

    hub = _isolated_hub(tmp_path, monkeypatch)
    hub.status.started_at = time.time() - 3600
    hub.market.assets = {"BTC": _asset("BTC")}
    hub.positions.positions = [_position("BTC", "long", 2_000_000, liq=79_500)]
    hub.positions.last_scan_at = time.time()
    hub.hlp.snapshots.append(object())
    yield HubTools(hub), hub
    hub.store.close()


def _strict_json(payload) -> None:
    json.dumps(payload, allow_nan=False)


def test_healthy_hub_has_no_warnings(tools):
    t, _ = tools
    assert t.meta()["warnings"] == []


def test_warnings_name_what_is_degraded(tools):
    t, hub = tools
    now = time.time()
    # A position scanned long ago: the scan is stale.
    hub.positions.positions[0].scanned_at = now - 10 * 3600
    # Binance never connected (geoblocked), Bybit and OKX up.
    for cls in (BinanceConnection, BybitConnection, OKXConnection):
        conn = cls(hub.liquidations)
        conn.started_at = now - 3600
        if cls is not BinanceConnection:
            conn.connected, conn.connected_at, conn.connects = True, now - 100, 1
            conn.last_frame_at = now
        else:
            conn.last_error = "HTTP 451"
        hub.liquidations._connections.append(conn)
    hub.status.orderflow_engine = "partial"
    hub.spot.active_source = "coinbase"
    hl_rate.get_governor().on_429("30")

    warnings = " | ".join(t.meta()["warnings"])
    assert "position scan is stale" in warnings
    assert "liquidation venues not receiving: binance" in warnings and "HTTP 451" in warnings
    assert "order flow is partial" in warnings
    assert "spot prices and basis come from coinbase" in warnings
    assert "Hyperliquid is rate limiting" in warnings
    assert VENUE_DOWN_AFTER_SECONDS < 3600


def test_position_tools_report_scan_age(tools):
    t, hub = tools
    hub.positions.positions[0].scanned_at = time.time() - 120
    for out in (t.whale_positions(min_size_usd=0), t.near_liquidation(max_distance_pct=5)):
        assert out["scan_age_seconds"] == pytest.approx(120, abs=2)
        assert out["as_of"] is not None
        pos = out["positions"][0]
        assert pos["scan_age_seconds"] == pytest.approx(120, abs=2)
        assert pos["scanned_at"].endswith("Z")


@pytest.mark.parametrize("call", [
    lambda t: t.whale_positions(min_size_usd=math.nan),
    lambda t: t.whale_positions(min_size_usd=math.inf),
    lambda t: t.near_liquidation(max_distance_pct=math.nan),
    lambda t: t.funding_extremes(min_annualized_pct=math.nan),
    lambda t: t.liquidation_heatmap("BTC", range_pct=math.nan),
])
def test_non_finite_inputs_are_never_echoed(tools, call):
    t, _ = tools
    _strict_json(call(t))


def test_instructions_cover_every_venue_and_the_background_load():
    from hyperdata_terminal.mcp_server import INSTRUCTIONS

    assert "Coinbase" in INSTRUCTIONS
    assert "cost no exchange requests" not in INSTRUCTIONS
    assert "meta.warnings" in INSTRUCTIONS
