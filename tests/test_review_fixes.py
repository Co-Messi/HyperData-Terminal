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

    def test_confidence_persisted_with_wallet_and_signal(self, tmp_path):
        from types import SimpleNamespace

        from data_layer.smart_money import WalletProfile
        store = DataStore(tmp_path / "w.db")
        try:
            w = WalletProfile(address="0x" + "a" * 40, discovered_at=1, last_seen=1,
                              last_analyzed=1, tier="smart", rank=1, confidence=0.7)
            store.save_wallet(w)
            loaded = store.load_wallets()
            assert loaded[0].confidence == 0.7

            sig = SimpleNamespace(timestamp=1.0, address=w.address, tier="smart", action="OPEN_LONG",
                                  symbol="BTC", size_usd=1.0, wallet_rank=1, signal_type="follow",
                                  wallet_confidence=0.7)
            store.save_signal(sig)
            assert store.get_signals(hours=1e9)[0]["wallet_confidence"] == 0.7
        finally:
            store.close()

    def test_legacy_wallets_table_gains_confidence_column(self, tmp_path):
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE wallets (address TEXT PRIMARY KEY, tier TEXT DEFAULT 'unknown');
            CREATE TABLE smart_money_signals (id INTEGER PRIMARY KEY, timestamp REAL NOT NULL);
            CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at REAL NOT NULL);
            INSERT INTO schema_version VALUES (2, 0);
        """)
        conn.commit()
        conn.close()
        DataStore(path).close()
        conn = sqlite3.connect(str(path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(wallets)")}
        sig_cols = {r[1] for r in conn.execute("PRAGMA table_info(smart_money_signals)")}
        conn.close()
        assert "confidence" in cols
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

    def test_health_monitor_emits_per_venue_checks(self):
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
    async def test_health_endpoint_reports_silent_venue_without_bare_except(self):
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
