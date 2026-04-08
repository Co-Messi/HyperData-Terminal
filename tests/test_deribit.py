"""Tests for Deribit IV (DVOL) data feed."""
from __future__ import annotations

import time
import pytest
from data_layer.deribit import DeribitIVSnapshot, DeribitFeed


def test_snapshot_fields():
    snap = DeribitIVSnapshot(
        timestamp=time.time(),
        underlying="BTC",
        mark_iv=55.5,
        bid_iv=54.0,
        ask_iv=57.0,
        oi_usd=1_000_000_000.0,
        index_price=83500.0,
    )
    assert snap.underlying == "BTC"
    assert snap.mark_iv == 55.5
    assert snap.bid_iv < snap.ask_iv


def test_feed_parse_dvol_message():
    """DeribitFeed correctly parses a DVOL channel message."""
    feed = DeribitFeed()
    msg = {
        "method": "subscription",
        "params": {
            "channel": "deribit_volatility_index.btc_usd",
            "data": {
                "volatility": 51.14,
                "timestamp": 1700000000000,
            }
        }
    }
    feed._handle_message(msg)
    assert "BTC" in feed.snapshots
    snap = feed.snapshots["BTC"]
    assert snap.mark_iv == pytest.approx(51.14)
    assert snap.underlying == "BTC"


def test_feed_parse_eth_dvol():
    feed = DeribitFeed()
    msg = {
        "method": "subscription",
        "params": {
            "channel": "deribit_volatility_index.eth_usd",
            "data": {
                "volatility": 73.29,
                "timestamp": 1700000000000,
            }
        }
    }
    feed._handle_message(msg)
    assert "ETH" in feed.snapshots
    assert feed.snapshots["ETH"].mark_iv == pytest.approx(73.29)


def test_feed_ignores_unknown_channel():
    feed = DeribitFeed()
    msg = {
        "method": "subscription",
        "params": {
            "channel": "trades.BTC-PERPETUAL.raw",
            "data": {"volatility": 55.0}
        }
    }
    feed._handle_message(msg)
    assert len(feed.snapshots) == 0


def test_feed_get_latest():
    feed = DeribitFeed()
    assert feed.get_latest("BTC") is None
    feed.snapshots["BTC"] = DeribitIVSnapshot(time.time(), "BTC", 55.5, 0.0, 0.0, 0.0, 83500.0)
    snap = feed.get_latest("BTC")
    assert snap is not None
    assert snap.underlying == "BTC"


def test_feed_handles_missing_volatility():
    """Message with missing volatility field defaults to 0."""
    feed = DeribitFeed()
    msg = {
        "method": "subscription",
        "params": {
            "channel": "deribit_volatility_index.btc_usd",
            "data": {
                "timestamp": 1700000000000,
                # No volatility field
            }
        }
    }
    feed._handle_message(msg)
    assert "BTC" in feed.snapshots
    assert feed.snapshots["BTC"].mark_iv == pytest.approx(0.0)


def test_feed_parse_index_price():
    """Price index messages update the index_price on subsequent DVOL snapshots."""
    feed = DeribitFeed()
    # First, send index price
    idx_msg = {
        "method": "subscription",
        "params": {
            "channel": "deribit_price_index.btc_usd",
            "data": {"price": 71450.0, "timestamp": 1700000000000}
        }
    }
    feed._handle_message(idx_msg)
    assert feed._index_prices.get("BTC") == pytest.approx(71450.0)

    # Then, send DVOL — should include the cached index price
    dvol_msg = {
        "method": "subscription",
        "params": {
            "channel": "deribit_volatility_index.btc_usd",
            "data": {"volatility": 52.0, "timestamp": 1700000001000}
        }
    }
    feed._handle_message(dvol_msg)
    assert feed.snapshots["BTC"].index_price == pytest.approx(71450.0)
