"""Regression tests for the third adversarial review (.roast/REPORT-latest.md).

One class per finding ID. Each test failed against the pre-fix tree and
passes after the fix; the docstrings say what the pre-fix behavior was.
"""
from __future__ import annotations

import sqlite3

import pytest

from src.data_layer import address_store
from src.data_layer.persistence import DataStore


@pytest.fixture
def isolated_address_store(tmp_path, monkeypatch):
    monkeypatch.setattr(address_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(address_store, "DB_PATH", tmp_path / "hyperdata.db")
    monkeypatch.setattr(address_store, "LEGACY_JSON", tmp_path / "legacy.json")
    monkeypatch.setattr(address_store, "_initialized", False)
    return tmp_path


def _addr(seed: str) -> str:
    return "0x" + (seed * 40)[:40]


def _tables(path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


# ── M1: dead tables / dead migration ─────────────────────────────

class TestM1DeadSchema:
    def test_fresh_db_has_no_dead_tables(self, tmp_path):
        """Pre-fix: `snapshots` and `paper_trades` were created on every fresh
        DB although nothing in src/ ever wrote to them."""
        store = DataStore(tmp_path / "fresh.db")
        store.close()
        tables = _tables(tmp_path / "fresh.db")
        assert "snapshots" not in tables
        assert "paper_trades" not in tables
        assert "discovered_addresses" in tables   # M10: now in the versioned schema
        assert not hasattr(DataStore, "save_paper_trade")

    def test_v2_db_upgrades_and_drops_empty_dead_tables(self, tmp_path):
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE snapshots (id INTEGER PRIMARY KEY, timestamp REAL);
            CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, timestamp REAL);
            CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at REAL NOT NULL);
            INSERT INTO schema_version VALUES (2, 0);
        """)
        conn.commit()
        conn.close()

        store = DataStore(path)
        try:
            assert store.get_schema_version() == DataStore.SCHEMA_VERSION
        finally:
            store.close()
        tables = _tables(path)
        assert "snapshots" not in tables and "paper_trades" not in tables

    def test_non_empty_legacy_table_is_preserved(self, tmp_path):
        """Dropping user data is never the migration's call — a populated
        legacy table is left alone and reported."""
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE snapshots (id INTEGER PRIMARY KEY, timestamp REAL);
            INSERT INTO snapshots VALUES (1, 0);
        """)
        conn.commit()
        conn.close()
        DataStore(path).close()
        assert "snapshots" in _tables(path)

    def test_migrations_are_version_gated(self, tmp_path):
        """Pre-fix: every migration ran on every startup and `current` was
        never consulted. Now a DB already at SCHEMA_VERSION runs nothing."""
        path = tmp_path / "gated.db"
        DataStore(path).close()
        ran = []
        store = DataStore.__new__(DataStore)
        # Re-open by hand so we can spy on the migration table.
        store.db_path = path
        store._lock = __import__("threading").Lock()
        store._conn = sqlite3.connect(str(path), check_same_thread=False)
        store._MIGRATIONS = {DataStore.SCHEMA_VERSION: lambda self: ran.append(1)}
        store._run_migrations()
        store._conn.close()
        assert ran == []


# ── M10: address_store failure policy ────────────────────────────

class TestM10AddressStore:
    def test_read_failure_raises_not_empty_set(self, isolated_address_store):
        """Pre-fix: an unreadable store returned set() and the scanner
        silently re-discovered from scratch."""
        (isolated_address_store / "hyperdata.db").write_bytes(b"garbage" * 200)
        with pytest.raises(sqlite3.Error):
            address_store.get_all_addresses()

    def test_write_failure_logged_at_warning(self, isolated_address_store, caplog, monkeypatch):
        address_store.add_addresses([_addr("a")], source="t")  # init OK

        def boom():
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(address_store, "_get_conn", boom)
        with caplog.at_level("WARNING", logger="src.data_layer.address_store"):
            assert address_store.add_addresses([_addr("b")], source="t") == 0
        assert "NOT persisted" in caplog.text

    def test_generator_input_counts_dropped_correctly(self, isolated_address_store, caplog):
        """L2: `dropped` was computed after the comprehension consumed the
        iterable, going negative for generators."""
        gen = (a for a in [_addr("c"), "junk", "0xshort"])
        with caplog.at_level("WARNING", logger="src.data_layer.address_store"):
            assert address_store.add_addresses(gen, source="t") == 1
        assert "dropped 2 invalid" in caplog.text

    def test_hub_opens_datastore_before_scanner(self):
        """Ordering guard: DataStore must be constructed before PositionScanner
        so a corrupted DB is quarantined before address_store touches it."""
        import inspect

        from src.data_layer.hub import HyperDataHub
        src = inspect.getsource(HyperDataHub.__init__)
        assert src.index("self.store = DataStore()") < src.index("self.positions = PositionScanner()")
