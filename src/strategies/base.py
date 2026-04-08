"""
Strategy base class and Signal dataclass.

Subclass Strategy, implement evaluate(), and the PaperTrader will call it
every tick with the full HyperDataHub available.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Signal — the output of every strategy evaluation
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    """A trading signal returned by a strategy.

    Attributes:
        symbol:     The asset to trade (e.g. "BTC", "ETH", "SOL").
        action:     One of "BUY", "SELL", or "HOLD".
        size_usd:   Dollar amount for this trade (paper money).
        confidence: How confident the strategy is, 0.0 to 1.0.
        reason:     Human-readable explanation of why this signal fired.
    """
    symbol: str
    action: str         # "BUY" | "SELL" | "HOLD"
    size_usd: float = 100.0
    confidence: float = 0.5
    reason: str = ""


# ---------------------------------------------------------------------------
# Strategy ABC — implement this to create your own strategy
# ---------------------------------------------------------------------------

class Strategy(ABC):
    """Base class for all paper trading strategies.

    To create your own strategy:
        1. Subclass Strategy
        2. Set the `name` property (used for logging and the trades table)
        3. Implement `evaluate(hub)` — return a Signal or None

    Example::

        class MyStrategy(Strategy):
            @property
            def name(self) -> str:
                return "my_strategy"

            def evaluate(self, hub) -> Signal | None:
                price = hub.market.assets.get("BTC")
                if price and price.funding_rate > 0.001:
                    return Signal("BTC", "SELL", reason="funding too high")
                return None
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this strategy (used in logs and the trades DB)."""
        ...

    @abstractmethod
    def evaluate(self, hub) -> Signal | None:
        """Evaluate current market conditions and optionally return a Signal.

        Called every tick by the PaperTrader. Return None to skip this tick.
        Return a Signal with action="HOLD" to explicitly log inaction.

        The `hub` parameter is a HyperDataHub instance with full access to
        every live data component:

        PRICE & MARKET DATA
            hub.market.assets[symbol]
                -> AssetInfo(price, funding_rate, open_interest, volume_24h,
                             price_change_24h_pct, mark_price, index_price)

        ORDER FLOW (CVD)
            hub.orderflow.get_snapshot(symbol, timeframe)
                -> CVDSnapshot(cvd, buy_volume, sell_volume, trade_count,
                               ofi, signal)
                timeframes: "1m", "5m", "15m", "1h", "4h", "24h"

        LIQUIDATIONS
            hub.liquidations.get_stats(window_minutes=N)
                -> dict with total count/volume, per-exchange, per-symbol,
                   long vs short breakdown

        FUNDING RATES
            hub.funding.rates
                -> {"binance": {"BTC": FundingRateSnapshot, ...},
                    "bybit":   {"BTC": FundingRateSnapshot, ...}}
            hub.funding.get_latest(exchange, symbol)
                -> FundingRateSnapshot(funding_rate_hourly,
                                       funding_rate_annualized)

        LONG/SHORT RATIO
            hub.lsr.get_latest(symbol)
                -> LongShortSnapshot(long_ratio, short_ratio,
                                     long_short_ratio)

        WHALE POSITIONS (Hyperliquid)
            hub.positions.positions
                -> list[TrackedPosition(address, symbol, side, size_usd,
                                        entry_price, liq_price, leverage,
                                        unrealized_pnl)]

        SPOT PRICES & BASIS
            hub.spot.get_latest(symbol)
                -> SpotPriceSnapshot(spot_price, perp_price, basis_pct)

        DERIBIT IMPLIED VOLATILITY
            hub.deribit.get_latest(underlying)
                -> DeribitIVSnapshot(dvol, underlying, timestamp)

        ORDERBOOK
            hub.orderbook.get_snapshot(symbol)
                -> OrderBookSnapshot(best_bid, best_ask, spread_pct, ...)

        Always handle the case where data is None (component not yet loaded
        or exchange is down). Strategies should be defensive:

            snap = hub.orderflow.get_snapshot("BTC", "5m")
            if snap is None:
                return None  # no data yet, skip this tick
        """
        ...
