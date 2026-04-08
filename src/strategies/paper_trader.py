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
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from rich.console import Console

from .base import Strategy, Signal

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
    ) -> None:
        self.hub = hub
        self.strategies = strategies
        self.check_interval = check_interval

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
        """Evaluate all strategies every check_interval seconds."""
        while self._running:
            try:
                for strategy in self.strategies:
                    try:
                        signal = strategy.evaluate(self.hub)
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

    def _execute_trade(self, strategy_name: str, signal: Signal) -> None:
        """Execute a paper trade: update positions, log to SQLite, print."""
        # Get current market price for the symbol
        asset = self.hub.market.assets.get(signal.symbol)
        if asset is None:
            logger.warning(
                "Cannot execute trade for %s — no market data", signal.symbol
            )
            return
        price = asset.price

        # Calculate PnL if closing/reducing an existing position
        pnl = 0.0
        if signal.symbol in self.positions:
            pos = self.positions[signal.symbol]
            # Closing a long (SELL) or closing a short (BUY)
            if (pos["side"] == "long" and signal.action == "SELL") or \
               (pos["side"] == "short" and signal.action == "BUY"):
                price_change_pct = (price - pos["entry_price"]) / pos["entry_price"]
                if pos["side"] == "short":
                    price_change_pct = -price_change_pct
                pnl = pos["size_usd"] * price_change_pct
                self.balance += pos["size_usd"] + pnl
                del self.positions[signal.symbol]
            else:
                # Adding to position in same direction — just increase size
                pos["size_usd"] += signal.size_usd
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
        self.trades.append(trade)

        # Persist to SQLite
        if self._db:
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
                logger.exception("Failed to persist trade to SQLite")

        # Print to console with Rich
        color = "green" if signal.action == "BUY" else "red"
        pnl_str = f"  PnL: ${pnl:+,.2f}" if pnl != 0 else ""
        console.print(
            f"[bold {color}]{signal.action}[/] {signal.symbol} | "
            f"${signal.size_usd:,.2f} @ ${price:,.2f} | "
            f"[dim]{strategy_name}[/] | "
            f"Confidence: {signal.confidence:.0%} | "
            f"{signal.reason}{pnl_str}"
        )

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
