"""Paper trader accounting (H5): one book per strategy, entry price as
notional over coins, taker fees and slippage, no silent balance floor, and
a trade log that balances (open positions are closed at exit)."""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hyperdata_terminal.strategies.base import Signal
from hyperdata_terminal.strategies.paper_trader import PaperTrader, ensure_schema


def _trader(price: float = 100.0, balance: float = 10_000.0, **kw) -> PaperTrader:
    hub = MagicMock()
    hub.market.assets = {"BTC": SimpleNamespace(price=price)}
    trader = PaperTrader(hub, [], starting_balance=balance, **kw)
    trader._db = sqlite3.connect(":memory:")
    ensure_schema(trader._db)
    return trader


def _set_price(trader: PaperTrader, price: float) -> None:
    trader.hub.market.assets["BTC"].price = price


def _ledger(trader: PaperTrader) -> tuple[float, float]:
    pnl, fees = trader._db.execute("SELECT SUM(pnl), SUM(fee_usd) FROM paper_trades").fetchone()
    return pnl or 0.0, fees or 0.0


def test_two_strategies_on_one_symbol_keep_separate_books():
    """All five built-ins trade BTC. Strategy B's SELL used to close
    strategy A's long and book A's PnL under B."""
    trader = _trader(fee_bps=0, slippage_bps=0)
    trader._execute_trade("a", Signal("BTC", "BUY", size_usd=1_000.0))
    trader._execute_trade("b", Signal("BTC", "SELL", size_usd=1_000.0))
    assert trader.positions[("a", "BTC")]["side"] == "long"
    assert trader.positions[("b", "BTC")]["side"] == "short"

    _set_price(trader, 110.0)
    trader._execute_trade("a", Signal("BTC", "SELL", size_usd=1_000.0))  # closes a only
    assert ("a", "BTC") not in trader.positions
    assert ("b", "BTC") in trader.positions
    rows = trader._db.execute("SELECT strategy, pnl FROM paper_trades WHERE pnl != 0").fetchall()
    assert rows == [("a", pytest.approx(100.0))]


def test_entry_price_is_notional_over_coins():
    """$1,000 at 100 then $1,000 at 200 is 15 coins for $2,000: entry 133.33,
    not the dollar-weighted 150 that understated long PnL after adds."""
    trader = _trader(fee_bps=0, slippage_bps=0)
    trader._execute_trade("t", Signal("BTC", "BUY", size_usd=1_000.0))
    _set_price(trader, 200.0)
    trader._execute_trade("t", Signal("BTC", "BUY", size_usd=1_000.0))
    pos = trader.positions[("t", "BTC")]
    assert pos["coins"] == pytest.approx(15.0)
    assert pos["entry_price"] == pytest.approx(2_000.0 / 15.0)
    # Marked at 200 the book is worth 15 * 200 = 3,000.
    assert trader.get_portfolio()["unrealized_pnl"] == pytest.approx(1_000.0)


def test_default_fills_pay_slippage_and_taker_fee():
    trader = _trader()  # defaults: 2 bps slippage, 4.5 bps fee
    trader._execute_trade("t", Signal("BTC", "BUY", size_usd=1_000.0))
    trade = trader.trades[0]
    assert trade["price"] == pytest.approx(100.0 * 1.0002)
    assert trade["fee_usd"] == pytest.approx(1_000.0 * 0.00045)
    assert trader.balance == pytest.approx(10_000.0 - 1_000.0 - 0.45)
    trader._execute_trade("t", Signal("BTC", "SELL", size_usd=1_000.0))
    # Round trip at an unchanged price loses the spread and two fees.
    assert trader.balance < 10_000.0 - 0.9
    assert "fee" in trader.fill_model() and "slippage" in trader.fill_model()


def test_loss_beyond_collateral_is_not_floored_and_ledger_agrees(caplog):
    """A short that loses more than it posted used to floor the balance at
    zero, so total_pnl disagreed with the trade log."""
    trader = _trader(balance=1_000.0, fee_bps=0, slippage_bps=0)
    trader._execute_trade("t", Signal("BTC", "SELL", size_usd=1_000.0))
    _set_price(trader, 300.0)  # +200% against the short
    with caplog.at_level("WARNING"):
        trader._execute_trade("t", Signal("BTC", "BUY", size_usd=1_000.0))
    assert trader.balance == pytest.approx(1_000.0 - 2_000.0)
    assert "beyond the posted collateral" in caplog.text
    pnl, fees = _ledger(trader)
    assert trader.get_portfolio()["total_pnl"] == pytest.approx(pnl - fees)


def test_stop_closes_open_positions_in_the_log_and_it_balances(tmp_path):
    trader = _trader(balance=5_000.0)
    trader._execute_trade("a", Signal("BTC", "BUY", size_usd=1_000.0))
    trader._execute_trade("b", Signal("BTC", "SELL", size_usd=500.0))
    _set_price(trader, 105.0)
    trader._execute_trade("a", Signal("BTC", "BUY", size_usd=500.0))
    db = trader._db
    trader.close_all("session end: open position closed at exit")
    assert trader.positions == {}
    reasons = [r[0] for r in db.execute("SELECT reason FROM paper_trades ORDER BY id")]
    assert reasons[-2:] == ["session end: open position closed at exit"] * 2
    pnl, fees = _ledger(trader)
    assert trader.balance - trader.starting_balance == pytest.approx(pnl - fees)


def test_stop_runs_the_session_end_close(tmp_path):
    trader = _trader()
    trader._execute_trade("a", Signal("BTC", "BUY", size_usd=1_000.0))
    db = trader._db
    trader._db = None
    trader.db_path = tmp_path / "pt.db"

    async def run():
        await trader.start()
        trader._execute_trade("a", Signal("BTC", "BUY", size_usd=100.0))
        await trader.stop()

    asyncio.run(run())
    db.close()
    rows = sqlite3.connect(tmp_path / "pt.db").execute("SELECT action, reason FROM paper_trades").fetchall()
    assert rows[-1] == ("SELL", "session end: open position closed at exit")
    assert trader.positions == {}


def test_old_trade_log_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE paper_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL, "
        "strategy TEXT NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL, price REAL NOT NULL, "
        "size_usd REAL NOT NULL, confidence REAL NOT NULL, reason TEXT NOT NULL DEFAULT '', "
        "pnl REAL NOT NULL DEFAULT 0.0)"
    )
    old.execute("INSERT INTO paper_trades (timestamp, strategy, symbol, action, price, size_usd, confidence) "
                "VALUES (1, 's', 'BTC', 'BUY', 100, 100, 0.5)")
    old.commit()
    ensure_schema(old)
    cols = {r[1] for r in old.execute("PRAGMA table_info(paper_trades)")}
    assert {"session_id", "coins", "fee_usd", "mark_price"} <= cols
    assert old.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 1


@pytest.mark.parametrize("argv", [["paper", "--fee-bps", "-1"], ["paper", "--slippage-bps", "nan"]])
def test_cli_rejects_bad_cost_flags(argv):
    from hyperdata_terminal.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


def test_cli_cost_flags_parse():
    from hyperdata_terminal.cli import build_parser

    args = build_parser().parse_args(["paper", "--fee-bps", "0", "--slippage-bps", "5"])
    assert (args.fee_bps, args.slippage_bps) == (0.0, 5.0)
