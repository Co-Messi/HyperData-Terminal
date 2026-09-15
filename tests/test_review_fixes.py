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


# ── H7 / M12: /v1/health top-level status and docs URL ───────────

class TestH7HealthStatus:
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
    def test_hub_panels(self, symbol):
        from unittest.mock import MagicMock

        from src.dashboards.hub_panels import HubHLP, HubLiqWatch, HubMarket, HubWhales
        from src.data_layer.hlp_tracker import HLPPosition
        hub = MagicMock()
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
