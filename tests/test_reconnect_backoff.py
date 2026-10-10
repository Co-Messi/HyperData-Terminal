"""Reconnect behaviour (M4): a venue that accepts and then drops the socket
must back off instead of reconnecting every second forever, and one
malformed frame must not tear a healthy socket down."""
from __future__ import annotations

from types import SimpleNamespace

import aiohttp
import pytest

from hyperdata_terminal.data_layer.liquidation_feed import (
    BinanceConnection,
    ExchangeConnection,
    LiquidationFeed,
)


class _WS:
    def __init__(self, frames):
        self._frames = list(frames)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=self._frames.pop(0))

    async def send_json(self, *a, **k):
        pass

    async def close(self):
        self.closed = True


class _Session:
    closed = False

    def __init__(self, frames_per_connect):
        self.frames_per_connect = list(frames_per_connect)
        self.connects = 0

    def ws_connect(self, *a, **k):
        self.connects += 1
        frames = self.frames_per_connect.pop(0) if self.frames_per_connect else []
        return _WS(frames)

    async def close(self):
        pass


async def _run(conn: ExchangeConnection, cycles: int, monkeypatch, clock=None) -> list[float]:
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if clock is not None:
            clock[0] += seconds
        if len(sleeps) >= cycles:
            conn._running = False

    monkeypatch.setattr("hyperdata_terminal.data_layer.liquidation_feed.asyncio.sleep", fake_sleep)
    if clock is not None:
        monkeypatch.setattr("hyperdata_terminal.data_layer.liquidation_feed.time.time", lambda: clock[0])
    conn._running = True
    await conn._run_loop()
    return sleeps


@pytest.mark.asyncio
async def test_accept_then_close_backs_off(monkeypatch):
    conn = BinanceConnection(LiquidationFeed())
    conn._session = _Session([[]] * 6)  # every socket opens and closes at once
    sleeps = await _run(conn, 5, monkeypatch)
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert conn.consecutive_failures == 5


@pytest.mark.asyncio
async def test_a_malformed_frame_does_not_reconnect(monkeypatch):
    feed = LiquidationFeed()
    feed.started_at = 0.0
    conn = BinanceConnection(feed)
    good = ('{"e":"forceOrder","E":1791594103105,"o":{"s":"KAIAUSDT","S":"SELL","o":"LIMIT","f":"IOC",'
            '"q":"3391","p":"0.0590500","ap":"0.0595600","X":"FILLED","l":"33","z":"3391","T":1791594102102}}')
    conn._session = _Session([["not json {", good]])
    await _run(conn, 1, monkeypatch)
    assert conn._session.connects == 1
    assert feed.parse_errors == {"binance": 1}
    assert len(feed.events) == 1


@pytest.mark.asyncio
async def test_a_connection_that_stayed_up_resets_the_backoff(monkeypatch):
    clock = [1_000_000.0]
    conn = BinanceConnection(LiquidationFeed())
    conn._backoff, conn.consecutive_failures = 32.0, 6

    class _LongWS(_WS):
        async def __anext__(self):
            if self._frames:
                clock[0] += ExchangeConnection.HEALTHY_AFTER_SECONDS + 5  # it lived a while
            return await super().__anext__()

    session = _Session([])
    session.ws_connect = lambda *a, **k: _LongWS(["{}"])
    conn._session = session
    sleeps = await _run(conn, 1, monkeypatch, clock)
    assert sleeps == [1.0]
    assert conn.consecutive_failures == 0


@pytest.mark.asyncio
async def test_orderbook_socket_closed_at_once_backs_off(monkeypatch):
    """The orderbook loop reset its backoff and reconnected immediately
    whenever the server closed the socket cleanly."""
    from hyperdata_terminal.data_layer.orderbook import OrderBookEngine

    engine = OrderBookEngine(symbols=["BTC"])
    engine._running = True
    sleeps: list[float] = []

    async def closes_at_once():
        return None

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 4:
            engine._running = False

    monkeypatch.setattr(engine, "_connect_and_listen", closes_at_once)
    monkeypatch.setattr("hyperdata_terminal.data_layer.orderbook.asyncio.sleep", fake_sleep)
    await engine._run_forever()
    assert sleeps == [1.0, 2.0, 4.0, 8.0]
