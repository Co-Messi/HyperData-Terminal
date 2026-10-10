"""HLP absorptions are grouped by transaction hash, coin and side (M9).

Captured: transaction 0xc7633001... carries GMT liquidation fills from
Strategy A and B next to unrelated STRK fills, so a hash does not identify
one coin."""
from __future__ import annotations

import copy
import sqlite3

from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker
from hyperdata_terminal.data_layer.persistence import DataStore
from tests.fixture_data import load_fixture


def _fills():
    return load_fixture("hyperliquid/hlp_fills_one_hash_two_coins.json")


def test_captured_hash_groups_only_its_liquidation_fills():
    tracker = HLPTracker()
    for vault, fills in _fills().items():
        tracker.process_fills(vault, fills)
    (absorption,) = tracker.absorptions.values()
    gmt = [f for fills in _fills().values() for f in fills if f.get("liquidation")]
    assert absorption.symbol == "GMT"
    assert absorption.size == sum(float(f["sz"]) for f in gmt)  # both vaults' shares, one liquidation
    assert absorption.vault.count(",") == 1


def test_two_coins_liquidated_in_one_transaction_stay_apart():
    """The captured transaction with its STRK fills flagged as liquidations
    too (a cross margin account liquidated in two coins in one transaction):
    the hash alone summed STRK and GMT sizes under GMT."""
    fills = copy.deepcopy(_fills())
    flag = next(f["liquidation"] for v in fills.values() for f in v if f.get("liquidation"))
    for v in fills.values():
        for f in v:
            if f["coin"] == "STRK":
                f["liquidation"] = flag
    tracker = HLPTracker()
    for vault, vault_fills in fills.items():
        tracker.process_fills(vault, vault_fills)
    by_coin = {a.symbol: a for a in tracker.absorptions.values()}
    assert set(by_coin) == {"GMT", "STRK"}
    strk = [f for v in fills.values() for f in v if f["coin"] == "STRK"]
    assert by_coin["STRK"].size == sum(float(f["sz"]) for f in strk)


def test_stored_plain_hashes_are_rewritten_to_the_absorption_key(tmp_path):
    path = tmp_path / "hyperdata.db"
    DataStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO hlp_trades (timestamp, symbol, side, price, size, size_usd, direction, closed_pnl, "
                 "is_liquidation, created_at, fill_hash, vault) VALUES (1, 'GMT', 'sell', 1, 2, 2, 'x', 0, 1, 1, "
                 "'0xc76330', 'A')")
    conn.execute("DELETE FROM schema_version WHERE version = 9")
    conn.commit()
    conn.close()
    store = DataStore(path)
    try:
        (key,) = store._conn.execute("SELECT fill_hash FROM hlp_trades").fetchone()
        assert key == "0xc76330|GMT|sell"
    finally:
        store.close()
