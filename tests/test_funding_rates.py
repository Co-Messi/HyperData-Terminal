"""Tests for multi-exchange funding rate collector."""
from __future__ import annotations

import time
import pytest
from data_layer.funding_rates import FundingRateSnapshot, FundingRateCollector, normalise_fr_symbol


def test_normalise_fr_symbol():
    assert normalise_fr_symbol("BTCUSDT") == "BTC"
    assert normalise_fr_symbol("ETHUSDT") == "ETH"
    assert normalise_fr_symbol("SOLUSDT") == "SOL"
    assert normalise_fr_symbol("BTC") == "BTC"
    assert normalise_fr_symbol("BTCPERP") == "BTC"


def test_funding_rate_snapshot_fields():
    snap = FundingRateSnapshot(
        timestamp=time.time(),
        exchange="binance",
        symbol="BTC",
        funding_rate_hourly=0.0001,
        funding_rate_annualized=0.0001 * 8760,
    )
    assert snap.exchange == "binance"
    assert snap.symbol == "BTC"
    assert abs(snap.funding_rate_annualized - 0.876) < 0.001


def test_collector_parse_binance_response():
    collector = FundingRateCollector()
    raw = [
        {"symbol": "BTCUSDT", "lastFundingRate": "0.0001", "time": 1700000000000},
        {"symbol": "ETHUSDT", "lastFundingRate": "-0.00005", "time": 1700000000000},
        {"symbol": "XRPUSDT", "lastFundingRate": "0.0003", "time": 1700000000000},
    ]
    collector._parse_binance(raw)
    assert "BTC" in collector.rates["binance"]
    assert "ETH" in collector.rates["binance"]
    btc = collector.rates["binance"]["BTC"]
    assert btc.exchange == "binance"
    assert btc.funding_rate_hourly == pytest.approx(0.0001)
    assert btc.funding_rate_annualized == pytest.approx(0.0001 * 8760)
    eth = collector.rates["binance"]["ETH"]
    assert eth.funding_rate_hourly == pytest.approx(-0.00005)


def test_collector_parse_bybit_response():
    collector = FundingRateCollector()
    raw = {
        "result": {
            "list": [
                {"symbol": "BTCUSDT", "fundingRate": "0.00015", "nextFundingTime": "1700000000000"},
                {"symbol": "ETHUSDT", "fundingRate": "-0.0001", "nextFundingTime": "1700000000000"},
            ]
        }
    }
    collector._parse_bybit(raw)
    assert "BTC" in collector.rates["bybit"]
    btc = collector.rates["bybit"]["BTC"]
    assert btc.exchange == "bybit"
    assert btc.funding_rate_hourly == pytest.approx(0.00015)


def test_collector_get_all_for_symbol():
    collector = FundingRateCollector()
    now = time.time()
    collector.rates["binance"]["BTC"] = FundingRateSnapshot(now, "binance", "BTC", 0.0001, 0.0001 * 8760)
    collector.rates["bybit"]["BTC"] = FundingRateSnapshot(now, "bybit", "BTC", 0.00015, 0.00015 * 8760)
    result = collector.get_all_for_symbol("BTC")
    assert len(result) == 2
    exchanges = {s.exchange for s in result}
    assert "binance" in exchanges
    assert "bybit" in exchanges


def test_collector_get_latest():
    collector = FundingRateCollector()
    now = time.time()
    collector.rates["binance"]["ETH"] = FundingRateSnapshot(now, "binance", "ETH", -0.00005, -0.00005 * 8760)
    snap = collector.get_latest("binance", "ETH")
    assert snap is not None
    assert snap.symbol == "ETH"
    assert collector.get_latest("okx", "ETH") is None
