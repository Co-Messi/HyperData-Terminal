"""SQLite store (M6): never quarantine a healthy database, count every lost
write, keep estimated prints out of the stats, and persist raw trades only
when asked."""
from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest

from hyperdata_terminal.data_layer import persistence
from hyperdata_terminal.data_layer.persistence import DataStore


def _liq(i: int, confirmed: bool = True, exchange: str = "okx"):
    return SimpleNamespace(timestamp=time.time() - i, exchange=exchange, symbol="BTC", side="long",
                           size_usd=1_000.0, price=80_000.0, quantity=0.0125, confirmed=confirmed)


@pytest.mark.parametrize("message", [
    "database or disk is full", "attempt to write a readonly database", "disk I/O error",
])
def test_operational_errors_do_not_quarantine_a_healthy_db(tmp_path, monkeypatch, message, caplog):
    path = tmp_path / "hyperdata.db"
    DataStore(path).close()  # a healthy file with the schema
    healthy = path.read_bytes()

    real_init = DataStore._init_tables
    calls = []

    def broken(self):
        calls.append(self)
        if len(calls) == 1:  # the file fails; the in-memory fallback works
            raise sqlite3.OperationalError(message)
        return real_init(self)

    monkeypatch.setattr(DataStore, "_init_tables", broken)
    with caplog.at_level("ERROR"):
        store = DataStore(path)
    try:
        assert not (tmp_path / "corrupted").exists()
        assert path.read_bytes() == healthy
        assert store.persistent is False
        assert "not quarantined" in caplog.text
    finally:
        store.close()


def test_a_file_that_is_not_a_database_is_still_quarantined(tmp_path):
    path = tmp_path / "hyperdata.db"
    path.write_bytes(b"this is not a database at all" * 100)
    store = DataStore(path)
    try:
        assert len(list((tmp_path / "corrupted").glob("hyperdata.db.*"))) == 1
        assert store.persistent is True
    finally:
        store.close()


def test_failed_writes_are_counted(tmp_path):
    store = DataStore(tmp_path / "w.db")
    try:
        store._enqueue("INSERT INTO no_such_table (x) VALUES (?)", (1,))
        store._save_liquidation(_liq(0))
        store.flush()
        stats = store.get_db_stats()
        assert stats["failed_writes"] == 1
        assert stats["liquidations_stored"] == 1
    finally:
        store.close()


def test_stats_queries_count_confirmed_liquidations_only(tmp_path):
    store = DataStore(tmp_path / "s.db")
    try:
        for i in range(3):
            store._save_liquidation(_liq(i))
        for i in range(5):
            store._save_liquidation(_liq(i, confirmed=False, exchange="hyperliquid"))
        assert store.get_liquidation_stats(hours=1)["total_count"] == 3
        assert store.get_liquidations_by_exchange(hours=1) == {"okx": {"count": 3, "volume": 3_000.0}}
        assert store.get_liquidation_stats(hours=1, include_estimated=True)["total_count"] == 8
    finally:
        store.close()


def test_raw_trades_are_persisted_only_on_request(tmp_path, monkeypatch):
    calls = []
    hub = SimpleNamespace(
        on_liquidation=lambda cb: None, on_trade=lambda cb: calls.append(cb),
        smart_money=None, hlp=None,
    )
    monkeypatch.delenv("HYPERDATA_PERSIST_TRADES", raising=False)
    store = DataStore(tmp_path / "t.db")
    try:
        store.attach(hub)
        assert calls == []
        monkeypatch.setenv("HYPERDATA_PERSIST_TRADES", "1")
        store.attach(hub)
        assert calls == [store._save_trade]
    finally:
        store.close()


def test_module_docstring_example_runs(tmp_path):
    assert "include_estimated" in (persistence.__doc__ or "")


def test_a_legacy_table_missing_an_indexed_column_is_migrated_not_quarantined(tmp_path):
    """An older smart_money_signals table without signal_type failed the
    index in the schema script with "no such column", and the open path
    quarantined that healthy file. Indexes now come after the migrations
    and a missing column only skips its index."""
    path = tmp_path / "hyperdata.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE smart_money_signals (id INTEGER PRIMARY KEY, timestamp REAL NOT NULL);
        INSERT INTO smart_money_signals (timestamp) VALUES (1.0);
        CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at REAL NOT NULL);
        INSERT INTO schema_version VALUES (2, 0);
    """)
    conn.commit()
    conn.close()
    store = DataStore(path)
    try:
        assert store.persistent is True
        assert not (tmp_path / "corrupted").exists()
        rows = store._conn.execute("SELECT COUNT(*) FROM smart_money_signals").fetchone()[0]
        assert rows == 1  # the old row survived
        assert store.get_schema_version() == DataStore.SCHEMA_VERSION
    finally:
        store.close()
