"""
Paper Trading Engine.

Runs a list of Strategy instances against live market data with fake money.
Every signal is filled at once, logged to SQLite, and printed.

Fill model (stated in the CLI output and the README):
- Each strategy has its own book: positions are keyed by (strategy,
  symbol), so two strategies trading BTC never close each other's trades.
- A BUY fills at the Hyperliquid price plus ``slippage_bps``, a SELL at it
  minus ``slippage_bps``; every fill pays a taker fee of ``fee_bps`` on its
  notional. Defaults: 2 bps slippage, 4.5 bps fee (Hyperliquid's base taker
  rate).
- Positions are 1x: the full notional is posted from the balance. A loss
  larger than the posted amount (a short that more than doubles) takes the
  balance below zero and is logged; it is never floored.
- Funding is not accrued, and nothing is ever liquidated.
- Each run starts flat. Positions still open when the trader stops are
  closed at the current price with a ``session end`` row in the trade log,
  so the log always balances. A run killed hard leaves its last positions
  open in the log; ``session_id`` tells runs apart.

Usage:
    from hyperdata_terminal.strategies import PaperTrader
    from hyperdata_terminal.strategies.examples import CVDMomentum, FundingRateArb

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
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape

from hyperdata_terminal.paths import DATA_DIR

from .base import Signal, Strategy

logger = logging.getLogger(__name__)
console = Console()

# SQLite database lives next to other HyperData data files
DB_DIR = DATA_DIR
DB_PATH = DB_DIR / "paper_trades.db"

DEFAULT_FEE_BPS = 4.5        # Hyperliquid base tier taker fee, 0.045%
DEFAULT_SLIPPAGE_BPS = 2.0

# Table schema for the trade log. `price` is the fill price (mark plus or
# minus slippage), `mark_price` the price it was derived from, `size_usd`
# the trade's notional, `pnl` the realized price PnL of a close (fees are
# in `fee_usd`, never netted into pnl).
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
    pnl         REAL    NOT NULL DEFAULT 0.0,
    session_id  TEXT    NOT NULL DEFAULT '',
    coins       REAL    NOT NULL DEFAULT 0.0,
    fee_usd     REAL    NOT NULL DEFAULT 0.0,
    mark_price  REAL    NOT NULL DEFAULT 0.0
)
"""

# Columns added after the first release; an existing paper_trades.db gets
# them with ALTER TABLE on open.
_ADDED_COLUMNS = (
    ("session_id", "TEXT NOT NULL DEFAULT ''"),
    ("coins", "REAL NOT NULL DEFAULT 0.0"),
    ("fee_usd", "REAL NOT NULL DEFAULT 0.0"),
    ("mark_price", "REAL NOT NULL DEFAULT 0.0"),
)


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute(CREATE_TABLE_SQL)
    have = {row[1] for row in db.execute("PRAGMA table_info(paper_trades)")}
    for name, decl in _ADDED_COLUMNS:
        if name not in have:
            db.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {decl}")  # literals only
    db.commit()


class PaperTrader:
    """Async paper trading engine that evaluates strategies on a loop."""

    def __init__(
        self,
        hub,
        strategies: list[Strategy],
        check_interval: int = 30,
        starting_balance: float = 10_000.0,
        reverse_on_opposite_signal: bool = False,
        fee_bps: float = DEFAULT_FEE_BPS,
        slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
        close_on_stop: bool = True,
    ) -> None:
        """
        reverse_on_opposite_signal: what an opposite-side signal means for an
        open position. False (default) = CLOSE ONLY: a SELL on a long flattens
        that strategy's book and does NOT open a short. True = close and
        immediately open the reverse position at the signal's size (both
        trades are logged).
        """
        self.hub = hub
        self.strategies = strategies
        self.check_interval = check_interval
        self.reverse_on_opposite_signal = reverse_on_opposite_signal
        self.fee_bps = float(fee_bps)
        self.slippage_bps = float(slippage_bps)
        self.close_on_stop = close_on_stop
        self._warned_close_only = False

        # Portfolio state. positions[(strategy, symbol)] = {side, coins,
        # size_usd (cost basis), entry_price (cost / coins), opened_at}.
        self.balance: float = starting_balance
        self.starting_balance: float = starting_balance
        self.positions: dict[tuple[str, str], dict[str, Any]] = {}
        self.trades: list[dict[str, Any]] = []
        self.fees_paid: float = 0.0
        self.session_id = f"{int(time.time())}-{os.getpid()}"

        # SQLite path (created on start)
        self.db_path: Path = DB_PATH

        # Internal
        self._running: bool = False
        self._task: asyncio.Task | None = None
        self._db: sqlite3.Connection | None = None

    def fill_model(self) -> str:
        return (f"fills at the Hyperliquid price -/+ {self.slippage_bps:g} bps slippage, "
                f"{self.fee_bps:g} bps taker fee per fill, no funding, 1x, open positions closed at exit")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize the database and begin the evaluation loop."""
        if self._running:
            return

        DB_DIR.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.db_path))
        ensure_schema(self._db)

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
            f"Strategies: {escape(strat_names)} | "
            f"Interval: {self.check_interval}s\n"
            f"[dim]Fill model: {self.fill_model()}[/]"
        )
        logger.info("PaperTrader started with strategies: %s (%s)", strat_names, self.fill_model())

    async def stop(self) -> None:
        """Stop the loop, close open positions in the log, close the database."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.close_on_stop and self._db is not None:
            self.close_all("session end: open position closed at exit")
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

    def _fill_price(self, mark: float, action: str) -> float:
        slip = self.slippage_bps / 10_000
        return mark * (1 + slip) if action == "BUY" else mark * (1 - slip)

    def _execute_trade(
        self, strategy_name: str, signal: Signal, mark_override: float | None = None,
        allow_reverse: bool = True,
    ) -> None:
        """Execute a paper trade: update the strategy's book, log to SQLite, print.

        Accounting invariants:
        - a trade that cannot be logged is not executed, including when the
          trade log is not open at all (`_db is None`);
        - an open or add needs its notional plus fee in the balance;
        - the entry price is notional over coins, so adds at a new price
          weight by coins, not dollars;
        - an opposite-side signal closes the whole position (partial
          reduction is not modeled) and, only if reverse_on_opposite_signal,
          then opens the reverse at the signal's size as a second logged trade;
        - final balance minus starting balance equals the sum of closed
          pnl minus every fee in the log, once flat.
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

        if mark_override is not None:
            mark = mark_override
        else:
            asset = self.hub.market.assets.get(signal.symbol)
            if asset is None or not asset.price or asset.price <= 0:
                logger.warning("Cannot execute trade for %s — no market data", signal.symbol)
                return
            mark = asset.price
        fill = self._fill_price(mark, signal.action)
        fee_rate = self.fee_bps / 10_000
        key = (strategy_name, signal.symbol)

        # Plan the state mutation WITHOUT applying it yet: the trade is
        # persisted to SQLite first, and only a logged trade mutates the books.
        pnl = 0.0
        reverse_after_close = False
        pos = self.positions.get(key)
        apply_mutation: Any
        if pos is not None and (
            (pos["side"] == "long" and signal.action == "SELL")
            or (pos["side"] == "short" and signal.action == "BUY")
        ):
            reverse_after_close = self.reverse_on_opposite_signal and allow_reverse
            if not reverse_after_close and allow_reverse and not self._warned_close_only:
                self._warned_close_only = True
                logger.warning(
                    "%s %s on an open %s: closing only (reverse_on_opposite_signal=False) — "
                    "the reverse position is NOT opened",
                    signal.action, signal.symbol, pos["side"],
                )
            coins = pos["coins"]
            notional = coins * fill
            if pos["side"] == "long":
                pnl = (fill - pos["entry_price"]) * coins
            else:
                pnl = (pos["entry_price"] - fill) * coins
            fee = notional * fee_rate
            credit = pos["size_usd"] + pnl - fee

            def apply_mutation() -> None:
                self.balance += credit
                del self.positions[key]
                if self.balance < 0:
                    logger.warning(
                        "%s %s: loss beyond the posted collateral; paper balance is now $%.2f",
                        strategy_name, signal.symbol, self.balance,
                    )
        else:
            notional = float(signal.size_usd)
            coins = notional / fill
            fee = notional * fee_rate
            if notional + fee > self.balance:
                logger.warning(
                    "Insufficient balance for %s %s (need $%.2f with fee, have $%.2f)",
                    signal.action, signal.symbol, notional + fee, self.balance,
                )
                return
            side = "long" if signal.action == "BUY" else "short"

            def apply_mutation() -> None:
                book = self.positions.get(key)
                if book is None:
                    self.positions[key] = {
                        "side": side, "coins": coins, "size_usd": notional,
                        "entry_price": fill, "opened_at": time.time(),
                    }
                else:
                    book["coins"] += coins
                    book["size_usd"] += notional
                    book["entry_price"] = book["size_usd"] / book["coins"]
                self.balance -= notional + fee

        trade = {
            "timestamp": time.time(),
            "strategy": strategy_name,
            "symbol": signal.symbol,
            "action": signal.action,
            "price": fill,
            "mark_price": mark,
            "size_usd": notional,
            "coins": coins,
            "fee_usd": fee,
            "confidence": signal.confidence,
            "reason": signal.reason,
            "pnl": pnl,
            "session_id": self.session_id,
        }

        # Persist FIRST; a trade that cannot be logged is not executed.
        try:
            self._db.execute(
                "INSERT INTO paper_trades "
                "(timestamp, strategy, symbol, action, price, size_usd, confidence, reason, pnl, "
                "session_id, coins, fee_usd, mark_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade["timestamp"], trade["strategy"], trade["symbol"],
                    trade["action"], trade["price"], trade["size_usd"],
                    trade["confidence"], trade["reason"], trade["pnl"],
                    trade["session_id"], trade["coins"], trade["fee_usd"], trade["mark_price"],
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
        self.fees_paid += fee
        self.trades.append(trade)

        # Symbol, strategy name and reason are external strings (exchange
        # payload / LLM output): escape them so markup can neither restyle the
        # line nor raise MarkupError after the books were already mutated.
        color = "green" if signal.action == "BUY" else "red"
        pnl_str = f"  PnL: ${pnl:+,.2f}" if pnl != 0 else ""
        console.print(
            f"[bold {color}]{signal.action}[/] {escape(signal.symbol)} | "
            f"${notional:,.2f} @ ${fill:,.4f} (fee ${fee:,.2f}) | "
            f"[dim]{escape(strategy_name)}[/] | "
            f"Confidence: {signal.confidence:.0%} | "
            f"{escape(signal.reason)}{pnl_str}"
        )

        # Reverse: the book is now flat, so re-running the same signal opens
        # the opposite side with every check applied and a second logged row.
        if reverse_after_close:
            self._execute_trade(strategy_name, signal, mark_override=mark_override, allow_reverse=False)

    def close_all(self, reason: str) -> None:
        """Close every open position at the current price (entry price if no
        price is available), as logged trades."""
        for (strategy_name, symbol), pos in list(self.positions.items()):
            asset = self.hub.market.assets.get(symbol)
            mark = getattr(asset, "price", 0.0) if asset is not None else 0.0
            note = reason
            if not mark or mark <= 0:
                mark = pos["entry_price"]
                note = f"{reason} (no price: closed at entry)"
            action = "SELL" if pos["side"] == "long" else "BUY"
            self._execute_trade(
                strategy_name,
                Signal(symbol, action, size_usd=pos["size_usd"], confidence=0.0, reason=note),
                mark_override=mark, allow_reverse=False,
            )

    # ------------------------------------------------------------------
    # Portfolio summary
    # ------------------------------------------------------------------

    def get_portfolio(self) -> dict[str, Any]:
        """Current portfolio state with unrealized PnL at the current price."""
        unrealized_pnl = 0.0
        positions_value = 0.0
        positions = []
        for (strategy_name, symbol), pos in self.positions.items():
            asset = self.hub.market.assets.get(symbol)
            price = getattr(asset, "price", 0.0) if asset is not None else 0.0
            if price and price > 0:
                if pos["side"] == "long":
                    pos_pnl = (price - pos["entry_price"]) * pos["coins"]
                else:
                    pos_pnl = (pos["entry_price"] - price) * pos["coins"]
            else:
                pos_pnl = 0.0
            unrealized_pnl += pos_pnl
            positions_value += pos["size_usd"] + pos_pnl
            positions.append({"strategy": strategy_name, "symbol": symbol, **pos, "unrealized_pnl": pos_pnl})

        total_value = self.balance + positions_value
        total_pnl = total_value - self.starting_balance

        return {
            "balance": self.balance,
            "positions": positions,
            "positions_value": positions_value,
            "total_value": total_value,
            "total_pnl": total_pnl,
            "total_pnl_pct": (total_pnl / self.starting_balance) * 100,
            "unrealized_pnl": unrealized_pnl,
            "fees_paid": self.fees_paid,
            "trade_count": len(self.trades),
            "fill_model": self.fill_model(),
        }
