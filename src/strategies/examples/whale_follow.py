# Educational example — not real trading alpha.
# Demonstrates how to use hub.positions to track whale activity.
"""
Whale Follow Strategy
=====================
Follow the largest whale positions on Hyperliquid.
If the biggest whale is heavily long, go long. If short, go short.

Data source: hub.positions — whale position tracking from Hyperliquid.
"""
from __future__ import annotations

from src.strategies.base import Strategy, Signal


class WhaleFollow(Strategy):
    """Mirror the direction of the largest whale position."""

    name = "whale_follow"

    def __init__(
        self,
        symbol: str = "BTC",
        min_position_usd: float = 500_000,
    ):
        self.symbol = symbol
        self.min_position_usd = min_position_usd

    def evaluate(self, hub) -> Signal | None:
        # Get tracked whale positions from Hyperliquid
        positions = getattr(hub.positions, "positions", []) if hub.positions else []
        if not positions:
            return None

        # Find the largest position for our symbol
        biggest = None
        biggest_size = 0.0

        for pos in positions:
            sym = getattr(pos, "symbol", "") or ""
            notional = abs(getattr(pos, "size_usd", 0) or 0)

            if sym.upper() == self.symbol.upper() and notional > biggest_size:
                biggest = pos
                biggest_size = notional

        if not biggest or biggest_size < self.min_position_usd:
            return None

        # Determine whale direction
        side = getattr(biggest, "side", "") or ""
        if side.upper() == "LONG":
            return Signal(
                symbol=self.symbol,
                action="BUY",
                size_usd=100.0,
                confidence=0.5,
                reason=f"Whale long ${biggest_size:,.0f}",
            )
        elif side.upper() == "SHORT":
            return Signal(
                symbol=self.symbol,
                action="SELL",
                size_usd=100.0,
                confidence=0.5,
                reason=f"Whale short ${biggest_size:,.0f}",
            )

        return None
