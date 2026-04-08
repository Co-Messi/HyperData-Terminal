# Educational example -- not real trading alpha.
#
# Funding Rate Arb: when funding is extremely positive, shorts are paying
# longs — go short to collect. When extremely negative, go long.
"""
Funding Rate Arbitrage Strategy.

Watches the Hyperliquid funding rate for a symbol. When funding is
abnormally high (longs paying shorts), open a short. When abnormally
low (shorts paying longs), open a long.
"""

from __future__ import annotations

from src.strategies.base import Strategy, Signal


class FundingRateArb(Strategy):
    """Fade extreme funding rates."""

    def __init__(
        self,
        symbol: str = "BTC",
        high_threshold: float = 0.0005,
        low_threshold: float = -0.0005,
    ) -> None:
        # high_threshold — funding rate above this -> SELL (collect funding)
        # low_threshold  — funding rate below this -> BUY  (collect funding)
        self.symbol = symbol
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold

    @property
    def name(self) -> str:
        return "funding_rate_arb"

    def evaluate(self, hub) -> Signal | None:
        # Get the asset info which includes Hyperliquid's funding rate
        asset = hub.market.assets.get(self.symbol)
        if asset is None:
            return None  # no market data yet

        rate = asset.funding_rate

        if rate > self.high_threshold:
            # Funding is very positive — longs are paying shorts.
            # Go short to collect the funding premium.
            return Signal(
                symbol=self.symbol,
                action="SELL",
                confidence=min(abs(rate) / (self.high_threshold * 3), 1.0),
                reason=f"Funding rate {rate:.6f} > {self.high_threshold} — fade longs",
            )

        if rate < self.low_threshold:
            # Funding is very negative — shorts are paying longs.
            # Go long to collect the funding premium.
            return Signal(
                symbol=self.symbol,
                action="BUY",
                confidence=min(abs(rate) / abs(self.low_threshold * 3), 1.0),
                reason=f"Funding rate {rate:.6f} < {self.low_threshold} — fade shorts",
            )

        # Funding is within normal range — no signal
        return None
