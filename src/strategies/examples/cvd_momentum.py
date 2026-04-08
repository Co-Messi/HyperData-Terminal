# Educational example -- not real trading alpha.
#
# CVD Momentum: when buying pressure overwhelms selling (or vice versa),
# trade in the direction of the imbalance.
"""
CVD Momentum Strategy.

Looks at the 5-minute Cumulative Volume Delta. If the difference between
buy volume and sell volume exceeds a threshold, emit a signal.
"""

from __future__ import annotations

from src.strategies.base import Strategy, Signal


class CVDMomentum(Strategy):
    """Trade in the direction of order flow imbalance."""

    def __init__(self, symbol: str = "BTC", threshold: float = 50_000) -> None:
        # symbol  — which asset to watch
        # threshold — minimum buy-sell volume delta (USD) to trigger a signal
        self.symbol = symbol
        self.threshold = threshold

    @property
    def name(self) -> str:
        return "cvd_momentum"

    def evaluate(self, hub) -> Signal | None:
        # Grab the 5-minute order flow snapshot
        snap = hub.orderflow.get_snapshot(self.symbol, "5m")
        if snap is None:
            return None  # no data yet

        # Net delta = buy volume minus sell volume
        delta = snap.buy_volume - snap.sell_volume

        if delta > self.threshold:
            # Strong buying pressure -> go long
            return Signal(
                symbol=self.symbol,
                action="BUY",
                confidence=min(abs(delta) / (self.threshold * 3), 1.0),
                reason=f"5m CVD delta +${delta:,.0f} exceeds threshold",
            )

        if delta < -self.threshold:
            # Strong selling pressure -> go short
            return Signal(
                symbol=self.symbol,
                action="SELL",
                confidence=min(abs(delta) / (self.threshold * 3), 1.0),
                reason=f"5m CVD delta -${abs(delta):,.0f} exceeds threshold",
            )

        # No strong signal either way
        return None
