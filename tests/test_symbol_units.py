"""1000-unit contracts (M5): Binance and Bybit list PEPE, BONK and FLOKI as
1000PEPEUSDT etc. and Hyperliquid as kPEPE; one unit is 1000 coins and the
price is per 1000. Captured frames from each venue."""
from __future__ import annotations

import asyncio

import pytest

from hyperdata_terminal.data_layer.liquidation_feed import BybitConnection, LiquidationFeed
from hyperdata_terminal.data_layer.orderflow_engine import OrderFlowEngine
from hyperdata_terminal.symbols import canonical, venue_contract
from tests.fixture_data import load_fixture


def test_symbol_map_round_trips():
    assert canonical("binance", "1000PEPEUSDT") == ("PEPE", 1000)
    assert canonical("bybit", "1000BONKUSDT") == ("BONK", 1000)
    assert canonical("hyperliquid", "kPEPE") == ("PEPE", 1000)
    assert canonical("binance", "BTCUSDT") == ("BTC", 1)
    assert canonical("okx", "PEPE-USDT-SWAP") == ("PEPE", 1)
    # A 1000-prefixed name is never stripped blindly: Binance lists both
    # CATUSDT and 1000CATUSDT, which are different coins.
    assert canonical("binance", "1000CATUSDT") == ("1000CAT", 1)
    assert venue_contract("bybit", "FLOKI") == ("1000FLOKIUSDT", 1000)
    assert venue_contract("hyperliquid", "BONK") == ("kBONK", 1000)
    assert venue_contract("binance", "ETH") == ("ETHUSDT", 1)


def test_bybit_subscribes_by_bybit_names_one_topic_per_request():
    topics = BybitConnection.topics()
    assert "allLiquidation.1000PEPEUSDT" in topics and "allLiquidation.PEPEUSDT" not in topics
    assert "allLiquidation.1000BONKUSDT" in topics and "allLiquidation.1000FLOKIUSDT" in topics

    sent = []

    class _WS:
        async def send_json(self, payload):
            sent.append(payload)

    conn = BybitConnection(LiquidationFeed())
    asyncio.run(conn._on_connected(_WS()))
    assert all(len(p["args"]) == 1 for p in sent)  # one bad topic cannot sink others
    asyncio.run(conn._on_message({"success": False, "ret_msg": "error:handler not found",
                                  "op": "subscribe", "req_id": "allLiquidation.MKRUSDT"}))
    assert conn.rejected_topics == ["allLiquidation.MKRUSDT"]


def test_bybit_1000pepe_liquidation_is_pepe_per_coin():
    frames = [item["frame"] for item in load_fixture("bybit/allLiquidation_frames.json")
              if item["frame"]["topic"] == "allLiquidation.1000PEPEUSDT"]
    assert frames, "the capture includes a 1000PEPEUSDT liquidation"
    feed = LiquidationFeed()
    feed.started_at = 0.0
    asyncio.run(BybitConnection(feed)._on_message(frames[0]))
    (ev,) = feed.events
    rec = frames[0]["data"][0]
    assert ev.symbol == "PEPE"
    assert ev.price == pytest.approx(float(rec["p"]) / 1000)
    assert ev.quantity == pytest.approx(float(rec["v"]) * 1000)
    assert ev.size_usd == pytest.approx(float(rec["p"]) * float(rec["v"]))
    assert feed.get_stats(10**7, include_estimated=False, symbol="PEPE")["total_count"] == 1


def test_binance_order_flow_streams_1000pepe_as_pepe():
    engine = OrderFlowEngine(symbols=["BTC", "PEPE"])
    assert "1000pepeusdt@aggTrade" in engine.binance_stream_url()
    assert "/pepeusdt@aggTrade" not in engine.binance_stream_url()
    frame = next(f for f in load_fixture("binance/aggtrade_1000pepe_1000bonk.json") if f["data"]["s"] == "1000PEPEUSDT")
    engine._handle_binance_trade(frame)
    (trade,) = engine.recent_trades["PEPE"]
    p, q = float(frame["data"]["p"]), float(frame["data"]["q"])
    assert trade.price == pytest.approx(p / 1000) and trade.size == pytest.approx(q * 1000)
    assert trade.size_usd == pytest.approx(p * q)


def test_hyperliquid_kpepe_trades_are_pepe_per_coin():
    engine = OrderFlowEngine(symbols=["PEPE", "BONK"])
    frames = load_fixture("hyperliquid/trades_kpepe_kbonk.json")
    for frame in frames:
        engine._handle_message(frame)
    raw = [t for f in frames for t in f["data"] if t["coin"] == "kPEPE"]
    trades = list(engine.recent_trades["PEPE"])
    assert len(trades) == len(raw) > 0
    assert trades[0].price == pytest.approx(float(raw[0]["px"]) / 1000)
    assert trades[0].size_usd == pytest.approx(float(raw[0]["px"]) * float(raw[0]["sz"]))
    assert engine.venues["hyperliquid"].parse_errors == 0
    # Subscriptions use Hyperliquid's names; PEPE is no longer dropped.
    assert engine._hl_listed(["PEPE", "BONK"], {"kPEPE", "kBONK"}) == ["PEPE", "BONK"]


def test_funding_for_1000_unit_contracts_is_keyed_by_the_coin():
    from hyperdata_terminal.data_layer.funding_rates import FundingRateCollector

    collector = FundingRateCollector()
    collector._parse_bybit(load_fixture("bybit/tickers_linear.json"))
    assert "BONK" in collector.rates["bybit"] and "1000BONK" not in collector.rates["bybit"]


def test_mcp_asset_joins_hyperliquid_kbonk_with_cex_bonk(tmp_path, monkeypatch):
    from hyperdata_terminal.data_layer.funding_rates import FundingRateCollector
    from hyperdata_terminal.mcp_server import HubTools
    from tests.test_launch_readiness import _asset, _isolated_hub

    hub = _isolated_hub(tmp_path, monkeypatch)
    try:
        hub.market.assets = {"kBONK": _asset("kBONK", price=0.02, funding=0.00001)}
        hub.funding = FundingRateCollector()
        hub.funding._parse_bybit(load_fixture("bybit/tickers_linear.json"))
        out = HubTools(hub).asset("BONK")
        assert out["symbol"] == "BONK" and out["hyperliquid_symbol"] == "kBONK"
        assert out["funding"]["bybit_interval_hours"] == 4
        assert "bybit_annualized_pct" in out["funding"]
    finally:
        hub.store.close()
