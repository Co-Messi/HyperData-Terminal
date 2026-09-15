"""
Paper Trading Engine.

Runs a list of Strategy instances against live market data with fake money.
Every signal gets executed instantly at the current market price, logged to
SQLite, and printed to the console.

Usage:
    from src.strategies import PaperTrader
    from src.strategies.examples import CVDMomentum, FundingRateArb

    trader = PaperTrader(hub, [CVDMomentum(), FundingRateArb()])
    await trader.start()
    ...
    await trader.stop()
    print(trader.get_portfolio())
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape

from .base import Signal, Strategy

logger = logging.getLogger(__name__)
console = Console()

# SQLite database lives next to other HyperData data files
DB_DIR = Path(__file__).resolve().parents[2] / "data"
DB_PATH = DB_DIR / "paper_trades.db"

# Table schema for the trade log
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL    NOT NULL,
    strategy    TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    price       REAL    NOT NULL,
    size_usd    REAL    NOT NULL,
    confidence  REAL    NOT NULL,
    reason      TEXT    NOT NULL DEFAULT '',
    pnl         REAL    NOT NULL DEFAULT 0.0
)
"""


class PaperTrader:
    """Async paper trading engine that evaluates strategies on a loop."""

    def __init__(
        self,
        hub,
        strategies: list[Strategy],
        check_interval: int = 30,
        starting_balance: float = 10_000.0,
        reverse_on_opposite_signal: bool = False,
    ) -> None:
        """
        reverse_on_opposite_signal: what an opposite-side signal means for an
        open position. False (default) = CLOSE ONLY — a SELL on a long flattens
        the book and does NOT open a short; the strategy's directional intent
        is dropped until its next signal. True = close and immediately open
        the reverse position at the signal's size (both trades are logged).
        The default is logged at start() so the divergence from "what the
        strategy asked for" is never silent.
        """
        self.hub = hub
        self.strategies = strategies
        self.check_interval = check_interval
        self.reverse_on_opposite_signal = reverse_on_opposite_signal
        self._warned_close_only = False

        # Portfolio state
        self.balance: float = starting_balance
        self.starting_balance: float = starting_balance
        self.positions: dict[str, dict[str, Any]] = {}  # symbol -> position info
        self.trades: list[dict[str, Any]] = []

        # SQLite path (created on start)
        self.db_path: Path = DB_PATH

        # Internal
        self._running: bool = False
        self._task: asyncio.Task | None = None
        self._db: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize the database and begin the evaluation loop."""
        if self._running:
            return

        # Ensure data directory exists
        DB_DIR.mkdir(parents=True, exist_ok=True)

        # Open SQLite connection and create table
        self._db = sqlite3.connect(str(self.db_path))
        self._db.execute(CREATE_TABLE_SQL)
        self._db.commit()

        self._running = True
        self._task = asyncio.create_task(self._loop(), name="paper-trader")

        if not self.reverse_on_opposite_signal:
            logger.warning(
                "PaperTrader close-only semantics: an opposite-side signal CLOSES an "
                "open position and does not open the reverse. Paper results will lag "
                "a backtest that reverses by one check_interval (%ss). Pass "
                "reverse_on_opposite_signal=True to reverse instead.", self.check_interval,
            )

        strat_names = ", ".join(s.name for s in self.strategies)
        console.print(
            f"[bold green]Paper Trader started[/] | "
            f"Balance: ${self.starting_balance:,.2f} | "
            f"Strategies: {strat_names} | "
            f"Interval: {self.check_interval}s"
        )
        logger.info("PaperTrader started with strategies: %s", strat_names)

    async def stop(self) -> None:
        """Stop the evaluation loop and close the database."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._db:
            self._db.close()
            self._db = None
        console.print("[bold red]Paper Trader stopped[/]")
        logger.info("PaperTrader stopped")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Evaluate all strategies every check_interval seconds.

        Strategies may implement evaluate() as sync or async; async ones
        (e.g. the LLM agent) are awaited so a slow evaluation never blocks
        the event loop for the other strategies.
        """
        while self._running:
            try:
                for strategy in self.strategies:
                    try:
                        result = strategy.evaluate(self.hub)
                        signal = await result if inspect.isawaitable(result) else result
                        if signal is None:
                            continue
                        if signal.action in ("BUY", "SELL"):
                            self._execute_trade(strategy.name, signal)
                    except Exception:
                        logger.exception(
                            "Strategy %s raised an error", strategy.name
                        )
            except Exception:
                logger.exception("Error in paper trader loop")

            await asyncio.sleep(self.check_interval)

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------

    @staticmethod
    def _signal_is_valid(signal: Signal) -> bool:
        """Reject malformed signals before they can corrupt the books."""
        try:
            size = float(signal.size_usd)
        except (TypeError, ValueError):
            return False
        return (
            signal.action in ("BUY", "SELL")
            and isinstance(signal.symbol, str) and bool(signal.symbol)
            and size > 0
            and math.isfinite(size)
        )

    def _execute_trade(self, strategy_name: str, signal: Signal) -> None:
        """Execute a paper trade: update positions, log to SQLite, print.

        Accounting invariants:
        - a trade that cannot be logged is not executed — including when the
          trade log is not open at all (`_db is None`), which is a hard refusal,
          not a silent skip of the audit trail;
        - balance never goes negative (adds to a position are balance-checked
          exactly like opens);
        - adding to a position updates the size-weighted average entry price;
        - an opposite-side signal closes the whole position (partial
          reduction is not modeled) and, only if reverse_on_opposite_signal,
          then opens the reverse at the signal's size as a second logged trade.
        """
        if not self._signal_is_valid(signal):
            logger.warning(
                "Rejected invalid signal from %s: action=%r symbol=%r size_usd=%r",
                strategy_name, signal.action, signal.symbol, signal.size_usd,
            )
            return

        if self._db is None:
            logger.error(
                "Paper trade REFUSED (%s %s %s): the trade log is not open — call "
                "start() first. A trade that cannot be logged is not executed.",
                strategy_name, signal.action, signal.symbol,
            )
            return

        # Get current market price for the symbol
        asset = self.hub.market.assets.get(signal.symbol)
        if asset is None or not asset.price or asset.price <= 0:
            logger.warning(
                "Cannot execute trade for %s — no market data", signal.symbol
            )
            return
        price = asset.price

        # Plan the state mutation WITHOUT applying it yet: the trade is
        # persisted to SQLite first, and only a logged trade mutates the
        # books. Otherwise a DB error silently diverges get_portfolio()
        # from the audit trail.
        pnl = 0.0
        reverse_after_close = False
        apply_mutation: Any
        if signal.symbol in self.positions:
            pos = self.positions[signal.symbol]
            # Closing a long (SELL) or closing a short (BUY)
            if (pos["side"] == "long" and signal.action == "SELL") or \
               (pos["side"] == "short" and signal.action == "BUY"):
                reverse_after_close = self.reverse_on_opposite_signal
                if not reverse_after_close and not self._warned_close_only:
                    self._warned_close_only = True
                    logger.warning(
                        "%s %s on an open %s: closing only (reverse_on_opposite_signal=False) — "
                        "the reverse position is NOT opened",
                        signal.action, signal.symbol, pos["side"],
                    )
                price_change_pct = (price - pos["entry_price"]) / pos["entry_price"]
                if pos["side"] == "short":
                    price_change_pct = -price_change_pct
                pnl = pos["size_usd"] * price_change_pct
                credit = pos["size_usd"] + pnl

                def apply_mutation() -> None:
                    # A loss beyond the margin posted would take the account
                    # negative; a real venue liquidates first. Floor at zero
                    # (position is wiped, balance cannot go below broke).
                    self.balance = max(0.0, self.balance + credit)
                    del self.positions[signal.symbol]
            else:
                # Adding in the same direction: balance-checked like an open,
                # entry price becomes the size-weighted average.
                if signal.size_usd > self.balance:
                    logger.warning(
                        "Insufficient balance to add to %s (need $%.2f, have $%.2f)",
                        signal.symbol, signal.size_usd, self.balance,
                    )
                    return
                new_size = pos["size_usd"] + signal.size_usd
                new_entry = (
                    pos["entry_price"] * pos["size_usd"] + price * signal.size_usd
                ) / new_size

                def apply_mutation() -> None:
                    pos["entry_price"] = new_entry
                    pos["size_usd"] = new_size
                    self.balance -= signal.size_usd
        else:
            # Open a new position
            if signal.size_usd > self.balance:
                logger.warning(
                    "Insufficient balance for %s %s (need $%.2f, have $%.2f)",
                    signal.action, signal.symbol, signal.size_usd, self.balance,
                )
                return
            side = "long" if signal.action == "BUY" else "short"

            def apply_mutation() -> None:
                self.positions[signal.symbol] = {
                    "side": side,
                    "entry_price": price,
                    "size_usd": signal.size_usd,
                    "opened_at": time.time(),
                }
                self.balance -= signal.size_usd

        # Build trade record
        trade = {
            "timestamp": time.time(),
            "strategy": strategy_name,
            "symbol": signal.symbol,
            "action": signal.action,
            "price": price,
            "size_usd": signal.size_usd,
            "confidence": signal.confidence,
            "reason": signal.reason,
            "pnl": pnl,
        }

        # Persist FIRST; a trade that cannot be logged is not executed.
        try:
            self._db.execute(
                "INSERT INTO paper_trades "
                "(timestamp, strategy, symbol, action, price, size_usd, "
                "confidence, reason, pnl) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade["timestamp"], trade["strategy"], trade["symbol"],
                    trade["action"], trade["price"], trade["size_usd"],
                    trade["confidence"], trade["reason"], trade["pnl"],
                ),
            )
            self._db.commit()
        except sqlite3.Error:
            logger.exception(
                "Failed to persist trade to SQLite — trade NOT executed "
                "(books stay consistent with the audit log)"
            )
            return

        apply_mutation()
        self.trades.append(trade)

        # Print to console with Rich. Symbol, strategy name and reason are
        # external strings (exchange payload / LLM output); escape them so
        # markup like "[bold red]" or an unbalanced "[" can neither restyle
        # the line nor raise MarkupError — which would fire AFTER the books
        # were already mutated above.
        color = "green" if signal.action == "BUY" else "red"
        pnl_str = f"  PnL: ${pnl:+,.2f}" if pnl != 0 else ""
        console.print(
            f"[bold {color}]{signal.action}[/] {escape(signal.symbol)} | "
            f"${signal.size_usd:,.2f} @ ${price:,.2f} | "
            f"[dim]{escape(strategy_name)}[/] | "
            f"Confidence: {signal.confidence:.0%} | "
            f"{escape(signal.reason)}{pnl_str}"
        )

        # Reverse: the position is now flat, so re-running the same signal
        # opens the opposite side with every check (validity, balance,
        # persist-first) applied and a second row in the audit log.
        if reverse_after_close:
            self._execute_trade(strategy_name, signal)

    # ------------------------------------------------------------------
    # Portfolio summary
    # ------------------------------------------------------------------

    def get_portfolio(self) -> dict[str, Any]:
        """Return current portfolio state with unrealized PnL."""
        # Calculate unrealized PnL across open positions
        unrealized_pnl = 0.0
        positions_value = 0.0
        for symbol, pos in self.positions.items():
            asset = self.hub.market.assets.get(symbol)
            if asset is None:
                positions_value += pos["size_usd"]
                continue
            price_change_pct = (asset.price - pos["entry_price"]) / pos["entry_price"]
            if pos["side"] == "short":
                price_change_pct = -price_change_pct
            pos_pnl = pos["size_usd"] * price_change_pct
            unrealized_pnl += pos_pnl
            positions_value += pos["size_usd"] + pos_pnl

        total_value = self.balance + positions_value
        total_pnl = total_value - self.starting_balance

        return {
            "balance": self.balance,
            "positions": dict(self.positions),
            "positions_value": positions_value,
            "total_value": total_value,
            "total_pnl": total_pnl,
            "total_pnl_pct": (total_pnl / self.starting_balance) * 100,
            "unrealized_pnl": unrealized_pnl,
            "trade_count": len(self.trades),
        }
