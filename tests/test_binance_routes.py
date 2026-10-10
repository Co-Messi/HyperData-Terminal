"""Binance USD-M market streams live under /market. The legacy /ws and
/stream routes accept the handshake and then deliver nothing (probed
2026-10-10), which is how the Binance liquidation feed and CVD leg went
silent while reading as a regional block."""
from __future__ import annotations

from hyperdata_terminal.data_layer.liquidation_feed import BinanceConnection, LiquidationFeed
from hyperdata_terminal.data_layer.orderflow_engine import OrderFlowEngine


def test_liquidations_use_the_market_route():
    assert BinanceConnection(LiquidationFeed()).ws_url == "wss://fstream.binance.com/market/ws/!forceOrder@arr"


def test_cvd_leg_uses_the_market_route():
    url = OrderFlowEngine(symbols=["BTC"]).binance_stream_url()
    assert url.startswith("wss://fstream.binance.com/market/stream?streams=")
    assert "btcusdt@aggTrade" in url
