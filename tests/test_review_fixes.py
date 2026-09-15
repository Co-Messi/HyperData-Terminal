"""Regression tests for the third adversarial review (.roast/REPORT-latest.md).

One class per finding ID. A test whose docstring starts with "Pre-fix:"
(or names the pre-fix behaviour) failed against the pre-fix tree and
passes after the fix. A test whose docstring starts with "Control:" or
"Guard:" passed BEFORE the fix too: it pins behaviour the fix must not
break (the other side of a branch, a bound, a preserved invariant) and is
kept deliberately — it is not evidence that the finding was real, the
"Pre-fix:" test next to it is.
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


def _scanner(monkeypatch, n_addresses: int = 0):
    """A real PositionScanner that never touches the repo's SQLite file."""
    monkeypatch.setattr(address_store, "get_all_addresses", lambda: set())
    from src.data_layer.position_scanner import PositionScanner
    s = PositionScanner()
    s.discovered_addresses = {f"0x{i:040x}" for i in range(n_addresses)}
    return s


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
        assert "wallets" not in tables            # S2: dead too, dropped in v4
        assert "discovered_addresses" in tables   # M10: now in the versioned schema
        assert not hasattr(DataStore, "save_paper_trade")
        assert not hasattr(DataStore, "save_wallet") and not hasattr(DataStore, "load_wallets")

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
        """Control: passed pre-fix too (nothing dropped anything then).
        Dropping user data is never the migration's call — a populated
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
        never consulted. Now a step runs exactly once — when the DB is below
        its version — and never again once the version is recorded."""
        import threading

        path = tmp_path / "gated.db"
        ran: list[int] = []

        def _open() -> DataStore:
            store = DataStore.__new__(DataStore)
            store.db_path = path
            store._lock = threading.Lock()
            store._conn = sqlite3.connect(str(path), check_same_thread=False)
            store._MIGRATIONS = {DataStore.SCHEMA_VERSION: lambda self: ran.append(1)}
            return store

        # DB one version behind: the step must run once and stamp the version.
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at REAL NOT NULL)")
        conn.execute("INSERT INTO schema_version VALUES (?, 0)", (DataStore.SCHEMA_VERSION - 1,))
        conn.commit()
        conn.close()
        store = _open()
        store._run_migrations()
        store._conn.commit()
        store._conn.close()
        assert ran == [1]

        # Already current: nothing runs.
        store = _open()
        store._run_migrations()
        store._conn.close()
        assert ran == [1]


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


# ── C1: proportional tiers + confidence surfaced ─────────────────

class TestC1Tiers:
    @staticmethod
    def _engine_with(n: int):
        from data_layer.smart_money import SmartMoneyEngine, WalletProfile
        engine = SmartMoneyEngine()
        for i in range(n):
            addr = f"0x{i:040x}"
            engine.wallets[addr] = WalletProfile(
                address=addr, discovered_at=0, last_seen=0, last_analyzed=0,
                total_trades=20, total_volume_usd=1e6,
                composite_score=1.0 - i / max(n, 1),  # strictly decreasing
            )
        engine.rank_all()
        return engine

    @staticmethod
    def _counts(engine) -> dict[str, int]:
        out = {"smart": 0, "average": 0, "dumb": 0, "unknown": 0}
        for w in engine.wallets.values():
            out[w.tier] += 1
        return out

    @pytest.mark.parametrize("n,smart,dumb", [
        (50, 5, 5),
        (150, 15, 15),
        (250, 25, 25),
        (2000, 100, 100),   # caps engage
    ])
    def test_tiers_are_proportional(self, n, smart, dumb):
        """Pre-fix: 50 qualified -> all 50 'smart'; 150 -> 100 smart + 50
        dumb and zero 'average'. The `else` branch was unreachable below 200."""
        counts = self._counts(self._engine_with(n))
        assert counts["smart"] == smart
        assert counts["dumb"] == dumb
        assert counts["average"] == n - smart - dumb
        assert counts["average"] > 0

    def test_worst_wallet_is_never_smart(self):
        engine = self._engine_with(50)
        worst = min(engine.wallets.values(), key=lambda w: w.composite_score)
        assert worst.rank == 50
        assert worst.tier == "dumb"
        best = max(engine.wallets.values(), key=lambda w: w.composite_score)
        assert best.rank == 1 and best.tier == "smart"

    def test_below_minimum_population_all_average_but_ranked(self):
        engine = self._engine_with(9)
        counts = self._counts(engine)
        assert counts == {"smart": 0, "average": 9, "dumb": 0, "unknown": 0}
        assert sorted(w.rank for w in engine.wallets.values()) == list(range(1, 10))
        # Exactly at the minimum, a top/bottom decile exists.
        assert self._counts(self._engine_with(10)) == {"smart": 1, "average": 8, "dumb": 1, "unknown": 0}

    def test_stats_report_average_tier(self):
        stats = self._engine_with(50).get_stats()
        assert stats["average_wallets"] == 40
        assert stats["ranking_criteria"]["tier_fraction"] == 0.1

    @pytest.mark.asyncio
    async def test_signal_carries_wallet_confidence(self):
        """Pre-fix: WalletProfile.confidence was computed and consumed by
        nothing — no signal field, no DB column, no panel."""
        import time as _t

        from data_layer.smart_money import SmartMoneySignal
        engine = self._engine_with(50)
        top = next(w for w in engine.wallets.values() if w.rank == 1)
        top.confidence = 0.4
        got = []
        engine.on_signal(got.append)
        await engine.check_signals(top.address, [{
            "time": _t.time() * 1000, "dir": "Open Long", "coin": "BTC", "px": "1", "sz": "1",
        }])
        assert len(got) == 1
        assert got[0].wallet_confidence == 0.4
        assert "wallet_confidence" in SmartMoneySignal.__dataclass_fields__

    def test_confidence_persisted_with_signal(self, tmp_path):
        """Signals are the ONLY persisted carrier of a tier, so the
        confidence rides with them. (Wallet profiles are not persisted —
        see TestS2NoDeadWalletTable.)"""
        from types import SimpleNamespace

        store = DataStore(tmp_path / "w.db")
        try:
            sig = SimpleNamespace(timestamp=1.0, address="0x" + "a" * 40, tier="smart", action="OPEN_LONG",
                                  symbol="BTC", size_usd=1.0, wallet_rank=1, signal_type="follow",
                                  wallet_confidence=0.7)
            store.save_signal(sig)
            assert store.get_signals(hours=1e9)[0]["wallet_confidence"] == 0.7
        finally:
            store.close()

    def test_legacy_signals_table_gains_confidence_column(self, tmp_path):
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE smart_money_signals (id INTEGER PRIMARY KEY, timestamp REAL NOT NULL);
            CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at REAL NOT NULL);
            INSERT INTO schema_version VALUES (2, 0);
        """)
        conn.commit()
        conn.close()
        DataStore(path).close()
        conn = sqlite3.connect(str(path))
        sig_cols = {r[1] for r in conn.execute("PRAGMA table_info(smart_money_signals)")}
        conn.close()
        assert "wallet_confidence" in sig_cols

    def test_smart_money_panel_shows_confidence(self):
        from unittest.mock import MagicMock

        from rich.console import Console

        from data_layer.smart_money import SmartMoneySignal
        from src.dashboards.hub_panels import HubSmartMoney
        engine = self._engine_with(50)
        for w in engine.wallets.values():
            w.confidence = 0.42
        hub = MagicMock()
        hub.smart_money = engine
        hub.get_smart_money = engine.get_smart_money
        hub.get_dumb_money = engine.get_dumb_money
        hub.get_smart_money_signals = lambda n=50: [SmartMoneySignal(
            timestamp=0, address="0x" + "b" * 40, tier="smart", action="OPEN_LONG", symbol="BTC",
            size_usd=1.0, wallet_rank=1, wallet_win_rate=0.5, wallet_pnl=1.0, signal_type="follow",
            wallet_confidence=0.42,
        )]
        console = Console(record=True, width=160, force_terminal=False)
        console.print(HubSmartMoney(hub).build_compact())
        text = console.export_text()
        assert "CONF" in text
        assert text.count("42%") >= 9   # 5 smart + 3 dumb rows + the signal line


# ── S2: wallets.confidence was migrated and written by a method nothing called ──

class TestS2NoDeadWalletTable:
    """Pre-fix: DataStore.save_wallet/load_wallets existed, v3 added a
    `confidence` column to `wallets`, and attach() never registered a
    wallet callback — so the column was 0.0 forever and the table had no
    writer anywhere in src/. Chosen fix: remove, not wire (SmartMoneyEngine
    recomputes profiles from fills every session; loading persisted scores
    whose formula changed would have been the dishonest option)."""

    def test_empty_legacy_wallets_table_is_dropped(self, tmp_path, caplog):
        path = tmp_path / "v3.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE wallets (address TEXT PRIMARY KEY, tier TEXT, confidence REAL DEFAULT 0.0);
            CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at REAL NOT NULL);
            INSERT INTO schema_version VALUES (3, 0);
        """)
        conn.commit()
        conn.close()
        with caplog.at_level("INFO"):
            store = DataStore(path)
            try:
                assert store.get_schema_version() == DataStore.SCHEMA_VERSION == 4
            finally:
                store.close()
        assert "wallets" not in _tables(path)
        assert "Dropped empty legacy table wallets" in caplog.text

    def test_populated_legacy_wallets_table_is_kept_and_reported(self, tmp_path, caplog):
        """Control: dropping user data is never the migration's call."""
        path = tmp_path / "v3.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE wallets (address TEXT PRIMARY KEY, tier TEXT, confidence REAL DEFAULT 0.0);
            INSERT INTO wallets VALUES ('0xabc', 'smart', 0.5);
            CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at REAL NOT NULL);
            INSERT INTO schema_version VALUES (3, 0);
        """)
        conn.commit()
        conn.close()
        with caplog.at_level("WARNING"):
            DataStore(path).close()
        assert "wallets" in _tables(path)
        assert "Legacy table wallets has 1 rows" in caplog.text

    def test_no_wallet_persistence_surface_remains(self):
        import inspect

        from src.data_layer import persistence, smart_money
        assert "wallets" not in inspect.getsource(persistence.DataStore._init_tables).replace(
            "no `wallets` table", "")
        assert not hasattr(persistence.DataStore, "save_wallet")
        assert not hasattr(persistence.DataStore, "load_wallets")
        assert "persisted DB column" not in inspect.getsource(smart_money)

# ── C2 / M7: no wildcard CORS on loopback, Host guard, one origin policy ──

async def _loopback_client(monkeypatch, cors_env: str = ""):
    """A test server wired exactly like HyperDataAPI.start() on 127.0.0.1."""
    from unittest.mock import MagicMock

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from src.api_server import (
        HyperDataAPI,
        _make_cors_middleware,
        _make_host_guard_middleware,
        _make_rate_limit_middleware,
    )
    monkeypatch.delenv("HYPERDATA_API_KEY", raising=False)
    monkeypatch.delenv("HYPERDATA_UNSAFE_PUBLIC_API", raising=False)
    if cors_env:
        monkeypatch.setenv("HYPERDATA_CORS_ORIGINS", cors_env)
    else:
        monkeypatch.delenv("HYPERDATA_CORS_ORIGINS", raising=False)
    api = HyperDataAPI(hub=MagicMock(), host="127.0.0.1")
    _, origins = api._resolve_security()
    api._cors_origins = origins
    app = web.Application(middlewares=[
        _make_host_guard_middleware("127.0.0.1"),
        _make_rate_limit_middleware(api._rate_limiter),
        _make_cors_middleware(origins),
    ])

    async def whales(request):
        return web.json_response({"positions": [{"address": "0xsecret"}]})

    app.router.add_get("/v1/whales", whales)
    client = TestClient(TestServer(app))
    await client.start_server()
    return api, client


class TestC2LoopbackCORS:
    @pytest.mark.asyncio
    async def test_no_wildcard_and_no_grant_to_unlisted_origin(self, monkeypatch):
        """Pre-fix: every loopback response carried Access-Control-Allow-Origin: *
        so any web page could fetch() /v1/whales and read the addresses."""
        _, client = await _loopback_client(monkeypatch)
        try:
            for headers in ({}, {"Origin": "https://evil.example"}):
                resp = await client.get("/v1/whales", headers=headers)
                assert resp.status == 200
                assert "Access-Control-Allow-Origin" not in resp.headers, headers
            pre = await client.options("/v1/whales", headers={"Origin": "https://evil.example"})
            assert pre.headers.get("Access-Control-Allow-Origin") != "*"
            assert "Access-Control-Allow-Origin" not in pre.headers
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_allowlisted_origin_is_echoed_not_wildcarded(self, monkeypatch):
        _, client = await _loopback_client(monkeypatch, cors_env="http://localhost:3000")
        try:
            ok = await client.get("/v1/whales", headers={"Origin": "http://localhost:3000"})
            assert ok.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"
            assert ok.headers.get("Vary") == "Origin"
            bad = await client.get("/v1/whales", headers={"Origin": "https://evil.example"})
            assert "Access-Control-Allow-Origin" not in bad.headers
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_host_header_must_be_loopback(self, monkeypatch):
        """DNS rebinding: evil.example resolving to 127.0.0.1 sends
        `Host: evil.example`. Pre-fix there was no Host validation at all."""
        _, client = await _loopback_client(monkeypatch)
        try:
            for good in ("127.0.0.1:8420", "localhost", "localhost:1", "[::1]:8420", "127.0.0.1"):
                resp = await client.get("/v1/whales", headers={"Host": good})
                assert resp.status == 200, good
            for bad in ("evil.example", "evil.example:8420", "192.168.1.5:8420", "", "127.0.0.1.evil.example"):
                resp = await client.get("/v1/whales", headers={"Host": bad})
                assert resp.status == 403, bad
        finally:
            await client.close()

    def test_host_guard_only_on_loopback_bind(self):
        from src.api_server import _host_header_is_loopback, _make_host_guard_middleware
        assert _make_host_guard_middleware("0.0.0.0") is None
        assert _make_host_guard_middleware("127.0.0.1") is not None
        assert _host_header_is_loopback("::1")
        assert not _host_header_is_loopback("[::1")          # malformed
        assert not _host_header_is_loopback("localhost.evil.example")

    @pytest.mark.asyncio
    async def test_m7_rest_and_ws_read_one_allowlist(self, monkeypatch):
        """M7: REST CORS and the WebSocket Origin gate must agree. Pre-fix,
        loopback REST was wildcard-open while WS rejected every origin."""
        from unittest.mock import MagicMock

        api, client = await _loopback_client(monkeypatch, cors_env="http://localhost:3000")
        try:
            allowed = {"Origin": "http://localhost:3000"}
            denied = {"Origin": "https://evil.example"}
            assert (await client.get("/v1/whales", headers=allowed)).headers.get(
                "Access-Control-Allow-Origin") == "http://localhost:3000"
            assert "Access-Control-Allow-Origin" not in (await client.get("/v1/whales", headers=denied)).headers

            req = MagicMock()
            req.headers = denied
            assert (await api.handle_ws(req)).status == 403
            api._ws_clients = [MagicMock()] * 100   # past the origin gate -> connection cap
            req.headers = allowed
            assert (await api.handle_ws(req)).status == 429
        finally:
            await client.close()


# ── C3 / H2 / M11: a connected-but-silent venue must be visible everywhere ──

def _isolated_hub(tmp_path, monkeypatch):
    """A HyperDataHub whose SQLite files live in tmp_path (no network)."""
    from src.data_layer import persistence
    monkeypatch.setattr(persistence, "DB_PATH", tmp_path / "hub.db")
    monkeypatch.setattr(address_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(address_store, "DB_PATH", tmp_path / "hub.db")
    monkeypatch.setattr(address_store, "LEGACY_JSON", tmp_path / "legacy.json")
    monkeypatch.setattr(address_store, "_initialized", False)
    from src.data_layer.hub import HyperDataHub
    return HyperDataHub()


def _hl_trade_frame(tid: int = 1) -> dict:
    return {"channel": "trades", "data": [
        {"coin": "BTC", "px": "80000", "sz": "0.1", "side": "B", "time": 1700000000000, "tid": tid},
    ]}


def _silent_binance_engine(now: float):
    """HL flowing; Binance connected 120s ago with ZERO frames — the live case."""
    from src.data_layer.orderflow_engine import OrderFlowEngine
    e = OrderFlowEngine(symbols=["BTC", "ETH", "SOL"])
    e._venue_connected("hyperliquid")
    e._handle_message(_hl_trade_frame())
    e._venue_connected("binance")
    e.venues["binance"].connected_at = now - 120
    return e


class TestC3VenueTruth:
    def test_status_machine(self):
        import time as _t

        from src.data_layer.orderflow_engine import OrderFlowEngine
        now = _t.time()
        e = OrderFlowEngine(symbols=["BTC"])
        assert e.venue_status("binance", now) == ("disconnected", "never connected")

        e._venue_connected("binance")
        assert e.venue_status("binance", now)[0] == "connecting"           # inside grace
        e.venues["binance"].connected_at = now - 60
        assert e.venue_status("binance", now)[0] == "silent"               # 0 frames, past grace
        assert "0 frames" in e.venue_status("binance", now)[1]

        e._handle_binance_trade({"data": {"s": "BTCUSDT", "p": "1", "q": "1", "m": False,
                                          "T": 1700000000000, "a": 1}})
        assert e.venue_status("binance", now)[0] == "ok"

        e.venues["binance"].connected_at = now - 200                       # long-lived connection...
        e.last_binance_message_at = now - 100                              # ...quiet, no frames either
        e.venues["binance"].last_frame_at = now - 100
        assert e.venue_status("binance", now)[0] == "stale"

        e.venues["binance"].last_frame_at = now - 1                        # frames flow, no trades parse
        assert e.venue_status("binance", now)[0] == "frozen"

        e._venue_disconnected("binance")
        assert e.venue_status("binance", now)[0] == "disconnected"

    @pytest.mark.asyncio
    async def test_hub_watchdog_warns_on_never_connected_silent_venue(self, tmp_path, monkeypatch, caplog):
        """Pre-fix: the `venue_data_age(venue) != float('inf')` guard excluded
        exactly the venue that never delivered a byte, so this warned never.
        The status also read 'connected' (not 'partial')."""
        import time as _t

        hub = _isolated_hub(tmp_path, monkeypatch)
        try:
            hub.orderflow = _silent_binance_engine(_t.time())
            hub.status.orderflow_engine = "connected"
            with caplog.at_level("WARNING"):
                await hub._update_feed_staleness()
            assert hub.status.orderflow_engine == "partial"
            assert "binance is silent" in caplog.text
            assert "0 frames received" in caplog.text
            assert "reflect hyperliquid" in caplog.text
        finally:
            hub.store.close()

    def test_health_monitor_emits_per_venue_checks(self, monkeypatch):
        import time as _t
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from src.data_layer.health_monitor import DataHealthMonitor
        now = _t.time()
        e = _silent_binance_engine(now)
        hub = SimpleNamespace(
            orderflow=e,
            orderbook=MagicMock(is_stale=lambda: False, data_age=lambda: 1.0),
            status=SimpleNamespace(last_market_refresh=now),
            deribit=MagicMock(get_latest=lambda s: None),
            positions=_scanner(monkeypatch),
        )
        checks = {c.name: c for c in DataHealthMonitor(hub)._check_freshness()}
        assert checks["order_flow"].status == "pass"          # blended: HL is flowing
        assert "hyperliquid" in checks["order_flow"].detail
        assert checks["order_flow_hyperliquid"].status == "pass"
        assert checks["order_flow_binance"].status == "warn"
        assert checks["order_flow_binance"].detail.startswith("silent:")

        # Everything dead -> the venue checks fail too, not just warn.
        e.last_hl_message_at = now - 1000
        e.venues["hyperliquid"].last_frame_at = now - 1000
        checks = {c.name: c for c in DataHealthMonitor(hub)._check_freshness()}
        assert checks["order_flow"].status == "fail"
        assert checks["order_flow_hyperliquid"].status == "fail"
        assert checks["order_flow_binance"].status == "fail"

    @pytest.mark.asyncio
    async def test_orderflow_endpoint_has_per_venue_cvd_and_coverage(self):
        import time as _t
        from unittest.mock import MagicMock

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from src.api_server import HyperDataAPI
        e = _silent_binance_engine(_t.time())
        hub = MagicMock()
        hub.orderflow = e
        api = HyperDataAPI(hub=hub)
        app = web.Application()
        app.router.add_get("/v1/orderflow/{symbol}", api.handle_orderflow)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            body = await (await client.get("/v1/orderflow/BTC")).json()
        finally:
            await client.close()
        assert body["cumulative_cvd"] == pytest.approx(8000.0)
        assert body["cumulative_cvd_by_venue"] == {"hyperliquid": pytest.approx(8000.0), "binance": 0.0}
        assert body["venue_coverage"] == {"hyperliquid": "ok", "binance": "silent"}
        assert body["venues_contributing"] == ["hyperliquid"]

    @pytest.mark.asyncio
    async def test_health_endpoint_reports_silent_venue_without_bare_except(self, monkeypatch):
        """L7/C3: /v1/health must carry the per-venue truth, and a broken
        venue_freshness() must raise rather than become `null`."""
        import time as _t
        from unittest.mock import MagicMock

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from src.api_server import HyperDataAPI
        from src.data_layer.hub import HubStatus
        hub = MagicMock()
        hub.status = HubStatus(mode="live")
        hub.orderflow = _silent_binance_engine(_t.time())
        hub.positions = _scanner(monkeypatch)
        hub.health.latest.return_value = None
        api = HyperDataAPI(hub=hub)
        app = web.Application()
        app.router.add_get("/v1/health", api.handle_health)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            body = await (await client.get("/v1/health")).json()
            venues = body["orderflow_venues"]
            assert venues["binance"]["status"] == "silent"
            assert venues["binance"]["frames"] == 0
            assert venues["binance"]["connected"] is True
            assert venues["hyperliquid"]["status"] == "ok"

            hub.orderflow = MagicMock()
            hub.orderflow.venue_freshness.side_effect = RuntimeError("boom")
            assert (await client.get("/v1/health")).status == 500
        finally:
            await client.close()

    def test_cvd_dashboard_price_bar_shows_venue_attribution(self):
        import time as _t

        from rich.console import Console

        from src.dashboards.cvd_dashboard import CVDDashboard
        e = _silent_binance_engine(_t.time())
        dash = CVDDashboard(engine=e, symbol="BTC")
        console = Console(record=True, width=200, force_terminal=False)
        console.print(dash.build_price_bar())
        text = console.export_text()
        assert "CVD: +8,000" in text
        assert "HL +8,000" in text
        assert "BN silent" in text

    def test_hub_cvd_panel_shows_venue_attribution(self):
        import time as _t
        from unittest.mock import MagicMock

        from rich.console import Console

        from src.dashboards.hub_panels import HubCVD
        e = _silent_binance_engine(_t.time())
        hub = MagicMock()
        hub.orderflow = e
        hub.market.assets = {}
        hub.status.total_trades_processed = 1
        console = Console(record=True, width=200, force_terminal=False)
        console.print(HubCVD(hub).build_compact())
        text = console.export_text()
        assert "CVD:+8,000" in text
        assert "BN silent" in text

    def test_demo_engine_is_labelled_synthetic_not_attributed(self):
        from rich.console import Console

        from src.dashboards.cvd_dashboard import CVDDashboard
        dash = CVDDashboard(demo=True, symbol="BTC")
        assert dash.engine.synthetic is True
        console = Console(record=True, width=200, force_terminal=False)
        console.print(dash.build_price_bar())
        assert "[DEMO]" in console.export_text()

    def test_health_badge_warn_is_partial_not_live(self):
        from unittest.mock import MagicMock

        from src.dashboards.combined_dashboard import CombinedDashboard
        dash = CombinedDashboard.__new__(CombinedDashboard)
        dash.hub = MagicMock()
        for overall, expected in (("ok", "LIVE"), ("warn", "PARTIAL"), ("stale", "STALE")):
            dash.hub.health.latest.return_value = {"overall": overall}
            label, _ = dash._health_badge()
            assert expected in label, overall
        assert "LIVE" not in dash._health_badge()[0] if dash.hub.health.latest.return_value else True

    @pytest.mark.asyncio
    async def test_binance_loop_logs_exception_detail(self, monkeypatch, caplog):
        """Pre-fix: `except Exception: logger.warning("Error, reconnecting")`
        carried no exception type or message."""
        import asyncio

        from src.data_layer import orderflow_engine as oe

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def ws_connect(self, *a, **kw):
                raise RuntimeError("boom-451")

        monkeypatch.setattr(oe.aiohttp, "ClientSession", FakeSession)
        e = oe.OrderFlowEngine(symbols=["BTC"])
        e._running = True
        with caplog.at_level("WARNING"):
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(e._binance_trade_loop(), timeout=0.2)
        assert "RuntimeError: boom-451" in caplog.text
        assert e.venues["binance"].connected is False


class TestH2FrameAccounting:
    def _engine(self):
        from src.data_layer.orderflow_engine import OrderFlowEngine
        e = OrderFlowEngine(symbols=["BTC"])
        e._venue_connected("binance")
        return e

    def test_ack_frames_count_as_frames_not_liveness(self):
        """Pre-fix: frames without `data` returned before any bookkeeping, so
        a stream of acks/error envelopes was indistinguishable from silence."""
        e = self._engine()
        e._handle_binance_trade({"result": None, "id": 1})
        e._handle_binance_trade({"error": {"code": 2, "msg": "Invalid request"}})
        st = e.venues["binance"]
        assert st.frames == 2
        assert st.trades == 0
        assert st.last_frame_at > 0
        assert e.last_binance_message_at == 0.0          # NOT stamped by an ack

    def test_schema_change_is_counted_logged_and_never_stamps_liveness(self, caplog):
        """Pre-fix: liveness was stamped BEFORE parsing and the parse error was
        a bare `pass` — 'fresh but frozen' with zero log output."""
        e = self._engine()
        renamed = {"data": {"s": "BTCUSDT", "price": "80000", "q": "0.1", "m": False, "T": 1700000000000}}
        with caplog.at_level("WARNING"):
            for _ in range(5):
                e._handle_binance_trade(renamed)
        st = e.venues["binance"]
        assert st.parse_errors == 5
        assert st.trades == 0
        assert e.last_binance_message_at == 0.0
        assert "KeyError" in caplog.text and "parse errors so far" in caplog.text
        assert caplog.text.count("failed to parse trade frame") == 1   # rate-limited

        # Past the grace period, with frames still arriving but nothing parsing,
        # this reads as 'frozen' with the count in the reason.
        import time as _t
        now = _t.time() + 60
        e.venues["binance"].last_frame_at = now - 1
        status, reason = e.venue_status("binance", now)
        assert status == "frozen"
        assert "5 parse errors" in reason
        fresh = e.venue_freshness(now)["binance"]
        assert fresh["parse_errors"] == 5 and fresh["frames"] == 5 and fresh["trades"] == 0

    def test_hl_parse_errors_counted_too(self):
        e = self._engine()
        e._venue_connected("hyperliquid")
        e._handle_message({"channel": "trades", "data": [{"coin": "BTC", "px": "bad", "sz": "1",
                                                          "side": "B", "time": 1, "tid": 9}]})
        assert e.venues["hyperliquid"].parse_errors == 1
        assert e.last_hl_message_at == 0.0
        e._handle_message(_hl_trade_frame(tid=10))
        assert e.venues["hyperliquid"].trades == 1
        assert e.last_hl_message_at > 0


class TestM11ConnectingStatus:
    @pytest.mark.asyncio
    async def test_feeds_promote_from_connecting_on_first_data(self, tmp_path, monkeypatch):
        """Pre-fix: on_ok set 'connected' the moment start() returned — before
        any socket opened — and the watchdog only handled 'connected'/'stale'."""
        import time as _t

        hub = _isolated_hub(tmp_path, monkeypatch)
        try:
            s = hub.status
            s.started_at = _t.time()   # hub just started: sockets still opening
            s.orderflow_engine = s.orderbook_feed = s.liquidation_feed = s.hlp_status = "connecting"
            await hub._update_feed_staleness()
            # Nothing has arrived: everything stays 'connecting' (never 'connected').
            assert s.orderflow_engine == "connecting"
            assert s.orderbook_feed == "connecting"
            assert s.liquidation_feed == "connecting"
            assert s.hlp_status == "connecting"

            e = hub.orderflow
            e._venue_connected("hyperliquid")
            e._venue_connected("binance")
            e._handle_message(_hl_trade_frame())
            e._handle_binance_trade({"data": {"s": "BTCUSDT", "p": "1", "q": "1", "m": False,
                                              "T": 1700000000000, "a": 1}})
            hub.orderbook.last_message_at = __import__("time").time()
            s.last_liq_event = 1.0
            hub.hlp.snapshots.append(object())
            await hub._update_feed_staleness()
            assert s.orderflow_engine == "connected"
            assert s.orderbook_feed == "connected"
            assert s.liquidation_feed == "connected"
            assert s.hlp_status == "connected"
        finally:
            hub.store.close()

    @pytest.mark.asyncio
    async def test_never_any_trade_past_grace_is_stale_not_connected(self, tmp_path, monkeypatch):
        import time as _t

        hub = _isolated_hub(tmp_path, monkeypatch)
        try:
            e = hub.orderflow
            for v in ("hyperliquid", "binance"):
                e._venue_connected(v)
                e.venues[v].connected_at = _t.time() - 120
            hub.status.orderflow_engine = "connecting"
            await hub._update_feed_staleness()
            assert hub.status.orderflow_engine == "stale"
        finally:
            hub.store.close()


# ── H7 / M12: /v1/health top-level status and docs URL ───────────

class TestH7HealthStatus:
    @pytest.fixture(autouse=True)
    def _mp(self, monkeypatch):
        self.monkeypatch = monkeypatch

    async def _health(self, mode: str, data_health, feed_overrides: dict | None = None,
                      failed: list | None = None) -> dict:
        from unittest.mock import MagicMock

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from src.api_server import HyperDataAPI
        from src.data_layer.hub import HubStatus
        from src.data_layer.orderflow_engine import OrderFlowEngine
        hub = MagicMock()
        hub.status = HubStatus(mode=mode, **(feed_overrides or {}))
        hub.status.failed_components = list(failed or [])
        hub.orderflow = OrderFlowEngine(symbols=["BTC"])
        hub.positions = _scanner(self.monkeypatch)
        hub.health.latest.return_value = data_health
        app = web.Application()
        app.router.add_get("/v1/health", HyperDataAPI(hub=hub).handle_health)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return await (await client.get("/v1/health")).json()
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_no_checks_yet_is_initializing_not_ok(self):
        """Pre-fix: the first ~45s of every live session reported 'ok'."""
        assert (await self._health("live", None))["status"] == "initializing"
        # Demo mode never runs the monitor; that is not "initializing".
        assert (await self._health("demo", None))["status"] == "ok"

    @pytest.mark.asyncio
    async def test_warn_is_not_ok(self):
        """Pre-fix: 'warn' (BTC price unavailable, no funding symbols, one
        venue silent, ...) mapped to top-level 'ok'."""
        assert (await self._health("live", {"overall": "warn"}))["status"] == "warn"
        assert (await self._health("live", {"overall": "ok"}))["status"] == "ok"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("overall", ["stale", "drift", "fail"])
    async def test_bad_overall_is_degraded(self, overall):
        assert (await self._health("live", {"overall": overall}))["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_feed_states_feed_into_status(self):
        ok = {"overall": "ok"}
        assert (await self._health("live", ok, {"orderflow_engine": "partial"}))["status"] == "warn"
        assert (await self._health("live", ok, {"orderflow_engine": "stale"}))["status"] == "degraded"
        assert (await self._health("live", ok, {"market_data": "error"}))["status"] == "degraded"
        assert (await self._health("live", ok, failed=["alerts"]))["status"] == "degraded"
        # 'connecting' is neutral: a sporadic feed may sit there on a quiet market.
        assert (await self._health("live", ok, {"liquidation_feed": "connecting"}))["status"] == "ok"

    @pytest.mark.asyncio
    async def test_docs_url_is_the_real_repo(self):
        """M12: the advertised docs URL 404'd."""
        body = await self._health("demo", None)
        assert body["docs"] == "https://github.com/Co-Messi/HyperData-Terminal"


# ── H5: integrity_check result was discarded ─────────────────────

class TestH5QuickCheck:
    @staticmethod
    def _page_corrupted_db(path):
        """A DataStore-created file (real schema, so table/index creation is
        a no-op on reopen) whose HEADER is intact but whose `liquidations`
        root page is garbage — it opens fine; only quick_check notices."""
        DataStore(path).close()
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=DELETE")      # everything in the main file
        conn.executemany(
            "INSERT INTO liquidations (timestamp, exchange, symbol, side, size_usd, price, "
            "quantity, confirmed, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [(1.0, "x" * 400, "BTC", "long", 1.0, 1.0, 1.0, 1, 1.0) for _ in range(200)],
        )
        conn.commit()
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='liquidations'").fetchone()[0]
        conn.close()
        raw = bytearray(path.read_bytes())
        page_size = int.from_bytes(raw[16:18], "big") or 4096
        start = (root - 1) * page_size
        assert root > 1 and len(raw) >= start + page_size
        raw[start:start + 256] = b"\xff" * 256          # smash the table page, leave page 1
        path.write_bytes(bytes(raw))
        # Sanity: the file still opens and its schema still reads; only the
        # integrity verdict is bad. That is what the pre-fix code missed.
        probe = sqlite3.connect(str(path))
        assert probe.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] > 0
        assert probe.execute("PRAGMA quick_check").fetchone()[0] != "ok"
        probe.close()

    def test_page_level_corruption_is_quarantined(self, tmp_path):
        """Pre-fix: `conn.execute("PRAGMA integrity_check")` never fetched the
        result, so this file opened 'successfully' and the app ran on it."""
        path = tmp_path / "hyperdata.db"
        self._page_corrupted_db(path)
        store = DataStore(path)
        try:
            quarantined = list((tmp_path / "corrupted").glob("hyperdata.db.*"))
            assert len(quarantined) == 1
            assert store.get_db_stats()["liquidations_stored"] == 0   # fresh DB
            # And the recreated DB passes its own check.
            DataStore._check_integrity(store._conn)
        finally:
            store.close()

    def test_check_integrity_raises_on_non_ok(self):
        conn = sqlite3.connect(":memory:")
        DataStore._check_integrity(conn)            # healthy -> no raise
        conn.close()

        class _Fake:
            def execute(self, sql):
                class _Cur:
                    @staticmethod
                    def fetchone():
                        return ("*** in database main ***\nPage 2: btree page corrupted",)
                return _Cur()

        with pytest.raises(sqlite3.DatabaseError, match="quick_check failed"):
            DataStore._check_integrity(_Fake())


# ── M2 / M3: paper trader persist-first and reverse semantics ────

def _paper_trader(price=100.0, balance=10_000.0, with_db=True, **kw):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from src.strategies.paper_trader import CREATE_TABLE_SQL, PaperTrader
    hub = MagicMock()
    hub.market.assets = {"BTC": SimpleNamespace(price=price)}
    trader = PaperTrader(hub, [], starting_balance=balance, **kw)
    if with_db:
        trader._db = sqlite3.connect(":memory:")
        trader._db.execute(CREATE_TABLE_SQL)
    return trader


class TestM2PersistFirst:
    def test_no_db_means_no_trade(self, caplog):
        """Pre-fix: `if self._db:` skipped the whole persistence block and
        apply_mutation() ran anyway — the one case the docstring's
        "a trade that cannot be logged is not executed" did not cover."""
        from src.strategies.base import Signal
        trader = _paper_trader(with_db=False)
        assert trader._db is None
        with caplog.at_level("ERROR"):
            trader._execute_trade("t", Signal("BTC", "BUY", size_usd=1_000.0))
        assert trader.positions == {}
        assert trader.trades == []
        assert trader.balance == 10_000.0
        assert "REFUSED" in caplog.text and "not open" in caplog.text

    def test_with_db_the_same_trade_executes_and_is_logged(self):
        """Control: passed pre-fix too. The refusal in the test above must
        not have made the normal path (DB open) refuse as well."""
        from src.strategies.base import Signal
        trader = _paper_trader()
        trader._execute_trade("t", Signal("BTC", "BUY", size_usd=1_000.0))
        assert trader.positions["BTC"]["size_usd"] == 1_000.0
        assert trader._db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 1


class TestM3ReverseSemantics:
    def test_default_close_only_flattens_and_warns(self, caplog):
        """Default behaviour is unchanged (test_close_realizes_pnl still holds)
        but is now explicit and logged instead of silent."""
        from src.strategies.base import Signal
        trader = _paper_trader(balance=1_000.0)
        trader._execute_trade("t", Signal("BTC", "BUY", size_usd=500.0))
        with caplog.at_level("WARNING"):
            trader._execute_trade("t", Signal("BTC", "SELL", size_usd=500.0))
        assert "BTC" not in trader.positions
        assert "closing only" in caplog.text
        assert len(trader.trades) == 1 + 1

    def test_reverse_flag_opens_opposite_side_as_second_logged_trade(self):
        """Pre-fix there was no way to get the strategy's directional intent
        honoured: a strong SELL left the book flat until the next tick."""
        from src.strategies.base import Signal
        trader = _paper_trader(balance=1_000.0, reverse_on_opposite_signal=True)
        trader._execute_trade("t", Signal("BTC", "BUY", size_usd=500.0))
        trader.hub.market.assets["BTC"].price = 110.0
        trader._execute_trade("t", Signal("BTC", "SELL", size_usd=500.0))
        pos = trader.positions["BTC"]
        assert pos["side"] == "short"
        assert pos["size_usd"] == 500.0
        assert pos["entry_price"] == 110.0
        # +50 realised on the close, then 500 posted for the short.
        assert trader.balance == pytest.approx(1_000.0 + 50.0 - 500.0)
        assert [t["action"] for t in trader.trades] == ["BUY", "SELL", "SELL"]
        assert trader._db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 3

    def test_reverse_is_balance_checked(self):
        """If the reverse leg cannot be afforded the book is simply flat —
        never negative."""
        from src.strategies.base import Signal
        trader = _paper_trader(balance=500.0, reverse_on_opposite_signal=True)
        trader._execute_trade("t", Signal("BTC", "BUY", size_usd=500.0))
        trader.hub.market.assets["BTC"].price = 10.0     # -90%: close credits 50
        trader._execute_trade("t", Signal("BTC", "SELL", size_usd=500.0))
        assert "BTC" not in trader.positions
        assert trader.balance == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_start_logs_close_only_semantics(self, tmp_path, caplog):
        trader = _paper_trader(with_db=False)
        trader.db_path = tmp_path / "pt.db"
        with caplog.at_level("WARNING"):
            await trader.start()
        await trader.stop()
        assert "close-only semantics" in caplog.text


# ── M4: hub.stop() must log, and keep stopping, when a component raises ──

class TestM4StopLogs:
    @pytest.mark.asyncio
    async def test_failing_stop_is_logged_and_others_still_stop(self, tmp_path, monkeypatch, caplog):
        """Pre-fix: nine consecutive `except Exception: pass` blocks — a
        component leaking a socket at shutdown produced no evidence."""
        from unittest.mock import AsyncMock

        hub = _isolated_hub(tmp_path, monkeypatch)
        hub.liquidations.stop = AsyncMock(side_effect=RuntimeError("socket still open"))
        for comp in (hub.orderflow, hub.smart_money, hub.hlp, hub.funding,
                     hub.lsr, hub.orderbook, hub.spot, hub.deribit, hub.alerts):
            comp.stop = AsyncMock()
        with caplog.at_level("ERROR"):
            await hub.stop()
        assert "Error stopping liquidation_feed" in caplog.text
        assert "socket still open" in caplog.text
        for comp in (hub.orderflow, hub.smart_money, hub.hlp, hub.funding,
                     hub.lsr, hub.orderbook, hub.spot, hub.deribit):
            assert comp.stop.await_count == 1
        assert hub.status.mode == "offline"


# ── M5 / M6: rate limiter LRU, per-IP WebSocket cap ──────────────

class TestM5RateLimiterLRU:
    def test_tracked_keys_are_bounded_by_lru_eviction(self):
        """Pre-fix: above 10k ACTIVE keys nothing was ever removed and a
        full-dict comprehension ran on every request."""
        from src.api_server import _RateLimiter
        limiter = _RateLimiter(max_requests=100, window_s=60, max_tracked_keys=100)
        for i in range(150):
            assert limiter.allow(f"ip-{i}", now=1000.0)      # all active, none expired
        assert len(limiter._hits) == 100
        assert limiter.evictions == 50
        assert "ip-0" not in limiter._hits and "ip-149" in limiter._hits

    def test_hot_key_survives_eviction(self):
        from src.api_server import _RateLimiter
        limiter = _RateLimiter(max_requests=1000, window_s=60, max_tracked_keys=50)
        for i in range(200):
            limiter.allow("hot", now=1000.0)
            limiter.allow(f"cold-{i}", now=1000.0)
        assert "hot" in limiter._hits
        assert len(limiter._hits) == 50


class TestM6PerIPWebSocketCap:
    @pytest.mark.asyncio
    async def test_one_address_cannot_fill_the_global_budget(self):
        """Pre-fix: MAX_WS_CONNECTIONS was global only — one client opening
        10 sockets locked everyone else out."""
        from unittest.mock import MagicMock

        from src.api_server import MAX_WS_CONNECTIONS, MAX_WS_CONNECTIONS_PER_IP, HyperDataAPI

        def fake_client(remote):
            c = MagicMock()
            c.remote = remote
            c.ws.closed = False
            return c

        api = HyperDataAPI(hub=MagicMock())
        api._cors_origins = set()
        api._ws_clients = [fake_client("10.0.0.1") for _ in range(MAX_WS_CONNECTIONS_PER_IP)]

        req = MagicMock()
        req.headers = {}
        req.remote = "10.0.0.1"
        resp = await api.handle_ws(req)
        assert resp.status == 429
        assert b"from this address" in resp.body

        # Another address is judged against the GLOBAL cap only.
        others = MAX_WS_CONNECTIONS - MAX_WS_CONNECTIONS_PER_IP
        api._ws_clients += [fake_client(f"10.0.0.{i}") for i in range(2, 2 + others)]
        assert len(api._ws_clients) == MAX_WS_CONNECTIONS
        req.remote = "10.0.9.9"
        resp = await api.handle_ws(req)
        assert resp.status == 429
        assert b"from this address" not in resp.body


# ── M9: exchange/LLM strings must never be parsed as Rich markup ─

# NOTE: a lone "[bold" is NOT an error for Rich (no closing bracket -> literal
# text). An unmatched CLOSING tag is what raises MarkupError when parsed.
UNBALANCED = "[/bold]"               # raises MarkupError when parsed
STYLED = "[bold red]PUMP[/]"         # silently restyles when parsed


def _render(renderable) -> str:
    from rich.console import Console
    console = Console(record=True, width=220, force_terminal=False)
    console.print(renderable)
    return console.export_text()


def _position(symbol: str):
    from src.data_layer.position_scanner import TrackedPosition
    return TrackedPosition(address="0x" + "a" * 40, symbol=symbol, side="long", size_usd=250_000.0,
                           entry_price=100.0, current_price=100.0, liq_price=99.0, distance_pct=1.0,
                           leverage=10.0, unrealized_pnl=5.0, margin_used=25_000.0)


def _asset(symbol: str):
    from src.data_layer.market_data import AssetInfo
    return AssetInfo(symbol=symbol, price=1.0, funding_rate=0.001, open_interest=1e6, volume_24h=1e6,
                     price_change_24h_pct=0.01, mark_price=1.0, index_price=1.0)


class TestM9MarkupSafety:
    """Every render site fed by exchange/LLM strings, with a symbol that
    would raise MarkupError (`[bold`) and one that would restyle
    (`[bold red]PUMP[/]`). Pre-fix each of these raised inside the Live loop."""

    @pytest.mark.parametrize("symbol", [UNBALANCED, STYLED])
    def test_hub_panels(self, symbol, monkeypatch):
        from unittest.mock import MagicMock

        from src.dashboards.hub_panels import HubHLP, HubLiqWatch, HubMarket, HubWhales
        from src.data_layer.hlp_tracker import HLPPosition
        hub = MagicMock()
        hub.positions = _scanner(monkeypatch)
        hub.status.mode = "live"
        hub.status.tracked_positions = 1
        hub.get_btc_price.return_value = 1.0
        hub.get_all_positions_sorted.return_value = [_position(symbol)]
        hub.get_whale_positions.return_value = [_position(symbol)]
        hub.get_all_assets.return_value = [_asset(symbol)]
        hub.market.assets = {symbol: _asset(symbol)}
        hub.get_extreme_funding.return_value = []
        hub.hlp.get_stats.return_value = {
            "account_value": 1.0, "session_pnl": 0.0, "num_positions": 1, "net_delta": 0.0,
            "delta_zscore": 0.0, "total_exposure": 1.0, "total_snapshots": 1, "total_trades": 0,
            "liquidation_absorptions": 0,
        }
        hub.hlp.get_latest_snapshot.return_value = object()
        hub.hlp.get_delta_history.return_value = []
        hub.hlp.get_liquidation_absorptions.return_value = []
        hub.hlp.get_top_positions.return_value = [HLPPosition(
            symbol=symbol, side="long", size=1.0, size_usd=1.0, entry_price=1.0,
            current_price=1.0, unrealized_pnl=0.0, leverage=1.0)]
        for panel in (HubLiqWatch(hub), HubWhales(hub), HubMarket(hub), HubHLP(hub)):
            text = _render(panel.build_compact())
            assert symbol[:5] in text, type(panel).__name__     # shown literally, not parsed

    @pytest.mark.parametrize("symbol", [UNBALANCED, STYLED])
    def test_standalone_dashboards(self, symbol):
        import time as _t

        from src.dashboards.liquidation_stream import LiquidationStreamDashboard
        from src.dashboards.market_overview import MarketOverviewDashboard
        from src.dashboards.whale_tracker import WhaleTrackerDashboard
        from src.data_layer.liquidation_feed import LiquidationEvent, LiquidationFeed

        feed = LiquidationFeed()
        feed.events.append(LiquidationEvent(_t.time(), "binance", symbol, "long", 1000.0, 1.0, 1.0))
        assert symbol[:5] in _render(LiquidationStreamDashboard(feed=feed).build_recent_feed())

        mo = MarketOverviewDashboard()
        mo.assets = [_asset(symbol)]
        for table in (mo.build_assets_table(mo.assets), mo.build_extreme_funding(mo.assets), mo.build_compact()):
            assert symbol[:5] in _render(table)

        wt = WhaleTrackerDashboard()
        wt.positions = [_position(symbol)]
        for table in (wt.build_whale_table(wt.positions), wt.build_symbol_breakdown(), wt.build_compact()):
            assert symbol[:5] in _render(table)

    def test_paper_trader_console_line(self, monkeypatch):
        """`signal.reason` comes verbatim from the LLM; a MarkupError here
        fired AFTER apply_mutation() had already changed the books."""
        import io
        from types import SimpleNamespace

        from rich.console import Console

        from src.strategies import paper_trader as pt
        from src.strategies.base import Signal
        recorder = Console(record=True, width=220, file=io.StringIO(), force_terminal=False)
        monkeypatch.setattr(pt, "console", recorder)
        trader = _paper_trader()
        trader.hub.market.assets = {UNBALANCED: SimpleNamespace(price=100.0)}
        trader._execute_trade("[bold]strat", Signal(UNBALANCED, "BUY", size_usd=10.0,
                                                    reason=f"{STYLED} because [oops"))
        assert UNBALANCED in trader.positions
        out = recorder.export_text()
        assert "[bold red]PUMP[/] because [oops" in out
        assert "[bold]strat" in out


# ── M8: scoring math ─────────────────────────────────────────────

class TestM8Scoring:
    @staticmethod
    def _engine():
        from data_layer.smart_money import SmartMoneyEngine
        return SmartMoneyEngine()

    def test_pnl_score_is_monotonic_bounded_and_uses_its_weight(self):
        """Pre-fix: log10(1+pnl)/10 mapped $1k..$1M into 0.30..0.60, so BETA's
        0.40 weight had ~0.12 of real discriminating range."""
        e = self._engine()
        pnls = [-1e8, -1e6, -1e4, -1e3, 0.0, 1e3, 1e4, 1e5, 1e6, 1e8]
        scores = [e._compute_pnl_score(p) for p in pnls]
        assert scores == sorted(scores)
        assert all(-1.0 <= s <= 1.0 for s in scores)
        assert e._compute_pnl_score(1e6) == pytest.approx(1.0)
        assert e._compute_pnl_score(-1e6) == pytest.approx(-1.0)
        assert e._compute_pnl_score(1e3) == pytest.approx(0.5, abs=0.01)
        # $1k -> $1M now spans ~0.5 of score, not ~0.3.
        assert e._compute_pnl_score(1e6) - e._compute_pnl_score(1e3) > 0.45

    def test_risk_ratio_is_return_based_and_small_sample_shrunk(self):
        """Pre-fix: mean/std of DOLLAR PnL with no shrinkage — ten similar $50
        scalps produced a huge ratio that clamped to the +1.0 maximum."""
        e = self._engine()
        assert e._compute_risk_adjusted([0.001] * 10) == 0.0            # zero dispersion says nothing
        assert e._compute_risk_adjusted([0.5]) == 0.0                   # n < 2
        # Same distribution, more samples -> less shrinkage -> larger ratio.
        pattern = [0.010, 0.012, 0.009, 0.011]
        small = e._compute_risk_adjusted(pattern)
        large = e._compute_risk_adjusted(pattern * 50)
        assert 0 < small < large
        assert small == pytest.approx(large * (4 / 24) / (200 / 220), rel=1e-6)
        # Sign follows the mean.
        assert e._compute_risk_adjusted([-0.01, -0.012, -0.009]) < 0

    def test_composite_is_bounded(self):
        """Guard: passed pre-fix too. Pins the composite's [-0.65, 1] range
        and that the documented weights still sum to 1 after the M8
        re-weighting; it does not by itself show M8 was a bug."""
        from data_layer.smart_money import WalletProfile
        e = self._engine()
        best = WalletProfile(address="0x" + "1" * 40, discovered_at=0, last_seen=0, last_analyzed=0,
                             win_rate=1.0, total_realized_pnl=1e12, sharpe_ratio=1e6)
        worst = WalletProfile(address="0x" + "2" * 40, discovered_at=0, last_seen=0, last_analyzed=0,
                              win_rate=0.0, total_realized_pnl=-1e12, sharpe_ratio=-1e6)
        assert e._compute_composite(best) == pytest.approx(1.0)
        assert e._compute_composite(worst) == pytest.approx(-0.65)
        assert e.ALPHA + e.BETA + e.GAMMA == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_analyze_wallet_feeds_returns_not_dollars(self, monkeypatch):
        e = self._engine()
        fills = []
        for i, (pnl, px, sz) in enumerate([(50, 50_000, 1.0), (60, 50_000, 1.0), (40, 50_000, 1.0),
                                           (55, 50_000, 1.0), (45, 50_000, 1.0)]):
            fills.append({"time": 1_700_000_000_000 + i, "dir": "Open Long", "coin": "BTC",
                          "px": str(px), "sz": str(sz), "closedPnl": "0"})
            fills.append({"time": 1_700_000_000_000 + i, "dir": "Close Long", "coin": "BTC",
                          "px": str(px), "sz": str(sz), "closedPnl": str(pnl)})

        async def fake_fills(address):
            return fills

        async def none(address):
            return None

        async def no_signals(address, fills):
            return None

        monkeypatch.setattr(e, "_fetch_fills", fake_fills)
        monkeypatch.setattr(e, "_fetch_clearinghouse", none)
        monkeypatch.setattr(e, "check_signals", no_signals)
        w = await e.analyze_wallet("0x" + "3" * 40)
        expected = e._compute_risk_adjusted([50 / 50_000, 60 / 50_000, 40 / 50_000, 55 / 50_000, 45 / 50_000])
        assert w.sharpe_ratio == pytest.approx(expected)
        assert w.total_trades == 5


# ── H3: LLM transport is cancellable; refunds only for unreachable provider ──

class TestH3LLMTransport:
    @staticmethod
    def _agent():
        from src.strategies.llm_agent import LLMAgent
        agent = LLMAgent(symbol="BTC")
        agent.api_key = "k"
        agent.base_url = "https://llm.example/v1"
        return agent

    def test_no_worker_thread_and_no_dead_sync_paths(self):
        """Pre-fix: a single-worker ThreadPoolExecutor whose thread a wait_for
        timeout could not cancel — one trickling response wedged every later
        evaluation forever. _async_evaluate existed but was dead code."""
        from src.strategies.llm_agent import LLMAgent
        agent = self._agent()
        assert not hasattr(agent, "_pool")
        assert not hasattr(LLMAgent, "_sync_evaluate")
        assert not hasattr(LLMAgent, "_sync_call")

    @pytest.mark.asyncio
    async def test_timeout_cancels_request_and_keeps_budget_slot(self, monkeypatch, caplog):
        """Pre-fix: the timeout branch REFUNDED the slot, so a slow-but-billing
        provider got ~2x the nominal hourly cap."""
        import asyncio

        agent = self._agent()
        agent.EVAL_TIMEOUT_S = 0.05
        cancelled = {"value": False}

        async def slow(hub):
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled["value"] = True
                raise

        monkeypatch.setattr(agent, "_async_evaluate", slow)
        with caplog.at_level("WARNING"):
            assert await agent.evaluate(object()) is None
        assert cancelled["value"] is True            # the request itself was cancelled
        assert len(agent._eval_times) == 1           # slot NOT refunded
        assert "budget slot kept" in caplog.text
        assert agent._inflight is False

    @pytest.mark.asyncio
    async def test_unreachable_provider_refunds_slot(self, monkeypatch):
        from unittest.mock import MagicMock

        import aiohttp

        agent = self._agent()

        async def refused(hub):
            raise aiohttp.ClientConnectorError(MagicMock(), OSError("refused"))

        monkeypatch.setattr(agent, "_async_evaluate", refused)
        assert await agent.evaluate(object()) is None
        assert len(agent._eval_times) == 0

    @pytest.mark.asyncio
    async def test_inflight_guard_skips_instead_of_queueing(self, monkeypatch):
        import asyncio

        agent = self._agent()
        started = asyncio.Event()

        async def slow(hub):
            started.set()
            await asyncio.sleep(0.2)
            return None

        monkeypatch.setattr(agent, "_async_evaluate", slow)
        first = asyncio.create_task(agent.evaluate(object()))
        await started.wait()
        # A second tick while the first is in flight is skipped immediately
        # and consumes NO budget slot.
        assert await asyncio.wait_for(agent.evaluate(object()), timeout=0.05) is None
        assert len(agent._eval_times) == 1
        await first
        assert agent._inflight is False

    def test_reason_is_bounded(self):
        from src.strategies.llm_agent import LLMAgent
        agent = self._agent()
        sig = agent._parse_response("BUY\n" + "x" * 5000)
        assert sig is not None
        assert len(sig.reason) <= len("[LLM] ") + LLMAgent.MAX_REASON_CHARS


# ── H4: bounded scan cycle, per-address cache, scan-age everywhere ──

class TestH4ScanBudget:
    @staticmethod
    def _quiet(monkeypatch, s):
        """No network: prices/meta/discovery are no-ops, batch sleeps are instant."""
        import asyncio
        import time as _t
        from unittest.mock import AsyncMock

        from src.data_layer import position_scanner as ps
        monkeypatch.setattr(s, "update_prices", AsyncMock())
        monkeypatch.setattr(s, "update_meta", AsyncMock())
        s._last_discovery = _t.time()
        monkeypatch.setattr(ps.asyncio, "sleep", AsyncMock())
        assert asyncio.sleep is ps.asyncio.sleep

    @pytest.mark.asyncio
    async def test_cycle_is_bounded_and_round_robins(self, monkeypatch):
        """Pre-fix: scan() walked every tracked address every cycle — 50k
        addresses at 10 req/s was an 83-minute cycle behind a 15s interval."""
        s = _scanner(monkeypatch, 400)
        self._quiet(monkeypatch, s)
        seen: list[str] = []

        async def fake_get(addr):
            seen.append(addr)
            return []

        monkeypatch.setattr(s, "get_positions_for_address", fake_get)
        await s.scan()
        assert len(seen) == s.scan_budget == 150 and len(set(seen)) == 150
        assert s.last_scan_at > 0 and s.last_full_pass_at == 0.0
        await s.scan()
        assert len(set(seen)) == 300
        await s.scan()                                   # wraps: 300..399 then 0..49
        assert len(set(seen)) == 400
        assert s.last_full_pass_at > 0

    @pytest.mark.asyncio
    async def test_cached_positions_get_fresh_distance_but_keep_scan_stamp(self, monkeypatch):
        s = _scanner(monkeypatch, 2)
        self._quiet(monkeypatch, s)
        s.scan_budget = 1
        a, b = sorted(s.discovered_addresses)
        pos = _position("BTC")
        pos.liq_price = 90.0
        prices = {"BTC": 100.0}

        async def fake_prices():
            s.market_prices = dict(prices)

        async def fake_get(addr):
            return [pos] if addr == a else []

        monkeypatch.setattr(s, "update_prices", fake_prices)
        monkeypatch.setattr(s, "get_positions_for_address", fake_get)

        await s.scan()                                   # cycle 1 scans `a`
        assert s.positions == [pos]
        stamp = pos.scanned_at
        assert stamp > 0
        assert pos.distance_pct == pytest.approx(10.0)

        prices["BTC"] = 95.0
        await s.scan()                                   # cycle 2 scans `b`; `a` served from cache
        assert s.positions == [pos]
        assert pos.current_price == 95.0
        assert pos.distance_pct == pytest.approx(abs(95.0 - 90.0) / 95.0 * 100)
        assert pos.scanned_at == stamp                   # honest: NOT re-fetched
        assert s.as_of(s.positions) == stamp

    @pytest.mark.asyncio
    async def test_failed_request_keeps_previous_cache_entry(self, monkeypatch):
        s = _scanner(monkeypatch, 1)
        self._quiet(monkeypatch, s)
        (a,) = s.discovered_addresses
        pos = _position("BTC")
        calls = {"n": 0}

        async def flaky(addr):
            calls["n"] += 1
            if calls["n"] == 1:
                return [pos]
            raise RuntimeError("HTTP 429")

        monkeypatch.setattr(s, "get_positions_for_address", flaky)
        await s.scan()
        await s.scan()
        assert s.positions == [pos]                      # not silently "no positions"

    def test_staleness_semantics(self, monkeypatch):
        from src.data_layer.position_scanner import POSITION_STALE_AFTER_SECONDS as STALE
        s = _scanner(monkeypatch)
        assert s.is_stale() is False                     # never scanned = starting, not stale
        now = 10_000.0
        p = _position("BTC")
        s.positions = [p]
        s.last_scan_at = now - 1
        p.scanned_at = now - 1
        assert s.is_stale(now) is False
        p.scanned_at = now - STALE - 100                 # a displayed position fell behind
        assert s.is_stale(now) is True
        p.scanned_at = now - 1
        s.last_scan_at = now - STALE - 100               # cycles stopped completing
        assert s.is_stale(now) is True
        f = s.freshness(now)
        assert f["stale"] is True and f["scan_age_seconds"] == STALE + 100

    @pytest.mark.asyncio
    async def test_api_exposes_scan_age_and_as_of(self, monkeypatch):
        from unittest.mock import MagicMock

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from src.api_server import HyperDataAPI
        from src.data_layer.hub import HubStatus
        from src.data_layer.orderflow_engine import OrderFlowEngine
        s = _scanner(monkeypatch)
        p = _position("BTC")
        p.scanned_at = 123.0
        s.positions = [p]
        s.last_scan_at = 130.0
        hub = MagicMock()
        hub.status = HubStatus(mode="demo")
        hub.orderflow = OrderFlowEngine(symbols=["BTC"])
        hub.positions = s
        hub.get_whale_positions.return_value = [p]
        hub.health.latest.return_value = None
        api = HyperDataAPI(hub=hub)
        app = web.Application()
        app.router.add_get("/v1/health", api.handle_health)
        app.router.add_get("/v1/whales", api.handle_whales)
        app.router.add_get("/v1/positions/danger-zone", api.handle_danger_zone)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            health = await (await client.get("/v1/health")).json()
            assert health["position_scan"]["scan_age_seconds"] > 0
            assert health["position_scan"]["stale"] is True
            whales = await (await client.get("/v1/whales")).json()
            assert whales["as_of"] == 123.0
            assert whales["positions"][0]["scanned_at"] == 123.0
            dz = await (await client.get("/v1/positions/danger-zone?threshold=50")).json()
            assert dz["as_of"] == 123.0
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_hub_flips_scanner_to_stale(self, tmp_path, monkeypatch):
        """Pre-fix: _update_feed_staleness never looked at the scanner, so
        'connected' stayed on a scanner whose last result was an hour old."""
        import time as _t

        from src.data_layer.position_scanner import POSITION_STALE_AFTER_SECONDS as STALE

        hub = _isolated_hub(tmp_path, monkeypatch)
        try:
            hub.status.position_scanner = "connected"
            hub.positions.last_scan_at = _t.time() - STALE - 100
            await hub._update_feed_staleness()
            assert hub.status.position_scanner == "stale"
            hub.positions.last_scan_at = _t.time()
            await hub._update_feed_staleness()
            assert hub.status.position_scanner == "connected"
        finally:
            hub.store.close()

    def test_health_monitor_checks_scanner(self, monkeypatch):
        import time as _t
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from src.data_layer.health_monitor import DataHealthMonitor
        from src.data_layer.orderflow_engine import OrderFlowEngine
        from src.data_layer.position_scanner import POSITION_STALE_AFTER_SECONDS as STALE
        now = _t.time()
        s = _scanner(monkeypatch)
        hub = SimpleNamespace(
            orderflow=OrderFlowEngine(symbols=["BTC"]), positions=s,
            orderbook=MagicMock(is_stale=lambda: False, data_age=lambda: 1.0),
            status=SimpleNamespace(last_market_refresh=now),
            deribit=MagicMock(get_latest=lambda x: None),
        )
        checks = {c.name: c for c in DataHealthMonitor(hub)._check_freshness()}
        assert checks["position_scanner"].status == "warn"      # no scan yet
        s.last_scan_at = now - STALE - 100
        checks = {c.name: c for c in DataHealthMonitor(hub)._check_freshness()}
        assert checks["position_scanner"].status == "fail"
        s.last_scan_at = now - 5
        checks = {c.name: c for c in DataHealthMonitor(hub)._check_freshness()}
        assert checks["position_scanner"].status == "pass"

    def test_retention_cap_is_scan_rate_derived(self):
        """The store cap must be coverable well inside the stale threshold
        (the old 50,000 cap implied an 83-minute cycle). The exact
        relationship is pinned by TestB1StalenessBudget."""
        from src.data_layer.position_scanner import (
            POSITION_STALE_AFTER_SECONDS,
            full_pass_seconds_worst_case,
        )
        assert full_pass_seconds_worst_case(address_store.MAX_TRACKED_ADDRESSES) < POSITION_STALE_AFTER_SECONDS


# ── B1: the staleness threshold must hold for the tracked set it bounds ──

class TestB1StalenessBudget:
    """Pre-fix: POSITION_STALE_AFTER_SECONDS was a hardcoded 600s while a
    full pass over the 3,000-address cap took >=580s at ZERO latency (150
    addresses/cycle, 14s of batch sleeps + the 15s scan_interval), so any
    real install tripped 'stale' — and /v1/health 'degraded' — forever.
    Worse, the scanner's in-memory set was loaded once and only ever grew;
    address_store.prune() trimmed the table but never the set, so the pass
    time was not even bounded by the cap."""

    def test_worst_case_full_pass_is_comfortably_under_the_stale_threshold(self):
        from src.data_layer import position_scanner as ps
        # The relationship the threshold is derived from...
        assert ps.full_pass_seconds_worst_case() * ps.STALE_MARGIN_FACTOR <= ps.POSITION_STALE_AFTER_SECONDS
        assert ps.STALE_MARGIN_FACTOR >= 1.5
        # ...and the reviewer's independent zero-latency arithmetic for the
        # store cap alone (20 cycles x (14s sleeps + 15s interval) = 580s),
        # which the old 600s threshold cleared by 20 seconds.
        cycles = -(-address_store.MAX_TRACKED_ADDRESSES // ps.SCAN_ADDRESS_BUDGET)
        batches = -(-ps.SCAN_ADDRESS_BUDGET // ps.RATE_LIMIT_PER_SEC)
        zero_latency_pass = cycles * ((batches - 1) * ps.BATCH_SLEEP_SECONDS + ps.SCAN_INTERVAL_SECONDS)
        assert zero_latency_pass * 1.5 <= ps.POSITION_STALE_AFTER_SECONDS
        # The in-memory bound covers the store cap plus what discovery can
        # add between prunes; the derivation uses THAT, not the store cap.
        assert ps.MAX_TRACKED_ADDRESSES_IN_MEMORY >= address_store.MAX_TRACKED_ADDRESSES + ps.DISCOVERY_LIMIT
        assert ps.full_pass_seconds_worst_case() > ps.full_pass_seconds_worst_case(address_store.MAX_TRACKED_ADDRESSES)

    def test_hub_default_interval_and_prune_cadence_match_the_derivation(self):
        import inspect

        from src.data_layer import hub as hub_mod
        from src.data_layer import position_scanner as ps
        sig = inspect.signature(hub_mod.HyperDataHub.__init__)
        assert sig.parameters["scan_interval"].default == ps.SCAN_INTERVAL_SECONDS
        assert hub_mod.DB_PRUNE_INTERVAL_TICKS == ps.ADDRESS_PRUNE_INTERVAL_SECONDS
        assert "% DB_PRUNE_INTERVAL_TICKS == 0" in inspect.getsource(hub_mod.HyperDataHub._status_update_tick)

    @pytest.mark.asyncio
    async def test_resync_drops_pruned_addresses_and_keeps_new_discoveries(self, isolated_address_store, monkeypatch):
        from src.data_layer.position_scanner import PositionScanner
        addrs = [_addr(f"{i}{i}{i}") for i in range(5)]
        for a in addrs:
            address_store.add_addresses([a], source="test")
        s = PositionScanner()
        assert s.discovered_addresses == set(addrs)
        s._position_cache = {a: [] for a in addrs}

        monkeypatch.setattr(address_store, "MAX_TRACKED_ADDRESSES", 3)
        assert address_store.prune() == 2
        # Pre-fix there was no way to notice: the set stayed at 5 forever.
        assert len(s.discovered_addresses) == 5

        # Something discovered while the (off-loop) read is in flight must
        # survive the re-sync — it is in the store too.
        late = _addr("fff")
        real_read = address_store.get_all_addresses

        def read_then_discover():
            got = real_read()
            s.discovered_addresses.add(late)
            return got

        monkeypatch.setattr(address_store, "get_all_addresses", read_then_discover)
        dropped = await s.resync_addresses()
        assert dropped == 2
        assert s.discovered_addresses == set(addrs[2:]) | {late}
        assert set(s._position_cache) == set(addrs[2:])
        assert len(s.discovered_addresses) <= address_store.MAX_TRACKED_ADDRESSES + 1

    @pytest.mark.asyncio
    async def test_hub_resyncs_the_scanner_after_the_hourly_prune(self, tmp_path, monkeypatch):
        from src.data_layer import hub as hub_mod
        addrs = [_addr(f"{i}{i}{i}") for i in range(5)]
        hub = _isolated_hub(tmp_path, monkeypatch)
        try:
            for a in addrs:
                address_store.add_addresses([a], source="test")
            hub.positions.discovered_addresses = set(addrs)
            monkeypatch.setattr(address_store, "MAX_TRACKED_ADDRESSES", 3)
            await hub._status_update_tick(hub_mod.DB_PRUNE_INTERVAL_TICKS - 1)   # the prune tick
            assert len(address_store.get_all_addresses()) == 3
            assert hub.positions.discovered_addresses == set(addrs[2:])
        finally:
            hub.store.close()


# ── H6: SQLite writes off the event loop ─────────────────────────

class _Liq:
    def __init__(self, i: int = 0):
        import time as _t
        self.timestamp = _t.time()
        self.exchange = "binance"
        self.symbol = "BTC"
        self.side = "long"
        self.size_usd = float(i)
        self.price = 1.0
        self.quantity = 1.0
        self.confirmed = True


class TestH6WriterThread:
    def test_insert_callbacks_never_touch_sqlite_on_the_caller_thread(self, tmp_path):
        """Pre-fix: _save_trade/_save_liquidation ran a blocking INSERT under
        a threading.Lock inside the WebSocket read path on the event loop."""
        import threading
        from types import SimpleNamespace

        store = DataStore(tmp_path / "w.db")
        try:
            real_conn = store._conn
            idents: list[int] = []

            class _Spy:
                def execute(self, *a, **kw):
                    idents.append(threading.get_ident())
                    return real_conn.execute(*a, **kw)

                def __getattr__(self, name):
                    return getattr(real_conn, name)

            store._conn = _Spy()
            for i in range(20):
                store._save_liquidation(_Liq(i))
                store._save_trade(SimpleNamespace(timestamp=1.0, symbol="BTC", side="buy",
                                                  price=1.0, size=1.0, size_usd=1.0))
            store.save_funding_rate(SimpleNamespace(timestamp=1.0, exchange="binance", symbol="BTC",
                                                    funding_rate_hourly=0.0, funding_rate_annualized=0.0))
            store.flush()
            assert idents, "nothing was written"
            assert threading.get_ident() not in idents          # all INSERTs ran on the writer
            assert store._writer is not None and store._writer.is_alive()
            assert store._writer.name == "datastore-writer"
        finally:
            store.close()

    def test_reads_drain_the_queue_first(self, tmp_path):
        store = DataStore(tmp_path / "r.db")
        try:
            for i in range(7):
                store._save_liquidation(_Liq(i))
            # No explicit flush: the read must still see everything queued.
            assert store.get_liquidation_stats(hours=1)["total_count"] == 7
            assert store.get_db_stats()["liquidations_stored"] == 7
            assert store.get_db_stats()["write_queue_pending"] == 0
        finally:
            store.close()

    def test_queue_is_bounded_and_drops_are_counted(self, tmp_path, monkeypatch, caplog):
        from src.data_layer import persistence
        monkeypatch.setattr(persistence, "WRITE_QUEUE_MAX", 5)
        store = DataStore(tmp_path / "q.db")
        try:
            # Park the writer: it pops the first item, then blocks on the
            # connection lock held here; everything else piles up in the queue.
            with store._lock:
                store._save_liquidation(_Liq(0))
                import time as _t
                deadline = _t.time() + 2
                while store._applied_seq == 0 and store._write_q and _t.time() < deadline:
                    _t.sleep(0.005)   # let the writer take the first batch
                with caplog.at_level("WARNING"):
                    for i in range(1, 10):
                        store._save_liquidation(_Liq(i))
            store.flush()
            assert store.dropped_writes == 4                       # 1 in flight + 5 queued + 4 dropped
            assert store.get_db_stats()["dropped_writes"] == 4
            assert store.get_db_stats()["liquidations_stored"] == 6
            assert "write queue full" in caplog.text
        finally:
            store.close()

    def test_close_stops_writer_and_persists_everything(self, tmp_path):
        path = tmp_path / "c.db"
        store = DataStore(path)
        for i in range(30):
            store._save_liquidation(_Liq(i))
        writer = store._writer
        store.close()
        assert not writer.is_alive()
        ro = sqlite3.connect(str(path))
        assert ro.execute("SELECT COUNT(*) FROM liquidations").fetchone()[0] == 30
        ro.close()

    def test_hub_runs_blocking_db_work_off_the_loop(self):
        import inspect

        from src.data_layer.hub import HyperDataHub
        src = inspect.getsource(HyperDataHub._status_update_tick)
        assert "asyncio.to_thread(self.store.get_db_stats)" in src
        assert "asyncio.to_thread(self.store.prune)" in src
        assert "asyncio.to_thread(address_store.prune)" in src

    def test_each_batch_is_committed_so_the_write_lock_is_released(self, tmp_path):
        """Found in the live run: holding the transaction open between
        commits (every 50 events / 5s) starved address_store's second
        connection — 'database is locked' despite a 10s busy timeout. After a
        batch is applied the connection must not be mid-transaction."""
        import time as _t

        store = DataStore(tmp_path / "commit.db")
        try:
            # Steady state under load: a commit happened moments ago and the
            # event count is not on a 50 boundary — exactly when the old
            # "every 50 events or 5s" rule kept the transaction open.
            store._last_commit_at = _t.time()
            for i in range(3):
                store._save_liquidation(_Liq(i))
            assert store._drain()
            with store._lock:
                assert store._conn.in_transaction is False
            # A second connection can write immediately, without waiting.
            other = sqlite3.connect(str(tmp_path / "commit.db"), timeout=0.2)
            other.execute("INSERT INTO discovered_addresses (address, source, first_seen, last_seen) "
                          "VALUES ('0x' || substr(hex(randomblob(20)), 1, 40), 't', 1, 1)")
            other.commit()
            other.close()
        finally:
            store.close()


class TestReconnectBackoff:
    @pytest.mark.asyncio
    async def test_short_lived_clean_close_backs_off(self, monkeypatch, caplog):
        """Found in the live run: 138 HL reconnects in 92s. A clean close
        reset the backoff to 1s and looped with NO sleep at all."""
        import asyncio

        from src.data_layer.orderflow_engine import OrderFlowEngine
        e = OrderFlowEngine(symbols=["BTC"])
        calls = {"n": 0}

        async def closes_immediately(shard=0):
            calls["n"] += 1

        monkeypatch.setattr(e, "_connect_and_listen", closes_immediately)
        e._running = True
        with caplog.at_level("INFO"):
            try:
                # _run_forever swallows the cancellation and returns, so on
                # 3.13 wait_for may return None rather than raise; either way
                # only the attempt count matters here.
                await asyncio.wait_for(e._run_forever(), timeout=0.3)
            except asyncio.TimeoutError:
                pass
        # 1s backoff -> exactly one connect attempt inside 0.3s (pre-fix: hundreds).
        assert calls["n"] == 1
        assert "short-lived" in caplog.text


class TestHLSharding:
    """Measured live: a `trades` subscription for a coin Hyperliquid does not
    list (PEPE/BONK/FLOKI in DEFAULT_SYMBOLS — HL calls them kPEPE/...) closes
    the socket with 1006. The single 50-symbol socket therefore died 0.6s
    after every connect and the zero-sleep reconnect loop hid it (107
    connects / 75s on the base commit). Fix: filter against the live meta
    universe; shard so a mid-session delisting takes down one shard, not the
    venue."""

    def test_unlisted_symbols_are_skipped_and_named_once(self, caplog):
        from src.data_layer.orderflow_engine import OrderFlowEngine
        e = OrderFlowEngine(symbols=["BTC", "PEPE", "ETH", "BONK"])
        universe = {"BTC", "ETH", "kPEPE", "SOL"}
        with caplog.at_level("WARNING"):
            assert e._hl_listed(["BTC", "PEPE", "ETH", "BONK"], universe) == ["BTC", "ETH"]
            assert e._hl_listed(["BTC", "PEPE", "ETH", "BONK"], universe) == ["BTC", "ETH"]
        assert caplog.text.count("PEPE is not listed") == 1          # once, not per reconnect
        assert "Hyperliquid lists it as kPEPE" in caplog.text
        assert caplog.text.count("BONK is not listed") == 1
        assert "lists it as kBONK" not in caplog.text                # no alias -> no hint
        # No universe (fetch failed): subscribe unfiltered, never go dark.
        assert e._hl_listed(["BTC", "PEPE"], None) == ["BTC", "PEPE"]

    @pytest.mark.asyncio
    async def test_universe_is_fetched_once_and_cached(self):
        from unittest.mock import AsyncMock, MagicMock

        from src.data_layer.orderflow_engine import OrderFlowEngine
        e = OrderFlowEngine(symbols=["BTC"])
        resp = AsyncMock()
        resp.json = AsyncMock(return_value={"universe": [{"name": "BTC"}, {"name": "kPEPE"}, "junk"]})
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.post = MagicMock(return_value=resp)
        assert await e._fetch_hl_universe(session) == {"BTC", "kPEPE"}
        assert await e._fetch_hl_universe(session) == {"BTC", "kPEPE"}
        assert session.post.call_count == 1                            # cached within TTL
        # A failing refresh keeps the last good universe instead of returning None.
        e._hl_universe_at = 0.0
        session.post = MagicMock(side_effect=RuntimeError("503"))
        assert await e._fetch_hl_universe(session) == {"BTC", "kPEPE"}

    def test_shards_cover_all_symbols_within_the_cap(self):
        from config.settings import DEFAULT_SYMBOLS
        from src.data_layer.orderflow_engine import HL_SUBSCRIPTIONS_PER_SOCKET, OrderFlowEngine
        e = OrderFlowEngine()                       # all 50 defaults
        shards = e._hl_shards()
        assert all(0 < len(s) <= HL_SUBSCRIPTIONS_PER_SOCKET for s in shards)
        assert [s for shard in shards for s in shard] == list(DEFAULT_SYMBOLS)
        assert len(shards) == -(-len(DEFAULT_SYMBOLS) // HL_SUBSCRIPTIONS_PER_SOCKET)

    @pytest.mark.asyncio
    async def test_start_runs_one_socket_loop_per_shard(self, monkeypatch):
        import asyncio

        from src.data_layer.orderflow_engine import OrderFlowEngine
        e = OrderFlowEngine()
        seen: list[int] = []

        async def fake_loop(shard=0):
            seen.append(shard)
            await asyncio.sleep(10)

        async def fake_binance():
            await asyncio.sleep(10)

        monkeypatch.setattr(e, "_run_forever", fake_loop)
        monkeypatch.setattr(e, "_binance_trade_loop", fake_binance)
        await e.start()
        await asyncio.sleep(0)
        try:
            assert sorted(seen) == list(range(len(e._hl_shards())))
            assert len(e._hl_tasks) == len(e._hl_shards()) == 7
        finally:
            await e.stop()
        assert e._hl_tasks == []

    def test_venue_state_follows_first_and_last_shard(self):
        """Seven shards reconnecting independently must not read as the venue
        reconnecting seven times, and one shard dropping is not 'disconnected'."""
        from unittest.mock import MagicMock

        from src.data_layer.orderflow_engine import OrderFlowEngine
        e = OrderFlowEngine()
        st = e.venues["hyperliquid"]
        e._venue_connected("hyperliquid")
        first_at = st.connected_at
        e._venue_connected("hyperliquid")            # second shard up
        assert st.connects == 2
        assert st.connected_at == first_at           # not reset by the second shard

        open_ws = MagicMock(closed=False)
        e._hl_sockets = {0: open_ws, 1: MagicMock(closed=True)}
        assert e.hl_sockets_open == 1

    @pytest.mark.asyncio
    async def test_force_reconnect_closes_every_open_shard(self):
        from unittest.mock import AsyncMock, MagicMock

        from src.data_layer.orderflow_engine import OrderFlowEngine
        e = OrderFlowEngine()
        a, b, c = MagicMock(closed=False), MagicMock(closed=False), MagicMock(closed=True)
        for ws in (a, b, c):
            ws.close = AsyncMock()
        e._hl_sockets = {0: a, 1: b, 2: c}
        assert await e.force_reconnect() == 2
        a.close.assert_awaited_once()
        b.close.assert_awaited_once()
        c.close.assert_not_awaited()

    def test_hub_watchdog_uses_shard_aware_reconnect(self):
        import inspect

        from src.data_layer.hub import HyperDataHub
        src = inspect.getsource(HyperDataHub._update_feed_staleness)
        assert "orderflow.force_reconnect()" in src
        assert "orderflow._ws" not in src

    @pytest.mark.asyncio
    async def test_stop_lets_shards_close_their_sessions(self, monkeypatch):
        """Seen live: five 'Unclosed client session' errors at shutdown — one
        per healthy shard — because stop() cancelled each loop while it was
        inside `await session.close()`. stop() must let the close finish."""
        import asyncio

        from src.data_layer.orderflow_engine import OrderFlowEngine
        e = OrderFlowEngine(symbols=["BTC"])

        class FakeSession:
            closed = False

            async def close(self):
                await asyncio.sleep(0.1)        # a real connector close takes time
                self.closed = True

        class FakeWS:
            closed = False

            async def close(self):
                self.closed = True
                gate.set()

        gate = asyncio.Event()
        session, ws = FakeSession(), FakeWS()

        async def fake_connect_and_listen(shard=0):
            e._hl_sessions[shard] = session
            e._hl_sockets[shard] = ws
            try:
                await gate.wait()               # "socket closed by stop()"
            finally:
                e._hl_sockets.pop(shard, None)
                e._hl_sessions.pop(shard, None)
                if not session.closed:
                    await asyncio.shield(session.close())

        monkeypatch.setattr(e, "_connect_and_listen", fake_connect_and_listen)
        e._running = True
        e._hl_tasks = [asyncio.create_task(e._run_forever(0))]
        await asyncio.sleep(0)
        await e.stop()
        assert ws.closed is True
        assert session.closed is True           # not abandoned mid-close
        assert e._hl_tasks == []


# ── S1: a dead Hyperliquid shard must be visible, not hidden by a live one ──

def _sharded_engine(now: float):
    """16 symbols -> 2 shards; shard 0 up and trading, shard 1's state is
    left for the test to set. Mirrors what start() sets up, without sockets."""
    from unittest.mock import MagicMock

    from src.data_layer.orderflow_engine import OrderFlowEngine, ShardState
    e = OrderFlowEngine(symbols=[f"S{i}" for i in range(16)])
    e._hl_shard_plan = e._hl_shards()
    e._hl_shard_state = {0: ShardState(connected_at=now - 300), 1: ShardState(down_since=now - 300)}
    e._hl_sockets = {0: MagicMock(closed=False)}
    e._venue_connected("hyperliquid")
    e._handle_message({"channel": "trades", "data": [
        {"coin": "S0", "px": "1", "sz": "1", "side": "B", "time": int(now * 1000), "tid": 1},
    ]})
    return e


class TestS1ShardLiveness:
    """Pre-fix: last_hl_message_at was stamped by ANY shard's trades and the
    venue only went 'disconnected' when `not self._hl_sockets`, so with one
    shard alive Hyperliquid read `ok` no matter how many were dark, and
    hl_sockets_open reached nothing but a log line."""

    def test_one_live_shard_no_longer_hides_a_dead_one(self):
        import time as _t
        now = _t.time()
        e = _sharded_engine(now)
        status, reason = e.venue_status("hyperliquid", now)
        assert status == "partial"
        assert "1/2 sockets open" in reason and "shards [1] dark" in reason and "S8" in reason
        fresh = e.venue_freshness(now)["hyperliquid"]
        assert fresh["sockets_open"] == 1 and fresh["sockets_expected"] == 2
        assert fresh["shards_dark"] == [1] and fresh["dark_symbols"] == [f"S{i}" for i in range(8, 16)]
        assert fresh["connected"] is True and fresh["stale"] is False
        # Still contributing (shard 0's trades are real), just not fully.
        assert e.contributing_venues(now) == ["hyperliquid"]
        # 'binance' carries no shard fields: sharding is a Hyperliquid thing.
        assert "sockets_open" not in e.venue_freshness(now)["binance"]

    def test_grace_idle_and_recovery(self):
        import time as _t
        from unittest.mock import MagicMock
        now = _t.time()
        e = _sharded_engine(now)
        st = e._hl_shard_state[1]
        st.down_since = now - 5                              # between reconnects, inside grace
        assert e.venue_status("hyperliquid", now)[0] == "ok"
        st.down_since = now - 31                             # past the grace: dark
        assert e.venue_status("hyperliquid", now)[0] == "partial"
        st.idle = True                                       # none of its symbols listed: expected to have no socket
        assert e.venue_status("hyperliquid", now)[0] == "ok"
        assert e.hl_shard_status(now)["sockets_expected"] == 1
        assert e.hl_shard_status(now)["shards_idle"] == [1]
        st.idle = False
        st.connected_at, st.down_since = now - 1, 0.0        # back up
        e._hl_sockets[1] = MagicMock(closed=False)
        assert e.venue_status("hyperliquid", now)[0] == "ok"

    def test_flapping_shard_is_dark_even_while_its_socket_is_briefly_open(self):
        """The confirmed-live failure: a rejected subscription closes the
        socket ~0.6s after every connect and the 1-15s backoff keeps every
        gap under the 30s grace, so 'socket gone for >30s' never fires."""
        import time as _t
        from unittest.mock import MagicMock

        from src.data_layer.orderflow_engine import HL_SHARD_FLAP_CLOSES
        now = _t.time()
        e = _sharded_engine(now)
        st = e._hl_shard_state[1]
        st.short_closes = HL_SHARD_FLAP_CLOSES
        st.connected_at, st.down_since = now - 0.5, 0.0      # just reconnected, again
        e._hl_sockets[1] = MagicMock(closed=False)
        assert e.hl_sockets_open == 2                        # the old signal says all is well
        assert e.venue_status("hyperliquid", now)[0] == "partial"
        st.connected_at = now - 31                           # this socket has lived: proven
        assert e.venue_status("hyperliquid", now)[0] == "ok"

    @pytest.mark.asyncio
    async def test_run_forever_counts_short_closes_and_resets_on_a_long_one(self, monkeypatch):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from src.data_layer import orderflow_engine as of
        from src.data_layer.orderflow_engine import HL_SHARD_FLAP_CLOSES, OrderFlowEngine
        e = OrderFlowEngine(symbols=["BTC"])
        clock = {"t": 1_000_000.0}
        monkeypatch.setattr(of, "time", SimpleNamespace(time=lambda: clock["t"]))
        monkeypatch.setattr(of.asyncio, "sleep", AsyncMock())
        lives = iter([0.6, 0.6, 0.6, 120.0, 0.6])

        async def fake_connect(shard=0):
            try:
                clock["t"] += next(lives)
            except StopIteration:
                e._running = False

        monkeypatch.setattr(e, "_connect_and_listen", fake_connect)
        e._running = True
        seen: list[int] = []
        real_state = e._shard_state

        def spy(shard):
            st = real_state(shard)
            seen.append(st.short_closes)
            return st

        monkeypatch.setattr(e, "_shard_state", spy)
        await asyncio.wait_for(e._run_forever(0), timeout=2)
        st = e._hl_shard_state[0]
        # three short closes -> flapping; one long-lived socket -> reset; one short -> 1
        assert max(seen) >= HL_SHARD_FLAP_CLOSES
        assert st.short_closes == 1

    @pytest.mark.asyncio
    async def test_hub_health_and_api_reflect_a_partial_venue(self, tmp_path, monkeypatch, caplog):
        import time as _t
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from src.api_server import HyperDataAPI
        from src.data_layer.health_monitor import DataHealthMonitor
        from src.data_layer.hub import HubStatus
        now = _t.time()
        e = _sharded_engine(now)
        e._venue_connected("binance")
        e._handle_binance_trade({"data": {"s": "BTCUSDT", "p": "1", "q": "1", "m": False,
                                          "T": int(now * 1000), "a": 1}})

        # Hub watchdog: 'partial', with the shard reason in the warning.
        hub = _isolated_hub(tmp_path, monkeypatch)
        try:
            hub.orderflow = e
            hub.status.orderflow_engine = "connected"
            hub.status.started_at = now - 600
            with caplog.at_level("WARNING"):
                await hub._update_feed_staleness()
            assert hub.status.orderflow_engine == "partial"
            assert "hyperliquid is partial" in caplog.text and "shards [1] dark" in caplog.text
        finally:
            hub.store.close()

        # Health monitor: the per-venue check warns, the blended one passes.
        mon_hub = SimpleNamespace(
            orderflow=e,
            orderbook=MagicMock(is_stale=lambda: False, data_age=lambda: 1.0),
            status=SimpleNamespace(last_market_refresh=now),
            deribit=MagicMock(get_latest=lambda x: None),
            positions=_scanner(monkeypatch),
        )
        checks = {c.name: c for c in DataHealthMonitor(mon_hub)._check_freshness()}
        assert checks["order_flow"].status == "pass"
        assert checks["order_flow_hyperliquid"].status == "warn"
        assert checks["order_flow_hyperliquid"].detail.startswith("partial:")

        # /v1/health: shard counts are a field, not a log line; status 'warn'.
        api_hub = MagicMock()
        api_hub.status = HubStatus(mode="live", orderflow_engine="partial")
        api_hub.orderflow = e
        api_hub.positions = _scanner(monkeypatch)
        api_hub.health.latest.return_value = {"overall": "ok"}
        api = HyperDataAPI(hub=api_hub)
        app = web.Application()
        app.router.add_get("/v1/health", api.handle_health)
        app.router.add_get("/v1/orderflow/{symbol}", api.handle_orderflow)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            body = await (await client.get("/v1/health")).json()
            hl = body["orderflow_venues"]["hyperliquid"]
            assert body["status"] == "warn"
            assert hl["status"] == "partial"
            assert hl["sockets_open"] == 1 and hl["sockets_expected"] == 2 and hl["shards_dark"] == [1]
            flow = await (await client.get("/v1/orderflow/S0")).json()
            assert flow["venue_coverage"]["hyperliquid"] == "partial"
            assert "hyperliquid" in flow["venues_contributing"]
        finally:
            await client.close()

    def test_cvd_renderer_keeps_the_number_and_flags_partial(self):
        import time as _t

        from rich.console import Console

        from src.dashboards.cvd_dashboard import venue_cvd_text
        e = _sharded_engine(_t.time())
        console = Console(record=True, width=120, force_terminal=False)
        console.print(venue_cvd_text(e, "S0"))
        text = console.export_text()
        assert "HL +1 partial" in text

    @pytest.mark.asyncio
    async def test_idle_shard_wakes_for_stop_instead_of_burning_the_grace(self, monkeypatch):
        """Nit: the idle branch slept HL_UNIVERSE_TTL without checking
        _running, so stop() waited the full STOP_GRACE_SECONDS on it."""
        import asyncio
        import time as _t
        from unittest.mock import AsyncMock

        from src.data_layer.orderflow_engine import OrderFlowEngine
        e = OrderFlowEngine(symbols=["PEPE"])                       # not listed -> idles
        monkeypatch.setattr(e, "_fetch_hl_universe", AsyncMock(return_value={"BTC"}))
        e._running = True
        e._stop_event = asyncio.Event()
        e._hl_shard_plan = e._hl_shards()
        e._hl_shard_state = {}

        class FakeWS:                                               # a healthy shard alongside it
            closed = False

            async def close(self):
                self.closed = True

        e._hl_sockets[1] = FakeWS()
        task = asyncio.create_task(e._run_forever(0))
        e._hl_tasks = [task]
        await asyncio.sleep(0.05)
        assert e._hl_shard_state[0].idle is True
        assert e.hl_shard_status()["shards_idle"] == [0]
        t0 = _t.monotonic()
        await e.stop()
        assert _t.monotonic() - t0 < 2.0                            # not STOP_GRACE_SECONDS (5s)
        assert task.done() and not task.cancelled()                 # it returned, it was not cut off


# ── S7: the shard plan is fixed at start(); add_symbol() cannot grow it ──

class TestS7ShardPlanFixed:
    @pytest.mark.asyncio
    async def test_add_symbol_while_running_does_not_repartition_shards(self, monkeypatch, caplog):
        import asyncio

        from src.data_layer.orderflow_engine import OrderFlowEngine
        e = OrderFlowEngine(symbols=[f"S{i}" for i in range(8)])   # exactly one full shard

        async def park(*a, **k):
            await asyncio.sleep(10)

        monkeypatch.setattr(e, "_run_forever", park)
        monkeypatch.setattr(e, "_binance_trade_loop", park)
        await e.start()
        try:
            plan = [list(x) for x in e._hl_shard_plan]
            with caplog.at_level("WARNING"):
                e.add_symbol("NEW")
            assert "NEW" in e.buckets
            assert len(e._hl_shards()) == 2                         # the naive recompute grows...
            assert e._hl_shard_plan == plan and len(e._hl_tasks) == 1   # ...the plan and the loops do not
            assert "shard plan is fixed at start()" in caplog.text
        finally:
            await e.stop()

    def test_add_symbol_before_start_is_silent(self, caplog):
        """Control: adding a symbol BEFORE start() is the supported path and
        must stay quiet."""
        from src.data_layer.orderflow_engine import OrderFlowEngine
        e = OrderFlowEngine(symbols=["BTC"])
        with caplog.at_level("WARNING"):
            e.add_symbol("ETH")
        assert "shard plan" not in caplog.text
        assert e._hl_shards() == [["BTC", "ETH"]]


# ── M13: extraction equivalence (demo generators, liquidation processing) ──

class TestM13Extraction:
    def test_hub_demo_bodies_moved_and_delegated(self):
        import inspect

        from src.data_layer import hub_demo
        from src.data_layer.hub import HyperDataHub
        for name in ("liquidation_generator", "trade_generator", "position_scan", "smart_money",
                     "hlp", "market_refresh", "deribit", "basis", "lsr"):
            assert callable(getattr(hub_demo, f"demo_{name}"))
            body = inspect.getsource(getattr(HyperDataHub, f"_demo_{name}"))
            assert f"hub_demo.demo_{name}(self)" in body
            assert body.count("\n") <= 3                      # a delegator, not a body
        hub_src = inspect.getsource(HyperDataHub)
        assert "random.choices" not in hub_src                # no generator code left in the orchestrator

    @pytest.mark.asyncio
    async def test_demo_generators_still_populate_the_hub(self, tmp_path, monkeypatch):
        """Behavioural equivalence: the one-shot generators fill the same
        component state as before, with the C1/C3/H4 fields attached."""
        import asyncio

        hub = _isolated_hub(tmp_path, monkeypatch)
        try:
            hub._running = True
            await hub._demo_position_scan()
            assert len(hub.positions.positions) == 60
            assert all(p.scanned_at > 0 for p in hub.positions.positions)
            assert hub.positions.last_scan_at > 0 and not hub.positions.is_stale()

            await hub._demo_market_refresh()
            assert len(hub.market.assets) == 20 and hub.market.assets["BTC"].price > 0

            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(hub._demo_smart_money(), timeout=0.05)
            wallets = hub.smart_money.wallets.values()
            assert len([w for w in wallets if w.rank > 0]) == 150
            tiers = {t: sum(1 for w in wallets if w.tier == t) for t in ("smart", "average", "dumb")}
            assert tiers == {"smart": 15, "average": 120, "dumb": 15}
            assert all(w.confidence > 0 for w in wallets if w.rank > 0)
        finally:
            hub.store.close()

    @pytest.mark.asyncio
    async def test_demo_hub_lifecycle(self, tmp_path, monkeypatch):
        """The whole demo hub starts, produces data and stops cleanly —
        previously the orchestrator had no lifecycle test at all."""
        import asyncio

        from src.data_layer import persistence
        monkeypatch.setattr(persistence, "DB_PATH", tmp_path / "hub.db")
        monkeypatch.setattr(address_store, "DATA_DIR", tmp_path)
        monkeypatch.setattr(address_store, "DB_PATH", tmp_path / "hub.db")
        monkeypatch.setattr(address_store, "LEGACY_JSON", tmp_path / "legacy.json")
        monkeypatch.setattr(address_store, "_initialized", False)
        from src.data_layer.hub import HyperDataHub
        hub = HyperDataHub(demo=True)
        await hub.start()
        try:
            await asyncio.sleep(0.4)
            s = hub.status
            assert s.mode == "demo"
            assert (s.liquidation_feed, s.orderflow_engine, s.position_scanner, s.market_data) == ("demo",) * 4
            assert hub.orderflow.synthetic is True
            assert s.tracked_positions == 60
            assert len(hub.market.assets) == 20
            assert s.total_trades_processed > 0
            assert s.failed_components == []
        finally:
            await hub.stop()
        assert hub.status.mode == "offline"

    def test_liquidation_processor_is_the_single_implementation(self):
        """The API's dedup/cascade/symbol methods are delegators; the logic
        (and its tests in TestLiquidationDedup) now exercise the data-layer
        class through them."""
        import inspect
        from unittest.mock import MagicMock

        from src.api_server import HyperDataAPI
        from src.data_layer.liquidation_processing import LiquidationProcessor
        api = HyperDataAPI(hub=MagicMock())
        assert isinstance(api._liq, LiquidationProcessor)
        for name in ("_is_duplicate_liq", "_check_cascade", "_clean_symbol", "_log_liq_stats"):
            assert inspect.getsource(getattr(HyperDataAPI, name)).count("\n") <= 3
        assert api._CASCADE_BYPASS_DURATION == LiquidationProcessor.CASCADE_BYPASS_DURATION
        assert api._clean_symbol("1000pepe") == "PEPE"
        assert api._clean_symbol("龙虾") == "LOBSTER"
        # State views are the processor's own dicts, not copies.
        assert api._cascade_bypass is api._liq.cascade_bypass

    def test_estimate_leverage(self):
        from types import SimpleNamespace

        from src.data_layer.liquidation_processing import LiquidationProcessor
        est = LiquidationProcessor.estimate_leverage
        assert est(SimpleNamespace(price=100.0, quantity=10.0, size_usd=100.0)) == 10
        assert est(SimpleNamespace(price=100.0, quantity=10.0, size_usd=1000.0)) is None   # 1x: implausible
        assert est(SimpleNamespace(price=100.0, quantity=10.0, size_usd=1.0)) is None      # 1000x: implausible
        assert est(SimpleNamespace(price=0.0, quantity=10.0, size_usd=1.0)) is None
