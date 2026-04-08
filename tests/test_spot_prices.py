"""Tests for Binance spot price and basis collector."""
from __future__ import annotations

import time
import pytest
from data_layer.spot_prices import SpotPriceSnapshot, SpotPriceCollector


def test_snapshot_fields():
    snap = SpotPriceSnapshot(
        timestamp=time.time(),
        symbol="BTC",
        spot_price=83000.0,
        perp_price=83500.0,
        basis_pct=(83500.0 - 83000.0) / 83000.0 * 100,
    )
    assert snap.symbol == "BTC"
    assert snap.basis_pct == pytest.approx((83500.0 - 83000.0) / 83000.0 * 100)
    assert snap.basis_pct > 0


def test_collector_parse_ticker_response():
    collector = SpotPriceCollector()
    raw = [
        {"symbol": "BTCUSDT", "price": "83000.00"},
        {"symbol": "ETHUSDT", "price": "3440.50"},
        {"symbol": "SOLUSDT", "price": "177.80"},
        {"symbol": "DOGEUSDT", "price": "0.165"},  # Not in default symbols, should be ignored
    ]
    perp_prices = {"BTC": 83500.0, "ETH": 3450.0, "SOL": 178.0}
    collector._parse_response(raw, perp_prices)

    assert "BTC" in collector.prices
    assert "ETH" in collector.prices
    assert "SOL" in collector.prices

    btc = collector.prices["BTC"]
    assert btc.spot_price == pytest.approx(83000.0)
    assert btc.perp_price == pytest.approx(83500.0)
    assert btc.basis_pct == pytest.approx((83500.0 - 83000.0) / 83000.0 * 100)


def test_collector_parse_no_perp_price():
    """If perp price is unavailable, basis_pct defaults to 0."""
    collector = SpotPriceCollector()
    raw = [{"symbol": "BTCUSDT", "price": "83000.00"}]
    collector._parse_response(raw, perp_prices={})
    assert collector.prices["BTC"].basis_pct == pytest.approx(0.0)
    assert collector.prices["BTC"].perp_price == pytest.approx(0.0)


def test_collector_get_latest():
    collector = SpotPriceCollector()
    assert collector.get_latest("BTC") is None
    collector.prices["BTC"] = SpotPriceSnapshot(time.time(), "BTC", 83000.0, 83500.0, 0.6)
    snap = collector.get_latest("BTC")
    assert snap is not None
    assert snap.symbol == "BTC"


def test_collector_default_symbols():
    collector = SpotPriceCollector()
    assert "BTC" in collector.symbols
    assert "ETH" in collector.symbols
    assert "SOL" in collector.symbols


def test_collector_symbol_map():
    """Default symbols map to correct Binance ticker symbols."""
    from data_layer.spot_prices import SYMBOL_TO_BINANCE
    assert SYMBOL_TO_BINANCE["BTC"] == "BTCUSDT"
    assert SYMBOL_TO_BINANCE["ETH"] == "ETHUSDT"
    assert SYMBOL_TO_BINANCE["SOL"] == "SOLUSDT"
