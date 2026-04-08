"""Tests for the CVD / Order Flow engine."""
from __future__ import annotations

import time

import pytest

from data_layer.orderflow_engine import (
    Trade,
    TimeframeBucket,
    OrderFlowEngine,
    classify_signal,
)


def _make_trade(
    side: str = "buy", price: float = 70000.0, size: float = 1.0,
    symbol: str = "BTC", ts: float | None = None,
) -> Trade:
    ts = ts or time.time()
    return Trade(timestamp=ts, symbol=symbol, side=side, price=price, size=size, size_usd=price * size)


def test_classify_signal() -> None:
    assert classify_signal(0.5) == "STRONG_BULL"
    assert classify_signal(0.41) == "STRONG_BULL"
    assert classify_signal(0.3) == "BULLISH"
    assert classify_signal(0.16) == "BULLISH"
    assert classify_signal(0.0) == "NEUTRAL"
    assert classify_signal(-0.1) == "NEUTRAL"
    assert classify_signal(-0.2) == "BEARISH"
    assert classify_signal(-0.39) == "BEARISH"
    assert classify_signal(-0.5) == "STRONG_BEAR"
    assert classify_signal(-0.41) == "STRONG_BEAR"


def test_trade_dataclass() -> None:
    t = _make_trade(side="buy", price=65000.0, size=0.5)
    assert t.side == "buy"
    assert t.price == 65000.0
    assert t.size == 0.5
    assert t.size_usd == 65000.0 * 0.5


def test_timeframe_bucket_add_and_snapshot() -> None:
    bucket = TimeframeBucket(symbol="BTC", timeframe="1m", window_seconds=60)
    now = time.time()

    bucket.add_trade(_make_trade("buy", 70000, 1.0, ts=now))
    bucket.add_trade(_make_trade("sell", 70000, 0.5, ts=now))
    bucket.add_trade(_make_trade("buy", 70000, 0.3, ts=now))

    snap = bucket.get_snapshot(now)
    assert snap.symbol == "BTC"
    assert snap.timeframe == "1m"
    assert snap.trade_count == 3
    assert snap.buy_volume == 70000 * 1.0 + 70000 * 0.3
    assert snap.sell_volume == 70000 * 0.5
    assert abs(snap.cvd - (91000 - 35000)) < 0.01
    assert -1.0 <= snap.ofi <= 1.0
    assert snap.trades_per_sec == 3 / 60


def test_timeframe_bucket_expiry() -> None:
    bucket = TimeframeBucket(symbol="ETH", timeframe="1m", window_seconds=60)
    now = time.time()

    bucket.add_trade(_make_trade("buy", 3500, 10.0, symbol="ETH", ts=now - 120))
    bucket.add_trade(_make_trade("sell", 3500, 2.0, symbol="ETH", ts=now))

    snap = bucket.get_snapshot(now)
    assert snap.trade_count == 1, f"Expected 1, got {snap.trade_count}"
    assert snap.buy_volume == 0.0
    assert snap.sell_volume == 3500 * 2.0


def test_engine_process_trade() -> None:
    engine = OrderFlowEngine(symbols=["BTC"])
    now = time.time()

    for t in [_make_trade("buy", 70000, 1.0, ts=now), _make_trade("buy", 70000, 0.5, ts=now),
              _make_trade("sell", 70000, 2.0, ts=now)]:
        engine._process_trade(t)

    snap = engine.get_snapshot("BTC", "1m")
    assert snap.trade_count == 3
    assert abs(snap.buy_volume - 70000 * 1.5) < 0.01
    assert abs(snap.sell_volume - 70000 * 2.0) < 0.01
    assert abs(engine.cumulative_cvd["BTC"] - (70000 * 1.5 - 70000 * 2.0)) < 0.01
    assert len(engine.recent_trades["BTC"]) == 3


def test_engine_callbacks() -> None:
    engine = OrderFlowEngine(symbols=["BTC"])
    received: list[Trade] = []
    engine.on_trade(lambda t: received.append(t))
    t = _make_trade("buy", 70000, 1.0)
    engine._process_trade(t)
    assert len(received) == 1
    assert received[0] is t


def test_engine_all_snapshots() -> None:
    engine = OrderFlowEngine(symbols=["SOL"])
    engine._process_trade(_make_trade("buy", 180, 100.0, symbol="SOL", ts=time.time()))
    snaps = engine.get_all_snapshots("SOL")
    assert set(snaps.keys()) == {"1m", "5m", "15m", "1h", "4h", "24h"}
    for tf, snap in snaps.items():
        assert snap.trade_count == 1
        assert snap.symbol == "SOL"
        assert snap.timeframe == tf


def test_engine_multi_timeframe_signal() -> None:
    engine = OrderFlowEngine(symbols=["BTC"])
    now = time.time()
    for _ in range(50):
        engine._process_trade(_make_trade("buy", 70000, 1.0, ts=now))
    signal = engine.get_multi_timeframe_signal("BTC")
    assert signal in ("STRONG_BULL", "BULLISH"), f"Got {signal}"


def test_engine_detect_divergence_bearish() -> None:
    engine = OrderFlowEngine(symbols=["BTC"])
    now = time.time()
    for i in range(30):
        engine._process_trade(_make_trade("sell", 70000, 1.0, ts=now + i * 0.01))
    prices = [69000, 69500, 70000, 70500, 71000]
    assert engine.detect_divergence("BTC", prices) == "BEARISH_DIVERGENCE"


def test_engine_detect_divergence_bullish() -> None:
    engine = OrderFlowEngine(symbols=["BTC"])
    now = time.time()
    for i in range(30):
        engine._process_trade(_make_trade("buy", 70000, 1.0, ts=now + i * 0.01))
    prices = [71000, 70500, 70000, 69500, 69000]
    assert engine.detect_divergence("BTC", prices) == "BULLISH_DIVERGENCE"


def test_engine_detect_divergence_none() -> None:
    engine = OrderFlowEngine(symbols=["BTC"])
    now = time.time()
    engine._process_trade(_make_trade("buy", 70000, 1.0, ts=now))
    engine._process_trade(_make_trade("sell", 70000, 1.0, ts=now))
    assert engine.detect_divergence("BTC", [70000, 70100]) is None


def test_trades_per_second() -> None:
    engine = OrderFlowEngine(symbols=["BTC"])
    now = time.time()
    for i in range(10):
        engine._process_trade(_make_trade("buy", 70000, 0.1, ts=now + i * 0.1))
    assert abs(engine.get_trades_per_second("BTC") - 10 / 60) < 0.001


def test_add_symbol_runtime() -> None:
    engine = OrderFlowEngine(symbols=["BTC"])
    assert "ETH" not in engine.buckets
    engine.add_symbol("ETH")
    assert "ETH" in engine.buckets
    assert "ETH" in engine.cumulative_cvd
    assert set(engine.buckets["ETH"].keys()) == {"1m", "5m", "15m", "1h", "4h", "24h"}
    engine.add_symbol("ETH")
    assert engine.symbols.count("ETH") == 1


def test_handle_message() -> None:
    engine = OrderFlowEngine(symbols=["BTC"])
    received: list[Trade] = []
    engine.on_trade(lambda t: received.append(t))

    msg = {
        "channel": "trades",
        "data": [
            {"coin": "BTC", "side": "B", "px": "71234.5", "sz": "0.01",
             "hash": "0xabc", "time": 1710000000000, "tid": 12345},
            {"coin": "BTC", "side": "A", "px": "71234.0", "sz": "0.02",
             "hash": "0xdef", "time": 1710000000100, "tid": 12346},
        ],
    }
    engine._handle_message(msg)

    assert len(received) == 2
    assert received[0].side == "buy"
    assert received[0].price == 71234.5
    assert received[1].side == "sell"


def test_handle_message_ignores_other_channels() -> None:
    engine = OrderFlowEngine(symbols=["BTC"])
    received: list[Trade] = []
    engine.on_trade(lambda t: received.append(t))
    engine._handle_message({"channel": "orderUpdates", "data": []})
    engine._handle_message({"channel": "trades", "data": None})
    engine._handle_message({})
    assert len(received) == 0


def test_ofi_bounds() -> None:
    now = time.time()

    bucket = TimeframeBucket(symbol="BTC", timeframe="1m", window_seconds=60)
    for _ in range(100):
        bucket.add_trade(_make_trade("buy", 70000, 1.0, ts=now))
    assert bucket.get_snapshot(now).ofi == 1.0

    bucket2 = TimeframeBucket(symbol="BTC", timeframe="1m", window_seconds=60)
    for _ in range(100):
        bucket2.add_trade(_make_trade("sell", 70000, 1.0, ts=now))
    assert bucket2.get_snapshot(now).ofi == -1.0

    bucket3 = TimeframeBucket(symbol="BTC", timeframe="1m", window_seconds=60)
    assert bucket3.get_snapshot(now).ofi == 0.0


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_feed() -> None:
    """Connect to Hyperliquid WS and verify trades arrive."""
    import asyncio

    engine = OrderFlowEngine(symbols=["BTC", "ETH", "SOL"])
    trade_count = 0

    def on_trade(t: Trade) -> None:
        nonlocal trade_count
        trade_count += 1

    engine.on_trade(on_trade)
    await engine.start()
    await asyncio.sleep(10)
    await engine.stop()

    assert trade_count > 0, "Expected at least one trade in 10s"
