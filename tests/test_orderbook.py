"""Tests for orderbook streaming engine."""
from __future__ import annotations

import time
import pytest
from data_layer.orderbook import OrderBookLevel, OrderBookSnapshot, OrderBookEngine, compute_imbalance


def test_order_book_level():
    level = OrderBookLevel(price=83500.0, size=1.5)
    assert level.price == 83500.0
    assert level.size == 1.5


def test_compute_imbalance_all_bids():
    bids = [OrderBookLevel(p, 1.0) for p in [83400, 83300, 83200]]
    asks = [OrderBookLevel(p, 0.0) for p in [83500, 83600, 83700]]
    assert compute_imbalance(bids, asks, depth=3) == pytest.approx(1.0)


def test_compute_imbalance_all_asks():
    bids = [OrderBookLevel(p, 0.0) for p in [83400, 83300, 83200]]
    asks = [OrderBookLevel(p, 1.0) for p in [83500, 83600, 83700]]
    assert compute_imbalance(bids, asks, depth=3) == pytest.approx(-1.0)


def test_compute_imbalance_balanced():
    bids = [OrderBookLevel(83400, 1.0)]
    asks = [OrderBookLevel(83500, 1.0)]
    assert compute_imbalance(bids, asks, depth=1) == pytest.approx(0.0)


def test_compute_imbalance_empty():
    assert compute_imbalance([], [], depth=10) == 0.0


def test_engine_update_book():
    engine = OrderBookEngine(symbols=["BTC"])
    levels_data = {
        "levels": [
            [{"px": "83400", "sz": "1.5"}, {"px": "83300", "sz": "2.0"}],
            [{"px": "83500", "sz": "1.0"}, {"px": "83600", "sz": "0.5"}],
        ]
    }
    engine._update_book("BTC", levels_data)
    book = engine.books["BTC"]
    assert len(book["bids"]) == 2
    assert len(book["asks"]) == 2
    assert book["bids"][0].price == 83400.0
    assert book["bids"][0].size == 1.5
    assert book["asks"][0].price == 83500.0


def test_engine_get_snapshot():
    engine = OrderBookEngine(symbols=["BTC"])
    levels_data = {
        "levels": [
            [{"px": "83400", "sz": "2.0"}, {"px": "83300", "sz": "1.0"}],
            [{"px": "83500", "sz": "1.5"}, {"px": "83600", "sz": "0.5"}],
        ]
    }
    engine._update_book("BTC", levels_data)
    snap = engine.get_snapshot("BTC")
    assert snap is not None
    assert snap.symbol == "BTC"
    assert len(snap.bids) == 2
    assert len(snap.asks) == 2
    assert -1.0 <= snap.imbalance <= 1.0


def test_engine_unknown_symbol_returns_none():
    engine = OrderBookEngine(symbols=["BTC"])
    assert engine.get_snapshot("DOGE") is None


def test_engine_handle_ws_message():
    engine = OrderBookEngine(symbols=["BTC"])
    msg = {
        "channel": "l2Book",
        "data": {
            "coin": "BTC",
            "levels": [
                [{"px": "83400", "sz": "1.0"}],
                [{"px": "83500", "sz": "0.8"}],
            ],
            "time": int(time.time() * 1000),
        }
    }
    engine._handle_message(msg)
    assert engine.books["BTC"]["bids"][0].price == 83400.0
