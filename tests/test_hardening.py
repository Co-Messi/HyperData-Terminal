"""Tests for the adversarial-review hardening pass.

Covers: API bind guard / auth / CORS / rate limiting, WebSocket abuse limits,
liquidation dedup + cascade bypass, malformed exchange payloads, degraded hub
startup, paper-trader accounting invariants, LLM response parsing, persistence
corruption quarantine + schema versioning, and alert log redaction.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from data_layer.liquidation_feed import (
    BinanceConnection,
    LiquidationEvent,
    LiquidationFeed,
    OKXConnection,
)
from data_layer.orderbook import OrderBookEngine
from data_layer.persistence import DataStore
from src.api_server import (
    WS_BAD_MSG_LIMIT,
    HyperDataAPI,
    _is_loopback_host,
    _make_auth_middleware,
    _make_cors_middleware,
    _make_rate_limit_middleware,
    _RateLimiter,
    _WSClient,
)
from src.strategies.base import Signal
from src.strategies.llm_agent import LLMAgent
from src.strategies.paper_trader import PaperTrader

# ── Helpers ──────────────────────────────────────────────────────

def _liq_event(**overrides) -> LiquidationEvent:
    defaults = dict(
        timestamp=time.time(),
        exchange="binance",
        symbol="BTC",
        side="long",
        size_usd=25_000.0,
        price=70_000.0,
        quantity=0.357,
    )
    defaults.update(overrides)
    return LiquidationEvent(**defaults)


def _api(monkeypatch=None, host="127.0.0.1") -> HyperDataAPI:
    return HyperDataAPI(hub=MagicMock(), host=host)


async def _client_for(middlewares) -> TestClient:
    app = web.Application(middlewares=middlewares)

    async def ok(request):
        return web.json_response({"ok": True})

    app.router.add_get("/v1/health", ok)
    app.router.add_get("/v1/whales", ok)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


# ── Bind guard / security resolution ─────────────────────────────

class TestBindGuard:
    def test_loopback_hosts(self):
        assert _is_loopback_host("127.0.0.1")
        assert _is_loopback_host("localhost")
        assert _is_loopback_host("::1")
        assert not _is_loopback_host("0.0.0.0")
        assert not _is_loopback_host("192.168.1.10")
        assert not _is_loopback_host("")

    def test_loopback_needs_no_key(self, monkeypatch):
        monkeypatch.delenv("HYPERDATA_API_KEY", raising=False)
        monkeypatch.delenv("HYPERDATA_UNSAFE_PUBLIC_API", raising=False)
        monkeypatch.delenv("HYPERDATA_CORS_ORIGINS", raising=False)
        key, origins = _api(host="127.0.0.1")._resolve_security()
        assert key == ""
        assert origins is None  # wildcard CORS allowed on loopback

    def test_public_bind_refused_without_key_or_ack(self, monkeypatch):
        monkeypatch.delenv("HYPERDATA_API_KEY", raising=False)
        monkeypatch.delenv("HYPERDATA_UNSAFE_PUBLIC_API", raising=False)
        with pytest.raises(RuntimeError, match="Refusing to bind"):
            _api(host="0.0.0.0")._resolve_security()

    def test_public_bind_allowed_with_key(self, monkeypatch):
        monkeypatch.setenv("HYPERDATA_API_KEY", "sekrit")
        monkeypatch.delenv("HYPERDATA_CORS_ORIGINS", raising=False)
        key, origins = _api(host="0.0.0.0")._resolve_security()
        assert key == "sekrit"
        assert origins == set()  # never wildcard CORS off loopback

    def test_public_bind_allowed_with_explicit_ack(self, monkeypatch):
        monkeypatch.delenv("HYPERDATA_API_KEY", raising=False)
        monkeypatch.setenv("HYPERDATA_UNSAFE_PUBLIC_API", "1")
        key, origins = _api(host="0.0.0.0")._resolve_security()
        assert key == ""
        assert origins == set()

    def test_cors_allowlist_parsed(self, monkeypatch):
        monkeypatch.setenv("HYPERDATA_API_KEY", "k")
        monkeypatch.setenv("HYPERDATA_CORS_ORIGINS", "https://a.example, https://b.example")
        _, origins = _api(host="0.0.0.0")._resolve_security()
        assert origins == {"https://a.example", "https://b.example"}


# ── Middlewares over a live test server ──────────────────────────

class TestAuthMiddleware:
    @pytest.mark.asyncio
    async def test_key_required_except_health(self):
        client = await _client_for([
            _make_auth_middleware("sekrit"),
            _make_cors_middleware(None),
        ])
        try:
            assert (await client.get("/v1/health")).status == 200
            assert (await client.get("/v1/whales")).status == 401
            ok_bearer = await client.get(
                "/v1/whales", headers={"Authorization": "Bearer sekrit"})
            assert ok_bearer.status == 200
            ok_header = await client.get(
                "/v1/whales", headers={"X-API-Key": "sekrit"})
            assert ok_header.status == 200
            bad = await client.get(
                "/v1/whales", headers={"Authorization": "Bearer wrong"})
            assert bad.status == 401
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_cors_allowlist_echoes_only_allowed_origin(self):
        client = await _client_for([
            _make_cors_middleware({"https://ok.example"}),
        ])
        try:
            allowed = await client.get(
                "/v1/health", headers={"Origin": "https://ok.example"})
            assert allowed.headers.get("Access-Control-Allow-Origin") == "https://ok.example"
            denied = await client.get(
                "/v1/health", headers={"Origin": "https://evil.example"})
            assert "Access-Control-Allow-Origin" not in denied.headers
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_rate_limit_returns_429(self):
        limiter = _RateLimiter(max_requests=3, window_s=60)
        client = await _client_for([_make_rate_limit_middleware(limiter)])
        try:
            for _ in range(3):
                assert (await client.get("/v1/health")).status == 200
            assert (await client.get("/v1/health")).status == 429
        finally:
            await client.close()


class TestRateLimiterUnit:
    def test_sliding_window(self):
        limiter = _RateLimiter(max_requests=2, window_s=10)
        assert limiter.allow("ip", now=100.0)
        assert limiter.allow("ip", now=101.0)
        assert not limiter.allow("ip", now=102.0)
        # Window slides: the first hit expires.
        assert limiter.allow("ip", now=110.5)

    def test_per_key_isolation(self):
        limiter = _RateLimiter(max_requests=1, window_s=10)
        assert limiter.allow("a", now=1.0)
        assert limiter.allow("b", now=1.0)
        assert not limiter.allow("a", now=2.0)


# ── WebSocket abuse limits ───────────────────────────────────────

class TestWSLimits:
    def _client(self) -> _WSClient:
        ws = MagicMock()
        ws.closed = False
        return _WSClient(ws)

    @pytest.mark.asyncio
    async def test_bad_messages_disconnect(self):
        api = _api()
        client = self._client()
        for i in range(WS_BAD_MSG_LIMIT - 1):
            assert api._ws_msg_violates_limits(client, "{not json") is False
        assert api._ws_msg_violates_limits(client, "{not json") is True

    @pytest.mark.asyncio
    async def test_subscribe_still_works(self):
        api = _api()
        client = self._client()
        raw = json.dumps({"subscribe": ["trade", "bogus_channel"]})
        assert api._ws_msg_violates_limits(client, raw) is False
        assert client.subscriptions == {"trade"}
        # Confirmation got queued for the writer task.
        assert client.queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_message_flood_disconnects(self):
        api = _api()
        client = self._client()
        raw = json.dumps({"subscribe": ["trade"]})
        violated = False
        for _ in range(50):
            if api._ws_msg_violates_limits(client, raw):
                violated = True
                break
        assert violated

    @pytest.mark.asyncio
    async def test_broadcast_drops_when_queue_full(self):
        api = _api()
        client = self._client()
        client.subscriptions = {"trade"}
        api._ws_clients.append(client)
        # No writer task draining -> queue fills to maxsize then drops.
        for _ in range(client.queue.maxsize + 10):
            api._broadcast("trade", {"x": 1})
        assert client.queue.qsize() == client.queue.maxsize
        assert client.dropped_msgs == 10


# ── Liquidation dedup + cascade bypass ───────────────────────────

class TestLiquidationDedup:
    def test_same_event_deduped(self):
        api = _api()
        ev = _liq_event()
        assert api._is_duplicate_liq(ev) is False
        assert api._is_duplicate_liq(ev) is True

    def test_different_exchanges_not_deduped(self):
        api = _api()
        ts = time.time()
        assert api._is_duplicate_liq(_liq_event(exchange="binance", timestamp=ts)) is False
        assert api._is_duplicate_liq(_liq_event(exchange="okx", timestamp=ts)) is False

    def test_dedup_uses_exchange_timestamp_not_local_clock(self):
        api = _api()
        # Two records of the same event in different dedup buckets by
        # exchange time are distinct regardless of local arrival time.
        assert api._is_duplicate_liq(_liq_event(timestamp=1000.0)) is False
        assert api._is_duplicate_liq(_liq_event(timestamp=1009.0)) is False
        assert api._is_duplicate_liq(_liq_event(timestamp=1000.5)) is True

    def test_cascade_bypass_lifts_dedup_for_own_venue_only(self):
        api = _api()
        ts = time.time()
        binance = _liq_event(exchange="binance", timestamp=ts)
        # Three rapid events trigger the cascade bypass for binance/BTC/long.
        for _ in range(3):
            api._check_cascade(binance)
        # A duplicate binance record now passes (cascade mode)...
        assert api._is_duplicate_liq(binance) is False
        assert api._is_duplicate_liq(binance) is False
        # ...but hyperliquid's heuristic stream still dedups normally.
        hl = _liq_event(exchange="hyperliquid", timestamp=ts)
        assert api._is_duplicate_liq(hl) is False
        assert api._is_duplicate_liq(hl) is True


# ── Malformed exchange payloads ──────────────────────────────────

class TestMalformedPayloads:
    @pytest.mark.asyncio
    async def test_binance_malformed_dropped_not_raised(self):
        feed = LiquidationFeed()
        conn = BinanceConnection(feed)
        received = []
        feed.on_liquidation(received.append)

        await conn._on_message({"e": "forceOrder", "o": {"p": "", "q": "1", "S": "SELL",
                                                         "s": "BTCUSDT", "T": 1}})
        await conn._on_message({"e": "forceOrder", "o": {}})
        await conn._on_message({"e": "forceOrder"})
        assert received == []
        assert feed.parse_errors["binance"] == 3

        # A valid message still parses after the bad ones.
        await conn._on_message({"e": "forceOrder", "o": {
            "p": "70000", "q": "0.5", "S": "SELL", "s": "BTCUSDT", "T": 1700000000000,
        }})
        assert len(received) == 1
        assert received[0].symbol == "BTC"
        assert received[0].side == "long"

    @pytest.mark.asyncio
    async def test_okx_malformed_detail_dropped_individually(self):
        feed = LiquidationFeed()
        conn = OKXConnection(feed)
        received = []
        feed.on_liquidation(received.append)

        await conn._on_message({"data": [{
            "instId": "BTC-USDT-SWAP",
            "details": [
                {"bkPx": "", "sz": "bogus", "side": "sell", "ts": "x"},   # bad
                {"bkPx": "70000", "sz": "1", "side": "sell", "ts": "1700000000000"},
            ],
        }]})
        assert len(received) == 1
        assert received[0].symbol == "BTC"
        assert feed.parse_errors["okx"] == 1

    @pytest.mark.asyncio
    async def test_okx_non_list_shapes(self):
        feed = LiquidationFeed()
        conn = OKXConnection(feed)
        await conn._on_message({"data": "not-a-list"})
        await conn._on_message({"data": [{"instId": "X", "details": "not-a-list"}]})
        assert feed.parse_errors["okx"] == 2

    def test_orderbook_malformed_levels_dropped(self):
        engine = OrderBookEngine(symbols=["BTC"])
        engine._update_book("BTC", {"levels": [
            [{"px": "70000", "sz": "1"}, {"px": "", "sz": "zzz"}, "garbage"],
            [{"px": "70010", "sz": "2"}],
        ]})
        book = engine.books["BTC"]
        assert len(book["bids"]) == 1
        assert book["bids"][0].price == 70000.0
        assert len(book["asks"]) == 1

    def test_orderbook_non_list_levels_ignored(self):
        engine = OrderBookEngine(symbols=["BTC"])
        engine._update_book("BTC", {"levels": {"bad": "shape"}})
        assert engine.books["BTC"]["bids"] == []


# ── Degraded hub startup ─────────────────────────────────────────

class TestHubDegradedStartup:
    @pytest.mark.asyncio
    async def test_failed_component_recorded_not_swallowed(self, tmp_path, monkeypatch):
        from src.data_layer import address_store, persistence
        monkeypatch.setattr(persistence, "DB_PATH", tmp_path / "hub.db")
        monkeypatch.setattr(address_store, "DATA_DIR", tmp_path)
        monkeypatch.setattr(address_store, "DB_PATH", tmp_path / "hub.db")
        monkeypatch.setattr(address_store, "LEGACY_JSON", tmp_path / "legacy.json")
        monkeypatch.setattr(address_store, "_initialized", False)

        from src.data_layer.hub import HyperDataHub
        hub = HyperDataHub()
        # Every component start is stubbed: one fails, the rest succeed.
        hub.liquidations.start = AsyncMock(side_effect=ConnectionError("down"))
        for comp in (hub.orderflow, hub.smart_money, hub.hlp, hub.funding,
                     hub.lsr, hub.orderbook, hub.deribit):
            comp.start = AsyncMock()
        hub.spot.start = AsyncMock()

        await hub._start_live()

        assert hub.status.failed_components == ["liquidation_feed"]
        assert hub.status.liquidation_feed == "error"
        assert hub.status.orderflow_engine == "connected"
        # Loop-driven components are 'starting', never a blind 'ready'.
        assert hub.status.position_scanner == "starting"
        assert hub.status.market_data == "starting"

        hub.store.close()


# ── Paper trader accounting invariants ───────────────────────────

class TestPaperTraderAccounting:
    def _trader(self, price=100.0, balance=10_000.0) -> PaperTrader:
        hub = MagicMock()
        hub.market.assets = {"BTC": SimpleNamespace(price=price)}
        return PaperTrader(hub, [], starting_balance=balance)

    def test_add_to_position_is_balance_checked(self):
        trader = self._trader(balance=10_000.0)
        trader._execute_trade("t", Signal("BTC", "BUY", size_usd=6_000.0))
        assert trader.balance == 4_000.0
        # Second same-direction BUY exceeds the remaining balance -> rejected.
        trader._execute_trade("t", Signal("BTC", "BUY", size_usd=6_000.0))
        assert trader.balance == 4_000.0
        assert trader.positions["BTC"]["size_usd"] == 6_000.0
        assert trader.balance >= 0

    def test_repeated_buys_never_go_negative(self):
        trader = self._trader(balance=1_000.0)
        for _ in range(50):
            trader._execute_trade("t", Signal("BTC", "BUY", size_usd=400.0))
            assert trader.balance >= 0
        assert trader.positions["BTC"]["size_usd"] == 800.0

    def test_weighted_average_entry_price(self):
        trader = self._trader(price=100.0)
        trader._execute_trade("t", Signal("BTC", "BUY", size_usd=1_000.0))
        trader.hub.market.assets["BTC"].price = 200.0
        trader._execute_trade("t", Signal("BTC", "BUY", size_usd=1_000.0))
        pos = trader.positions["BTC"]
        # (100*1000 + 200*1000) / 2000 = 150
        assert abs(pos["entry_price"] - 150.0) < 1e-9
        assert pos["size_usd"] == 2_000.0

    def test_close_realizes_pnl(self):
        trader = self._trader(price=100.0, balance=1_000.0)
        trader._execute_trade("t", Signal("BTC", "BUY", size_usd=500.0))
        trader.hub.market.assets["BTC"].price = 110.0
        trader._execute_trade("t", Signal("BTC", "SELL", size_usd=500.0))
        # +10% on 500 = +50
        assert abs(trader.balance - 1_050.0) < 1e-9
        assert "BTC" not in trader.positions

    def test_invalid_signals_rejected(self):
        trader = self._trader()
        for bad in [
            Signal("BTC", "BUY", size_usd=-5.0),
            Signal("BTC", "BUY", size_usd=0.0),
            Signal("BTC", "BUY", size_usd=float("nan")),
            Signal("BTC", "BUY", size_usd=float("inf")),
            Signal("BTC", "HOLD", size_usd=100.0),
            Signal("", "BUY", size_usd=100.0),
        ]:
            trader._execute_trade("t", bad)
        assert trader.positions == {}
        assert trader.balance == 10_000.0


# ── LLM response parsing ─────────────────────────────────────────

class TestLLMParsing:
    def _agent(self) -> LLMAgent:
        return LLMAgent(symbol="BTC")

    def test_exact_actions(self):
        agent = self._agent()
        assert agent._parse_response("BUY\nmomentum").action == "BUY"
        assert agent._parse_response("SELL\nfunding").action == "SELL"
        assert agent._parse_response("HOLD\nchop") is None

    def test_case_and_punctuation_tolerated(self):
        agent = self._agent()
        assert agent._parse_response("buy.").action == "BUY"
        assert agent._parse_response("**SELL**\nx").action == "SELL"

    def test_ambiguous_rejected_not_substring_matched(self):
        agent = self._agent()
        assert agent._parse_response("I would not BUY here") is None
        assert agent._parse_response("BUY or SELL depending on funding") is None
        assert agent._parse_response("Definitely bullish") is None
        assert agent._parse_response("") is None

    def test_eval_budget(self):
        agent = self._agent()
        agent.max_evals_per_hour = 2
        agent._eval_times.clear()
        assert agent._within_budget(now=100.0)
        assert agent._within_budget(now=101.0)
        assert not agent._within_budget(now=102.0)
        # Window slides after an hour.
        assert agent._within_budget(now=100.0 + 3601)


# ── Persistence: quarantine + schema version ─────────────────────

class TestPersistence:
    def test_corrupted_db_quarantined_not_deleted(self, tmp_path):
        db_path = tmp_path / "hyperdata.db"
        db_path.write_bytes(b"this is not a sqlite database " * 100)

        store = DataStore(db_path)
        try:
            quarantine = tmp_path / "corrupted"
            quarantined = list(quarantine.glob("hyperdata.db.*"))
            assert len(quarantined) == 1
            # Original bytes preserved for postmortem.
            assert quarantined[0].read_bytes().startswith(b"this is not")
            # Fresh DB works.
            assert store.get_db_stats()["liquidations_stored"] == 0
        finally:
            store.close()

    def test_schema_version_recorded(self, tmp_path):
        store = DataStore(tmp_path / "fresh.db")
        try:
            assert store.get_schema_version() == DataStore.SCHEMA_VERSION
        finally:
            store.close()

    def test_reopen_keeps_schema_version(self, tmp_path):
        path = tmp_path / "reopen.db"
        DataStore(path).close()
        store = DataStore(path)
        try:
            assert store.get_schema_version() == DataStore.SCHEMA_VERSION
        finally:
            store.close()


# ── Alert redaction ──────────────────────────────────────────────

class TestAlertRedaction:
    @pytest.mark.asyncio
    async def test_alert_payload_not_logged(self, caplog):
        from data_layer.alerts import AlertManager
        mgr = AlertManager()
        mgr.telegram_token = ""   # no channels configured
        mgr.discord_webhook = ""
        secret_wallet = "0x" + "ab" * 20
        message = f"whale alert\nwallet {secret_wallet} is near liquidation"

        with caplog.at_level("WARNING", logger="data_layer.alerts"):
            await mgr._send(message)
        log_text = caplog.text
        assert secret_wallet not in log_text
        assert "ALERT sent" in log_text
        await mgr.stop()


# ── Smart money ranking thresholds ───────────────────────────────

class TestSmartMoneyThresholds:
    def _wallet(self, engine, addr, trades, volume, score):
        from data_layer.smart_money import WalletProfile
        w = WalletProfile(
            address=addr, discovered_at=0, last_seen=0, last_analyzed=0,
            total_trades=trades, total_volume_usd=volume, composite_score=score,
        )
        engine.wallets[addr] = w
        return w

    def test_small_samples_not_ranked(self):
        from data_layer.smart_money import SmartMoneyEngine
        engine = SmartMoneyEngine()
        tiny = self._wallet(engine, "0x" + "1" * 40, trades=3, volume=1e6, score=0.9)
        thin = self._wallet(engine, "0x" + "2" * 40, trades=50, volume=100.0, score=0.9)
        solid = self._wallet(engine, "0x" + "3" * 40,
                             trades=SmartMoneyEngine.MIN_TRADES_FOR_RANKING,
                             volume=SmartMoneyEngine.MIN_VOLUME_FOR_RANKING, score=0.5)
        engine.rank_all()
        assert tiny.rank == 0 and tiny.tier == "unknown"
        assert thin.rank == 0 and thin.tier == "unknown"
        assert solid.rank == 1 and solid.tier == "smart"

    def test_disqualified_wallet_loses_stale_tier(self):
        from data_layer.smart_money import SmartMoneyEngine
        engine = SmartMoneyEngine()
        w = self._wallet(engine, "0x" + "4" * 40, trades=20, volume=1e6, score=0.8)
        engine.rank_all()
        assert w.tier == "smart"
        w.total_trades = 2  # sample no longer qualifies
        engine.rank_all()
        assert w.rank == 0
        assert w.tier == "unknown"

    def test_confidence_scales_with_sample(self):
        from data_layer.smart_money import SmartMoneyEngine, WalletProfile
        engine = SmartMoneyEngine()
        w = WalletProfile(address="0x" + "5" * 40, discovered_at=0,
                          last_seen=0, last_analyzed=0, total_trades=25)
        assert engine._compute_confidence(w) == 0.5
        w.total_trades = 500
        assert engine._compute_confidence(w) == 1.0


# ── Per-venue orderflow freshness ────────────────────────────────

class TestPerVenueFreshness:
    def test_dead_venue_visible_while_combined_fresh(self):
        from data_layer.orderflow_engine import OrderFlowEngine
        e = OrderFlowEngine(symbols=["BTC"])
        now = time.time()
        e.last_hl_message_at = now - 1000    # HL dead
        e.last_binance_message_at = now - 1  # Binance fresh
        assert e.is_stale() is False          # combined follows freshest
        assert e.venue_is_stale("hyperliquid") is True
        assert e.venue_is_stale("binance") is False
        fresh = e.venue_freshness()
        assert fresh["hyperliquid"]["stale"] is True
        assert fresh["binance"]["stale"] is False

    def test_no_data_reports_none_age(self):
        from data_layer.orderflow_engine import OrderFlowEngine
        e = OrderFlowEngine(symbols=["BTC"])
        fresh = e.venue_freshness()
        assert fresh["hyperliquid"]["data_age_seconds"] is None
        assert fresh["hyperliquid"]["stale"] is True
