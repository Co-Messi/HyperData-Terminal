# Educational example — not real trading alpha.
# Demonstrates how to use hub.liquidations to detect cascade events.
"""
Liquidation Cascade Strategy
=============================
Buy the dip after a large liquidation cascade (long liquidations spike).
The idea: forced selling creates temporary mispricings.

Data source: hub.liquidations — real-time liquidation feed from 4 exchanges.
"""
from __future__ import annotations

import time

from src.strategies.base import Strategy, Signal


class LiquidationCascade(Strategy):
    """Buy after a large wave of long liquidations."""

    name = "liquidation_cascade"

    def __init__(
        self,
        symbol: str = "BTC",
        cascade_threshold_usd: float = 1_000_000,
        cooldown_seconds: float = 300,
    ):
        self.symbol = symbol
        self.cascade_threshold_usd = cascade_threshold_usd
        self.cooldown_seconds = cooldown_seconds
        self._last_signal_time = 0.0

    def evaluate(self, hub) -> Signal | None:
        # Cooldown: don't fire more than once per period
        now = time.time()
        if now - self._last_signal_time < self.cooldown_seconds:
            return None

        # Get liquidation stats for the last 5 minutes
        stats = hub.liquidations.get_stats(window_minutes=5)
        if not stats:
            return None

        # Look for a spike in long liquidations (longs getting wiped = price dropping)
        long_liq_usd = getattr(stats, "total_long_usd", 0) or 0

        if long_liq_usd >= self.cascade_threshold_usd:
            self._last_signal_time = now
            return Signal(
                symbol=self.symbol,
                action="BUY",
                size_usd=100.0,
                confidence=0.6,
                reason=f"Liquidation cascade: ${long_liq_usd:,.0f} longs wiped in 5m",
            )

        return None
