"""Rolling confirmed-liquidation volume over fixed windows.

Shared by the Telegram/Discord alerts (alerts.py) and the API's WebSocket
cascade alert, so both count the same thing: CONFIRMED liquidations only
(Hyperliquid large prints are mostly ordinary trades), market wide, with
running sums updated incrementally per event instead of a rescan of the
whole liquidation buffer.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class WindowTotals:
    window_seconds: float
    count: int = 0
    volume_usd: float = 0.0
    long_usd: float = 0.0
    short_usd: float = 0.0
    by_symbol: dict[str, float] = field(default_factory=dict)
    by_exchange: dict[str, float] = field(default_factory=dict)

    def top_symbols(self, n: int = 3) -> list[tuple[str, float]]:
        return sorted(self.by_symbol.items(), key=lambda kv: -kv[1])[:n]


class _Window:
    """One window: events in arrival order plus running sums."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.events: deque[tuple[float, str, str, str, float]] = deque()
        self.totals = WindowTotals(window_seconds=seconds)

    def add(self, ts: float, exchange: str, symbol: str, side: str, size: float) -> None:
        self.events.append((ts, exchange, symbol, side, size))
        t = self.totals
        t.count += 1
        t.volume_usd += size
        if side == "long":
            t.long_usd += size
        else:
            t.short_usd += size
        t.by_symbol[symbol] = t.by_symbol.get(symbol, 0.0) + size
        t.by_exchange[exchange] = t.by_exchange.get(exchange, 0.0) + size

    def expire(self, now: float) -> None:
        cutoff = now - self.seconds
        t = self.totals
        while self.events and self.events[0][0] < cutoff:
            _, exchange, symbol, side, size = self.events.popleft()
            t.count -= 1
            t.volume_usd -= size
            if side == "long":
                t.long_usd -= size
            else:
                t.short_usd -= size
            for key, bucket in ((symbol, t.by_symbol), (exchange, t.by_exchange)):
                left = bucket.get(key, 0.0) - size
                if left > 1e-6:
                    bucket[key] = left
                else:
                    bucket.pop(key, None)
        if not self.events:  # no float drift survives an empty window
            self.totals = WindowTotals(window_seconds=self.seconds)


class CascadeDetector:
    """Confirmed liquidation totals over each window in ``windows_seconds``.

    Events are bucketed by arrival time: a Hyperliquid confirmed
    liquidation reaches the feed about two minutes after it happened, and
    an alert is about what the feed has just seen.
    """

    def __init__(self, windows_seconds: tuple[float, ...] = (300.0, 3600.0)) -> None:
        self._windows = {float(w): _Window(float(w)) for w in windows_seconds}

    def add(self, event, now: float | None = None) -> bool:
        """Count a liquidation event. Estimated events are ignored (returns False)."""
        if not getattr(event, "confirmed", True):
            return False
        now = time.time() if now is None else now
        for w in self._windows.values():
            w.expire(now)
            w.add(now, event.exchange, event.symbol, event.side, float(event.size_usd))
        return True

    def totals(self, window_seconds: float, now: float | None = None) -> WindowTotals:
        w = self._windows[float(window_seconds)]
        w.expire(time.time() if now is None else now)
        return w.totals
