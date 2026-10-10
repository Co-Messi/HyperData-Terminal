"""Tests for the multi-exchange liquidation feed."""
from __future__ import annotations

import asyncio
import time

import pytest

from hyperdata_terminal.data_layer.liquidation_feed import (
    BybitConnection,
    LiquidationEvent,
    LiquidationFeed,
    normalize_symbol,
)


def test_normalize_symbol() -> None:
    assert normalize_symbol("BTCUSDT", "binance") == "BTC"
    assert normalize_symbol("ETHUSDT", "bybit") == "ETH"
    assert normalize_symbol("BTC-USDT-SWAP", "okx") == "BTC"
    assert normalize_symbol("SOL-USD-SWAP", "okx") == "SOL"
    assert normalize_symbol("BTC", "hyperliquid") == "BTC"
    assert normalize_symbol("SOLUSD", "binance") == "SOL"
    assert normalize_symbol("DOGEUSDT", "bybit") == "DOGE"


def test_liquidation_event() -> None:
    ev = LiquidationEvent(
        timestamp=time.time(), exchange="binance", symbol="BTC",
        side="long", size_usd=50000.0, price=65000.0, quantity=0.769,
    )
    assert ev.exchange == "binance"
    assert ev.side == "long"
    assert ev.size_usd == 50000.0
    assert ev.confirmed is True


def test_liquidation_event_confirmed_flag() -> None:
    ev = LiquidationEvent(
        timestamp=time.time(), exchange="bybit", symbol="BTC",
        side="long", size_usd=50000.0, price=65000.0, quantity=0.769,
        confirmed=False,
    )
    assert ev.confirmed is False


def test_feed_dispatch_and_stats() -> None:
    feed = LiquidationFeed(max_events=100)
    received: list[LiquidationEvent] = []
    feed.on_liquidation(lambda ev: received.append(ev))

    now = time.time()
    events = [
        LiquidationEvent(now, "binance", "BTC", "long", 100_000, 65000, 1.538),
        LiquidationEvent(now, "bybit", "ETH", "short", 50_000, 3500, 14.285),
        LiquidationEvent(now, "okx", "SOL", "long", 25_000, 180, 138.888),
        LiquidationEvent(now - 400, "binance", "BTC", "short", 10_000, 64000, 0.156),
    ]

    async def dispatch_all() -> None:
        for ev in events:
            await feed.emit(ev)

    asyncio.run(dispatch_all())

    assert len(received) == 4
    assert len(feed.events) == 4

    stats = feed.get_stats(window_minutes=5)
    assert stats["total_count"] == 3, f"expected 3, got {stats['total_count']}"
    assert stats["long_count"] == 2
    assert stats["short_count"] == 1
    assert stats["total_volume_usd"] == 175_000
    assert "binance" in stats["by_exchange"]
    assert stats["by_exchange"]["binance"]["count"] == 1


def test_get_recent_filters() -> None:
    feed = LiquidationFeed(max_events=100)
    now = time.time()

    events = [
        LiquidationEvent(now - 30, "binance", "BTC", "long", 100_000, 65000, 1.538),
        LiquidationEvent(now - 30, "bybit", "BTC", "short", 50_000, 65100, 0.768),
        LiquidationEvent(now - 30, "okx", "ETH", "long", 25_000, 3500, 7.142),
        LiquidationEvent(now - 600, "binance", "SOL", "short", 10_000, 180, 55.555),
    ]

    async def dispatch_all() -> None:
        for ev in events:
            await feed.emit(ev)

    asyncio.run(dispatch_all())

    recent = feed.get_recent(minutes=5)
    assert len(recent) == 3

    btc_only = feed.get_recent(minutes=5, symbol="BTC")
    assert len(btc_only) == 2

    binance_only = feed.get_recent(minutes=5, exchange="binance")
    assert len(binance_only) == 1
    assert binance_only[0].symbol == "BTC"


def test_deque_max_size() -> None:
    feed = LiquidationFeed(max_events=5)
    now = time.time()

    async def fill() -> None:
        for i in range(10):
            ev = LiquidationEvent(now, "binance", "BTC", "long", 1000 * i, 65000, 0.01 * i)
            await feed.emit(ev)

    asyncio.run(fill())
    assert len(feed.events) == 5
    assert feed.events[0].size_usd == 5000


def test_bybit_liquidation_parsing() -> None:
    """BybitConnection parses allLiquidation v5 messages correctly."""
    feed = LiquidationFeed(max_events=100)
    conn = BybitConnection(feed)
    received: list[LiquidationEvent] = []
    feed.on_liquidation(lambda ev: received.append(ev))

    # "Sell" side = long position was liquidated
    sell_msg = {
        "topic": "allLiquidation.BTCUSDT",
        "data": {
            "symbol": "BTCUSDT",
            "side": "Sell",
            "price": "65000.00",
            "qty": "0.5",
            "updatedTime": "1711000000000",
        },
    }
    asyncio.run(conn._on_message(sell_msg))

    assert len(received) == 1
    ev = received[0]
    assert ev.exchange == "bybit"
    assert ev.symbol == "BTC"
    assert ev.side == "long"
    assert ev.size_usd == 65000.0 * 0.5
    assert ev.price == 65000.0
    assert ev.quantity == 0.5
    assert ev.confirmed is True

    # "Buy" side = short position was liquidated
    buy_msg = {
        "topic": "allLiquidation.ETHUSDT",
        "data": {
            "symbol": "ETHUSDT",
            "side": "Buy",
            "price": "3500.00",
            "qty": "2.0",
            "updatedTime": "1711000001000",
        },
    }
    asyncio.run(conn._on_message(buy_msg))

    assert len(received) == 2
    ev2 = received[1]
    assert ev2.symbol == "ETH"
    assert ev2.side == "short"
    assert ev2.confirmed is True

    # Non-liquidation topic should be ignored
    other_msg = {"topic": "publicTrade.BTCUSDT", "data": []}
    asyncio.run(conn._on_message(other_msg))
    assert len(received) == 2


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_feed() -> None:
    """Connect to real exchange feeds for a short duration."""
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    feed = LiquidationFeed()
    event_count = 0

    def on_liq(ev: LiquidationEvent) -> None:
        nonlocal event_count
        event_count += 1

    feed.on_liquidation(on_liq)
    await feed.start()
    await asyncio.sleep(15)
    await feed.stop()

    assert event_count > 0, "Expected at least one liquidation event in 15s"


# ── Window coverage and truncation (H3) ─────────────────────────────────────


def _emit_all(feed: LiquidationFeed, events: list[LiquidationEvent]) -> None:
    async def run() -> None:
        for ev in events:
            await feed.emit(ev)

    asyncio.run(run())


def test_stats_report_partial_coverage_shortly_after_start() -> None:
    """A 24h window ten minutes after launch covers ten minutes, and says so."""
    feed = LiquidationFeed(max_events=100)
    now = time.time()
    feed.started_at = now - 600
    _emit_all(feed, [LiquidationEvent(now - 30, "okx", "BTC", "long", 10_000, 80_000, 0.125)])

    stats = feed.get_stats(window_minutes=1440, include_estimated=False)

    assert stats["truncated"] is False
    assert stats["covered_since"] == pytest.approx(now - 600, abs=1)
    assert stats["window_coverage"] == pytest.approx(600 / 86_400, rel=0.01)


def test_stats_full_coverage_when_window_is_inside_uptime() -> None:
    feed = LiquidationFeed(max_events=100)
    feed.started_at = time.time() - 7200
    stats = feed.get_stats(window_minutes=60, include_estimated=False)
    assert stats["window_coverage"] == 1.0
    assert stats["truncated"] is False


def test_stats_flag_truncation_when_the_buffer_overflows() -> None:
    """The event past the buffer cap evicts the oldest; a window reaching
    back past the evicted events is partial, not a full total."""
    feed = LiquidationFeed(max_events=5)
    now = time.time()
    feed.started_at = now - 7200
    _emit_all(feed, [
        LiquidationEvent(now - 1000 + i * 10, "okx", "BTC", "long", 1_000, 80_000, 0.0125) for i in range(10)
    ])

    stats = feed.get_stats(window_minutes=60, include_estimated=False)

    assert stats["truncated"] is True
    # Events up to the newest evicted one (now - 1000 + 40) may be missing.
    assert stats["covered_since"] == pytest.approx(now - 960, abs=1)
    assert stats["window_coverage"] < 1.0
    # A window that starts after the evicted events is complete again.
    assert feed.get_stats(window_minutes=10, include_estimated=False)["truncated"] is False


def test_truncation_uses_the_newest_evicted_timestamp_not_deque_order() -> None:
    """Hyperliquid confirmed events arrive about two minutes late, so the
    buffer is not in time order: the evicted event can be newer than what
    is left at the front."""
    feed = LiquidationFeed(max_events=2)
    now = time.time()
    feed.started_at = now - 7200
    _emit_all(feed, [
        LiquidationEvent(now - 100, "okx", "BTC", "long", 1_000, 80_000, 0.0125),
        LiquidationEvent(now - 300, "hyperliquid", "BTC", "long", 1_000, 80_000, 0.0125),  # late arrival
        LiquidationEvent(now - 50, "okx", "BTC", "long", 1_000, 80_000, 0.0125),
    ])
    stats = feed.get_stats(window_minutes=60, include_estimated=False)
    assert stats["truncated"] is True
    assert stats["covered_since"] == pytest.approx(now - 100, abs=1)


def test_estimated_overflow_does_not_mark_confirmed_totals_truncated() -> None:
    feed = LiquidationFeed(max_events=2)
    now = time.time()
    feed.started_at = now - 7200
    _emit_all(feed, [
        LiquidationEvent(now - 100 + i, "hyperliquid", "BTC", "long", 20_000, 80_000, 0.25, confirmed=False)
        for i in range(5)
    ])
    assert feed.get_stats(60, include_estimated=False)["truncated"] is False
    assert feed.get_stats(60, include_estimated=True)["truncated"] is True
