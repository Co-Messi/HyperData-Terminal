"""Tests for long/short ratio collector."""
from __future__ import annotations

import time
import pytest
from data_layer.long_short_ratio import LongShortSnapshot, LongShortCollector


def test_snapshot_fields():
    snap = LongShortSnapshot(
        timestamp=time.time(),
        symbol="BTC",
        long_ratio=0.55,
        short_ratio=0.45,
        long_short_ratio=0.55 / 0.45,
    )
    assert snap.symbol == "BTC"
    assert snap.long_ratio + snap.short_ratio == pytest.approx(1.0)
    assert snap.long_short_ratio == pytest.approx(0.55 / 0.45)


def test_collector_parse_binance_response():
    collector = LongShortCollector()
    raw = [{"timestamp": "1700000000000", "longAccount": "0.5500", "shortAccount": "0.4500"}]
    collector._parse_response("BTC", raw)
    assert "BTC" in collector.ratios
    snap = collector.ratios["BTC"]
    assert snap.symbol == "BTC"
    assert snap.long_ratio == pytest.approx(0.55)
    assert snap.short_ratio == pytest.approx(0.45)
    assert snap.long_short_ratio == pytest.approx(0.55 / 0.45)


def test_collector_parse_empty_response():
    collector = LongShortCollector()
    now = time.time()
    collector.ratios["BTC"] = LongShortSnapshot(now, "BTC", 0.55, 0.45, 1.22)
    collector._parse_response("BTC", [])
    assert collector.ratios["BTC"].long_ratio == pytest.approx(0.55)


def test_collector_get_latest():
    collector = LongShortCollector()
    assert collector.get_latest("BTC") is None
    collector.ratios["BTC"] = LongShortSnapshot(time.time(), "BTC", 0.55, 0.45, 1.22)
    snap = collector.get_latest("BTC")
    assert snap is not None
    assert snap.symbol == "BTC"


def test_collector_default_symbols():
    collector = LongShortCollector()
    assert "BTC" in collector.symbols
    assert "ETH" in collector.symbols
    assert "SOL" in collector.symbols


def test_collector_custom_symbols():
    collector = LongShortCollector(symbols=["BTC", "ETH"])
    assert collector.symbols == ["BTC", "ETH"]
