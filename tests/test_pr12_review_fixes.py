"""Regression tests for the findings of the PR #12 review.

Each test pins one reported bug: MCP tools racing the hub loop, symbol
filtering of liquidation totals, estimated events evicting confirmed ones,
HLP AUM/exposure, HLP fill dedupe across polls, the CLI's .env ordering and
busy-port handling, strategy loader edge cases, and the CVD symbol labels.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from hyperdata_terminal.data_layer.liquidation_feed import LiquidationEvent, LiquidationFeed

# ── liquidation feed ─────────────────────────────────────────────────────


async def _feed_with(events):
    feed = LiquidationFeed(max_events=5)
    for ev in events:
        await feed.emit(ev)
    return feed


async def test_stats_filter_by_symbol():
    now = time.time()
    feed = await _feed_with([
        LiquidationEvent(now, "okx", "STRK", "long", 100, 1, 1),
        LiquidationEvent(now, "okx", "BTC", "short", 900, 1, 1),
    ])
    btc = feed.get_stats(60, include_estimated=False, symbol="btc")
    assert btc["total_count"] == 1 and btc["total_volume_usd"] == 900
    assert list(btc["by_symbol"]) == ["BTC"]


async def test_large_prints_cannot_evict_confirmed_liquidations():
    now = time.time()
    confirmed = [LiquidationEvent(now - i, "okx", "BTC", "long", 10, 1, 1) for i in range(3)]
    prints = [LiquidationEvent(now, "hyperliquid", "BTC", "short", 50_000, 1, 1, confirmed=False) for _ in range(20)]
    feed = await _feed_with(confirmed + prints)
    assert len(feed.events) == 3            # all confirmed still there (maxlen 5)
    assert len(feed.estimated_events) == 5  # prints capped in their own buffer
    assert feed.get_stats(60, include_estimated=False)["total_count"] == 3
    assert feed.get_stats(60)["heuristic_count"] == 5


async def test_get_recent_merges_newest_first_and_can_exclude_estimates():
    now = time.time()
    feed = await _feed_with([
        LiquidationEvent(now - 10, "okx", "BTC", "long", 1, 1, 1),
        LiquidationEvent(now - 5, "hyperliquid", "BTC", "short", 1, 1, 1, confirmed=False),
        LiquidationEvent(now - 20, "bybit", "BTC", "long", 1, 1, 1),  # arrives late (HLP backfill)
    ])
    assert [e.exchange for e in feed.get_recent(1)] == ["hyperliquid", "okx", "bybit"]
    assert [e.exchange for e in feed.get_recent(1, include_estimated=False)] == ["okx", "bybit"]


def test_hl_large_prints_ride_the_order_flow_socket():
    """The liquidation feed used to open its own Hyperliquid trades socket
    (with its own unlisted coin filter); Hyperliquid allows 10 websockets
    per IP, so large prints now come from the order flow engine's stream,
    which already skips unlisted coins."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from hyperdata_terminal.data_layer import liquidation_feed as lf

    feed = LiquidationFeed()
    with patch.object(lf.ExchangeConnection, "start", AsyncMock()), \
            patch.object(lf.ExchangeConnection, "stop", AsyncMock()):
        asyncio.run(feed.start())
        assert [c.name for c in feed._connections] == ["binance", "bybit", "okx"]
        asyncio.run(feed.stop())


# ── HLP ──────────────────────────────────────────────────────────────────


def _state(account_value, positions):
    return {
        "marginSummary": {"accountValue": str(account_value), "totalMarginUsed": "0"},
        "assetPositions": [
            {"position": {"coin": c, "szi": str(szi), "positionValue": str(abs(val)), "entryPx": "1",
                          "unrealizedPnl": "0", "leverage": {"value": 1}}}
            for c, szi, val in positions
        ],
    }


class TestHlp:
    def test_aum_prefers_reported_and_exposure_is_gross(self):
        from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

        states = [_state(3_000_000, [("BTC", 1, 1_000_000)]), _state(3_000_000, [("BTC", -0.9, 900_000)])]
        snap = HLPTracker.build_snapshot(states, reported_aum=179_900_000)
        assert snap.account_value == 179_900_000
        assert snap.net_delta_usd == pytest.approx(100_000)
        assert snap.total_exposure_usd == pytest.approx(1_900_000)  # not the netted 100K
        assert HLPTracker.build_snapshot(states).account_value == 6_000_000  # fallback: summed

    @staticmethod
    def _details(aum, cum_pnl):
        return {"portfolio": [
            ["day", {"accountValueHistory": [[1, "0"], [2, str(aum)]], "pnlHistory": [[1, "0"], [2, "7"]]}],
            ["allTime", {"accountValueHistory": [[2, str(aum)]], "pnlHistory": [[1, "0"], [2, str(cum_pnl)]]}],
        ]}

    def test_session_pnl_ignores_deposits_and_withdrawals(self):
        """Second review: AUM fell $103K in a day while PnL rose $20K. Session PnL
        must follow Hyperliquid's cumulative PnL series, not the AUM."""
        from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

        tracker = HLPTracker()
        states = [_state(79_000_000, [])]
        tracker.record_snapshot(HLPTracker.build_snapshot(states))  # vaultDetails not read yet
        tracker.apply_vault_details(self._details(179_900_000, 138_601_049))
        tracker.record_snapshot(HLPTracker.build_snapshot(states, reported_aum=tracker.reported_aum))
        # A $2M withdrawal and $20,497 of profit later:
        tracker.apply_vault_details(self._details(177_920_497, 138_621_546))
        tracker.record_snapshot(HLPTracker.build_snapshot(states, reported_aum=tracker.reported_aum))
        snaps = list(tracker.snapshots)
        assert [s.pnl_known for s in snaps] == [False, True, True]
        assert [round(s.session_pnl) for s in snaps] == [0, 0, 20_497]
        assert tracker.get_stats()["aum_source"] == "vaultDetails"

    def test_stale_reported_aum_falls_back_and_says_so(self):
        from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

        tracker = HLPTracker()
        tracker.apply_vault_details(self._details(179_900_000, 1.0), now=time.time() - 3600)
        assert not tracker._details_fresh()  # an hour old: not shown as current

    def test_one_liquidation_filled_by_two_vaults_is_one_absorption(self):
        from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

        tracker = HLPTracker()
        absorbed = []
        tracker.on_hlp_absorption(lambda a, first: absorbed.append((a.size_usd, first)))
        liq = {"coin": "APT", "side": "A", "dir": "Open Short", "time": 5_000, "hash": "0x69fa",
               "liquidation": {"method": "market"}}
        tracker.process_fills("0xA", [dict(liq, px="2", sz="10", tid=1), dict(liq, px="2", sz="5", tid=2)])
        tracker.process_fills("0xB", [dict(liq, px="2", sz="20", tid=3)])
        tracker.flush_absorptions()
        assert len(tracker.absorptions) == 1 and absorbed == [(70.0, True)]
        group = tracker.absorptions["0x69fa"]
        assert group.size == 35 and group.vault == "0xA,0xB"
        # A later poll adds a third vault's share: same absorption, reported as an update.
        tracker.process_fills("0xC", [dict(liq, px="2", sz="1", tid=4)])
        tracker.flush_absorptions()
        assert absorbed[-1] == (72.0, False)

    async def test_busy_vault_pages_forward_during_a_spike(self):
        from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

        tracker = HLPTracker()
        tracker._fill_watermark["0xbusy"] = 0
        backlog = [{"coin": "X", "px": "1", "sz": "1", "side": "B", "dir": "Open Long", "tid": i, "time": 10 + i}
                   for i in range(4500)]
        backlog[4200]["liquidation"] = {"method": "market"}
        backlog[4200]["hash"] = "0xspike"
        pages = []

        async def fake_post(payload):
            start = payload["startTime"]
            page = [f for f in backlog if f["time"] >= start][: HLPTracker.FILLS_PAGE_LIMIT]
            pages.append(len(page))
            return page

        tracker._post = fake_post
        await tracker._fetch_fills("0xbusy")
        assert pages[:3] == [2000, 2000, 502]  # inclusive start time: one overlap fill per page
        assert "0xspike" in tracker.absorptions

    def test_absorption_upsert_survives_restart_and_growth(self, tmp_path):
        from hyperdata_terminal.data_layer.hlp_tracker import HLPTrade
        from hyperdata_terminal.data_layer.persistence import DataStore

        def absorption(size):
            return HLPTrade(1.0, "JUP", "sell", 1, size, size, "Open Short", 0, True, "market", "0xA", "0xd80d")

        store = DataStore(tmp_path / "s.db")
        try:
            store._save_hlp_absorption(absorption(10), True)
            store._save_hlp_absorption(absorption(30), False)   # B's share arrived a poll later
            store._save_hlp_absorption(absorption(30), True)    # quick restart re-reads it
            store.flush()
            rows = store._conn.execute("SELECT COUNT(*), MAX(size) FROM hlp_trades").fetchone()
        finally:
            store.close()
        assert rows == (1, 30)

    async def test_failed_vault_details_is_retried_next_pass(self):
        from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

        tracker = HLPTracker()

        async def down(payload):
            return None

        tracker._post = down
        await tracker.refresh_vault_details()
        assert tracker._vault_details_at == 0.0  # still due: the snapshot loop retries in 30s

    def test_parse_reported_aum(self):
        from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

        details = {"portfolio": [["day", {"accountValueHistory": [[1, "1.0"], [2, "179900121.41"]]}],
                                 ["week", {"accountValueHistory": [[1, "5"]]}]]}
        assert HLPTracker.parse_reported_aum(details) == pytest.approx(179_900_121.41)
        assert HLPTracker.parse_reported_aum({}) == 0.0

    def test_empty_response_does_not_reset_dedupe(self):
        from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

        tracker = HLPTracker()
        seen = []
        tracker.on_hlp_trade(seen.append)
        base = {"coin": "ETH", "px": "1", "sz": "1", "side": "A", "dir": "Open Short"}
        fills = [dict(base, time=1_000, tid=1), dict(base, time=2_000, tid=2,
                                                       liquidation={"method": "backstop"})]
        tracker.process_fills("0xv", fills)
        tracker.process_fills("0xv", [])                         # an empty or lagging poll
        tracker.process_fills("0xv", fills)                      # same fills again
        tracker.process_fills("0xv", [fills[1], dict(base, time=2_000, tid=3)])  # same ms, new tid
        assert [t.timestamp for t in seen] == [1000.0, 2000.0, 2000.0]  # tid 1, tid 2, then only tid 3
        assert len(tracker.absorptions) == 1

    async def test_busy_vault_first_page_keeps_absorptions_and_resumes_at_start(self):
        from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

        tracker = HLPTracker()
        tracker._started_at = time.time()
        page = [{"coin": "X", "px": "1", "sz": "1", "side": "B", "dir": "Open Long", "tid": i,
                 "time": int((time.time() - 80_000) * 1000) + i} for i in range(HLPTracker.FILLS_PAGE_LIMIT)]
        page[5]["liquidation"] = {"method": "market"}
        requests = []

        async def fake_post(payload):
            requests.append(payload)
            return page

        tracker._post = fake_post
        await tracker._fetch_fills("0xbusy")
        assert requests[0]["type"] == "userFillsByTime"
        assert len(tracker.trades) == 1 and len(tracker.absorptions) == 1  # only the flagged fill kept
        resume = tracker._fill_watermark["0xbusy"]
        # One poll interval before start: covers the end of a previous run (upserted by hash).
        assert resume == int((tracker._started_at - HLPTracker.FILLS_INTERVAL) * 1000)
        await tracker._fetch_fills("0xbusy")
        assert requests[1]["startTime"] == resume  # no paging through a day of fills

    def test_persistence_keeps_absorptions_only(self, tmp_path):
        from hyperdata_terminal.data_layer.hlp_tracker import HLPTrade
        from hyperdata_terminal.data_layer.persistence import DataStore

        store = DataStore(tmp_path / "s.db")
        try:
            for liq in (False, True, False):
                store._save_hlp_trade(HLPTrade(time.time(), "BTC", "buy", 1, 1, 1, "Open Long", 0, liq))
            store.flush()
            rows = store._conn.execute("SELECT COUNT(*), SUM(is_liquidation) FROM hlp_trades").fetchone()
        finally:
            store.close()
        assert rows == (1, 1)


def test_db_from_an_early_build_of_this_branch_gets_the_hlp_columns(tmp_path):
    """Early builds recorded schema v5 without the HLP columns; v6 must add them."""
    import sqlite3

    from hyperdata_terminal.data_layer.hlp_tracker import HLPTrade
    from hyperdata_terminal.data_layer.persistence import DataStore

    path = tmp_path / "early_v5.db"
    store = DataStore(path)
    store.close()
    conn = sqlite3.connect(path)
    conn.executescript("""
        DROP INDEX IF EXISTS idx_hlp_trade_hash;
        CREATE TABLE hlp_trades_old AS SELECT id, timestamp, symbol, side, price, size, size_usd,
            direction, closed_pnl, is_liquidation, created_at FROM hlp_trades;
        DROP TABLE hlp_trades;
        ALTER TABLE hlp_trades_old RENAME TO hlp_trades;
        DELETE FROM schema_version WHERE version > 5;
    """)
    conn.commit()
    conn.close()
    store = DataStore(path)
    try:
        assert store.get_schema_version() == DataStore.SCHEMA_VERSION >= 6
        store._save_hlp_absorption(HLPTrade(1.0, "JUP", "sell", 1, 2, 2, "Open Short", 0, True, "market", "0xA", "0xh"))
        store._save_hlp_absorption(HLPTrade(1.0, "JUP", "sell", 1, 3, 3, "Open Short", 0, True, "market", "0xA", "0xh"))
        store.flush()
        assert store._conn.execute("SELECT COUNT(*), MAX(size) FROM hlp_trades").fetchone() == (1, 3)
    finally:
        store.close()


def test_long_short_source_is_persisted(tmp_path):
    from hyperdata_terminal.data_layer.long_short_ratio import LongShortSnapshot
    from hyperdata_terminal.data_layer.persistence import DataStore

    store = DataStore(tmp_path / "s.db")
    try:
        store.save_long_short_ratio(LongShortSnapshot(time.time(), "BTC", 0.6, 0.4, 1.5, source="okx"))
        rows = store.get_long_short_ratios("BTC")
    finally:
        store.close()
    assert rows[0]["source"] == "okx"


# ── MCP ──────────────────────────────────────────────────────────────────


def test_every_mcp_tool_runs_on_the_event_loop():
    pytest.importorskip("mcp")
    from hyperdata_terminal.mcp_server import build_server

    server = build_server(hub_factory=lambda: None)
    tools = server._tool_manager.list_tools()
    assert len(tools) == 10
    assert all(t.is_async for t in tools), [t.name for t in tools if not t.is_async]


async def test_mcp_liquidations_symbol_filters_totals(tmp_path, monkeypatch):
    from hyperdata_terminal.mcp_server import HubTools
    from tests.test_launch_readiness import _isolated_hub

    hub = _isolated_hub(tmp_path, monkeypatch)
    now = time.time()
    await hub.liquidations.emit(LiquidationEvent(now, "okx", "STRK", "long", 100, 1, 1))
    out = HubTools(hub).liquidations(symbol="BTC")
    assert out["symbol"] == "BTC" and out["totals"]["count"] == 0 and out["by_exchange"] == {}


# ── CLI ──────────────────────────────────────────────────────────────────


def test_cli_import_does_not_resolve_the_data_dir():
    """paths must be imported after .env is loaded, so HYPERDATA_DATA_DIR in .env works."""
    code = "import sys, hyperdata_terminal.cli; print('hyperdata_terminal.paths' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_cwd_env_file_cannot_move_the_data_dir(tmp_path, monkeypatch):
    """A .env in the working directory (a cloned repository, or whatever
    project an MCP client has open) used to be able to point the data dir
    anywhere; that, the API bind and the LLM endpoint now come only from the
    environment or the data dir's own .env."""
    target = tmp_path / "from-dotenv"
    (tmp_path / ".env").write_text(f"HYPERDATA_DATA_DIR={target}\n")
    code = (
        "import os; os.environ.pop('HYPERDATA_DATA_DIR', None)\n"
        "from hyperdata_terminal import cli\n"
        "cli._load_env()\n"
        "from hyperdata_terminal.paths import DATA_DIR; print(DATA_DIR)\n"
    )
    import os

    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=tmp_path, env=env)
    assert out.stdout.strip() != str(target)
    assert "ignored HYPERDATA_DATA_DIR" in out.stderr


async def test_api_command_fails_loudly_when_the_port_is_taken(monkeypatch, capsys):
    from hyperdata_terminal import cli
    from hyperdata_terminal.data_layer import hub as hub_mod

    class FakeHub:
        def __init__(self, **kw):
            self.status = type("S", (), {"failed_components": ["api_server"]})()
            self.stopped = False

        async def start(self):
            pass

        async def stop(self):
            self.stopped = True

    monkeypatch.setattr(hub_mod, "HyperDataHub", FakeHub)
    assert await cli._run_api(8420) == 1
    assert "could not start the API" in capsys.readouterr().err


def test_api_port_falls_back_to_global_flag():
    from hyperdata_terminal.cli import build_parser

    args = build_parser().parse_args(["--api-port", "9000", "api"])
    assert args.port is None and args.api_port == 9000


# ── strategy loader ──────────────────────────────────────────────────────


class TestLoaderEdges:
    def test_strategy_can_import_a_sibling_helper(self, tmp_path):
        from hyperdata_terminal.strategies.loader import load_strategies

        (tmp_path / "pr12_helper_mod.py").write_text("THRESHOLD = 7\n")
        f = tmp_path / "with_helper.py"
        f.write_text(
            "from pr12_helper_mod import THRESHOLD\n"
            "from hyperdata_terminal.strategies import Strategy\n"
            "class H(Strategy):\n"
            "    name = 'with_helper'\n"
            "    def evaluate(self, hub):\n"
            "        return None\n"
        )
        assert [s.name for s in load_strategies([str(f)])] == ["with_helper"]

    def test_constructor_arguments_are_a_clean_error(self, tmp_path):
        from hyperdata_terminal.strategies.loader import StrategyLoadError, load_strategies

        f = tmp_path / "needs_args.py"
        f.write_text(
            "from hyperdata_terminal.strategies import Strategy\n"
            "class N(Strategy):\n"
            "    name = 'n'\n"
            "    def __init__(self, x):\n"
            "        self.x = x\n"
            "    def evaluate(self, hub):\n"
            "        return None\n"
        )
        with pytest.raises(StrategyLoadError, match="no arguments"):
            load_strategies([str(f)])

    def test_missing_name_says_what_is_missing(self, tmp_path):
        from hyperdata_terminal.strategies.loader import StrategyLoadError, load_strategies

        f = tmp_path / "no_name.py"
        f.write_text(
            "from hyperdata_terminal.strategies import Strategy\n"
            "class M(Strategy):\n"
            "    def evaluate(self, hub):\n"
            "        return None\n"
        )
        with pytest.raises(StrategyLoadError, match="missing name"):
            load_strategies([str(f)])

    def test_user_strategy_shadowing_a_builtin_name_is_an_error(self, tmp_path):
        from hyperdata_terminal.strategies.loader import StrategyLoadError, load_strategies

        f = tmp_path / "shadow.py"
        f.write_text(
            "from hyperdata_terminal.strategies import Strategy\n"
            "class S(Strategy):\n"
            "    name = 'cvd_momentum'\n"
            "    def evaluate(self, hub):\n"
            "        return None\n"
        )
        with pytest.raises(StrategyLoadError, match="two different strategies"):
            load_strategies(["cvd_momentum", str(f)])


# ── CVD labels ───────────────────────────────────────────────────────────


def test_cvd_dashboard_labels_follow_the_symbol():
    from rich.console import Console

    from hyperdata_terminal.dashboards.cvd_dashboard import CVDDashboard
    from hyperdata_terminal.data_layer.orderflow_engine import OrderFlowEngine

    dash = CVDDashboard(engine=OrderFlowEngine(symbols=["SOL"]), symbol="SOL")
    console = Console(width=160, record=True, file=open("/dev/null", "w"))
    console.print(dash.build_dashboard())
    text = console.export_text()
    assert "SOL CVD" in text and "BITCOIN" not in text and "BTC CVD" not in text
    assert "Moon Dev" not in text


def test_no_third_party_brand_in_the_package():
    pkg = Path(__file__).resolve().parents[1] / "hyperdata_terminal"
    hits = [p.name for p in pkg.rglob("*.py") if "Moon Dev" in p.read_text()]
    assert hits == []


# ── second review round ──────────────────────────────────────────────────


def test_strategy_helpers_from_different_folders_do_not_collide(tmp_path):
    from hyperdata_terminal.strategies.loader import load_strategies

    paths = []
    for name, value in (("alpha", 1), ("beta", 2)):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "pr12_shared_helpers.py").write_text(f"VALUE = {value}\n")
        f = folder / f"{name}.py"
        f.write_text(
            "import pr12_shared_helpers\n"
            "from hyperdata_terminal.strategies import Strategy\n"
            "class S(Strategy):\n"
            f"    name = '{name}'\n"
            "    value = pr12_shared_helpers.VALUE\n"
            "    def evaluate(self, hub):\n"
            "        return None\n"
        )
        paths.append(str(f))
    loaded = {s.name: s.value for s in load_strategies(paths)}
    assert loaded == {"alpha": 1, "beta": 2}


def test_source_cooldown_separates_geoblocks_from_blips():
    import aiohttp

    from hyperdata_terminal.utils.helpers import (
        BLOCKED_SOURCE_COOLDOWN,
        TRANSIENT_SOURCE_COOLDOWN,
        source_cooldown_seconds,
    )

    def http(status):
        return aiohttp.ClientResponseError(request_info=None, history=(), status=status)

    assert source_cooldown_seconds(http(451)) == BLOCKED_SOURCE_COOLDOWN
    assert source_cooldown_seconds(http(403)) == BLOCKED_SOURCE_COOLDOWN
    assert source_cooldown_seconds(http(502)) == TRANSIENT_SOURCE_COOLDOWN
    assert source_cooldown_seconds(TimeoutError()) == TRANSIENT_SOURCE_COOLDOWN
    assert TRANSIENT_SOURCE_COOLDOWN < 60


# ── third review round ───────────────────────────────────────────────────


def test_loader_does_not_evict_libraries_installed_under_the_strategy_folder(tmp_path):
    """A project .venv under the strategy's folder: its libraries must survive the load."""
    from hyperdata_terminal.strategies.loader import load_strategies

    site = tmp_path / ".venv" / "lib" / "site-packages"
    (site / "pr12_fakelib").mkdir(parents=True)
    (site / "pr12_fakelib" / "__init__.py").write_text("class Frame:\n    pass\n")
    sys.path.insert(0, str(site))
    try:
        f = tmp_path / "lib_strategy.py"
        f.write_text(
            "import pr12_fakelib\n"
            "from hyperdata_terminal.strategies import Strategy\n"
            "class L(Strategy):\n"
            "    name = 'lib_strategy'\n"
            "    def evaluate(self, hub):\n"
            "        return None\n"
        )
        load_strategies([str(f)])
        assert "pr12_fakelib" in sys.modules  # not evicted: it is not one of the folder's own modules
    finally:
        sys.path.remove(str(site))
        sys.modules.pop("pr12_fakelib", None)


def test_restart_reread_never_shrinks_a_stored_absorption(tmp_path):
    from hyperdata_terminal.data_layer.hlp_tracker import HLPTrade
    from hyperdata_terminal.data_layer.persistence import DataStore

    def absorption(size, vault):
        return HLPTrade(1.0, "JUP", "sell", 0.36, size, size * 0.36, "Open Short", 0, True, "market", vault, "0xjup")

    store = DataStore(tmp_path / "s.db")
    try:
        store._save_hlp_absorption(absorption(582, "A,B"), True)  # session 1: both vaults
        store._save_hlp_absorption(absorption(310, "A"), True)    # session 2 re-read: one vault's share
        store.flush()
        row = store._conn.execute("SELECT size, vault FROM hlp_trades").fetchone()
    finally:
        store.close()
    assert row == (582, "A,B")


def test_stale_details_make_pnl_unknown_not_frozen():
    from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

    tracker = HLPTracker()
    details = TestHlp._details(179_900_000, 100.0)
    tracker.apply_vault_details(details, now=time.time())
    tracker.record_snapshot(HLPTracker.build_snapshot([_state(1, [])], reported_aum=tracker.reported_aum))
    tracker._vault_details_at = time.time() - 3300  # 55 minutes without a fresh reading
    tracker.record_snapshot(HLPTracker.build_snapshot([_state(1, [])]))
    assert [s.pnl_known for s in tracker.snapshots] == [True, False]


async def test_stop_flushes_absorptions_read_mid_pass():
    from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

    tracker = HLPTracker()
    got = []
    tracker.on_hlp_absorption(lambda a, first: got.append(a.fill_hash))
    tracker.process_fills("0xA", [{"coin": "X", "px": "1", "sz": "1", "side": "A", "dir": "Open Short",
                                   "time": 5, "tid": 1, "hash": "0xabc", "liquidation": {"method": "market"}}])
    await tracker.stop()
    assert got == ["0xabc"]


def test_zero_hash_is_not_used_for_grouping():
    from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

    tracker = HLPTracker()
    base = {"coin": "X", "px": "1", "sz": "1", "side": "A", "dir": "Open Short", "time": 5,
            "hash": "0x" + "0" * 64, "liquidation": {"method": "market"}}
    tracker.process_fills("0xA", [dict(base, tid=1), dict(base, tid=2)])
    assert len(tracker.absorptions) == 2  # not merged into one


async def test_gap_beyond_retention_is_warned(caplog):
    from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

    tracker = HLPTracker()
    tracker._fill_watermark["0xbusy"] = 0
    start_after_gap = 10 * 60 * 1000  # the oldest retrievable fill is 10 minutes past the watermark

    async def fake_post(payload):
        return [{"coin": "X", "px": "1", "sz": "1", "side": "B", "dir": "Open Long", "tid": i,
                 "time": start_after_gap + i} for i in range(HLPTracker.FILLS_PAGE_LIMIT)]

    tracker._post = fake_post
    with caplog.at_level("WARNING"):
        await tracker._fetch_fills("0xbusy")
    assert "no longer available" in caplog.text


def test_demo_hlp_runs_without_errors(caplog):
    import asyncio

    from hyperdata_terminal.data_layer import hub_demo

    class FakeHub:
        def __init__(self):
            from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

            self.hlp = HLPTracker()
            self._running = True

    hub = FakeHub()

    async def run_briefly():
        task = asyncio.create_task(hub_demo.demo_hlp(hub))
        await asyncio.sleep(0.3)
        hub._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with caplog.at_level("ERROR"):
        asyncio.run(run_briefly())
    assert "Demo HLP error" not in caplog.text
    snap = hub.hlp.get_latest_snapshot()
    assert snap is not None and snap.pnl_known and snap.aum_source == "vaultDetails"


async def test_smart_money_keeps_to_its_weight_budget(monkeypatch):
    """A batch of active wallets' userFills (~120 weight each) drew HTTP 429s.
    Smart money now goes through the process-wide governor as an elastic
    caller and waits once its allocation of the minute is spent."""
    from hyperdata_terminal.data_layer import hl_rate
    from hyperdata_terminal.data_layer.smart_money import SmartMoneyEngine
    from tests.test_hl_rate import FakeTime, _Resp, _Session

    t = FakeTime()
    gov = hl_rate.reset_governor(hl_rate.HLRateGovernor(budget_per_min=1000.0, clock=t.clock, sleep=t.sleep))
    engine = SmartMoneyEngine()
    engine._session = _Session(_Resp(200, [{}] * 2000))
    for _ in range(6):
        assert len(await engine._post({"type": "userFills", "user": "0x" + "a" * 40})) == 2000
    assert gov.weight_by_component["smart_money"] == 6 * 120
    assert t.slept  # waited instead of firing over budget
    assert gov.used_last_minute("smart_money") <= gov.allocation("smart_money") + 120


def test_mcp_hlp_vault_reports_aum_source_and_pnl_validity(tmp_path, monkeypatch):
    from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker
    from hyperdata_terminal.mcp_server import HubTools
    from tests.test_launch_readiness import _isolated_hub

    hub = _isolated_hub(tmp_path, monkeypatch)
    hub.hlp.record_snapshot(HLPTracker.build_snapshot([_state(79_000_000, [])]))
    out = HubTools(hub).hlp_vault()
    assert out["aum_source"] == "clearinghouseState" and out["aum_is_partial"] is True
    assert out["session_pnl_usd"] is None  # unknown, not a misleading 0


def _okx_frame(inst_id: str, bk_px: str, sz: str, side: str = "buy") -> dict:
    return {"arg": {"channel": "liquidation-orders"}, "data": [{
        "instId": inst_id,
        "details": [{"bkPx": bk_px, "sz": sz, "side": side, "ts": "1791531267237"}],
    }]}


@pytest.mark.parametrize("inst_id,spec,bk_px,sz,usd,qty", [
    # live sample: 7.31 contracts of 0.01 BTC, was reported as $603K
    ("BTC-USDT-SWAP", ("linear", 0.01), "82561.5", "7.31", 6035.25, 0.0731),
    ("DOGE-USDT-SWAP", ("linear", 1000.0), "0.25", "2", 500.0, 2000.0),  # was $0.50
    ("BTC-USD-SWAP", ("inverse", 100.0), "80000", "3", 300.0, 0.00375),  # $100 a contract
])
async def test_okx_liquidation_size_uses_contract_value(inst_id, spec, bk_px, sz, usd, qty):
    """OKX reports liquidation size in contracts; contracts x price was off by 100x for BTC."""
    from hyperdata_terminal.data_layer.liquidation_feed import OKXConnection

    feed = LiquidationFeed()
    got = []
    feed.on_liquidation(got.append)
    conn = OKXConnection(feed)
    conn._contracts = {inst_id: spec}
    await conn._on_message(_okx_frame(inst_id, bk_px, sz))
    assert len(got) == 1
    assert got[0].size_usd == pytest.approx(usd)
    assert got[0].quantity == pytest.approx(qty)
    assert got[0].symbol == ("BTC" if inst_id.startswith("BTC") else "DOGE")


async def test_okx_unknown_swap_is_dropped_not_guessed():
    from hyperdata_terminal.data_layer.liquidation_feed import OKXConnection

    feed = LiquidationFeed()
    got = []
    feed.on_liquidation(got.append)
    conn = OKXConnection(feed)
    conn._contracts = {"BTC-USDT-SWAP": ("linear", 0.01)}
    await conn._on_message(_okx_frame("NEWCOIN-USDT-SWAP", "1.0", "500"))
    await conn._on_message(_okx_frame("NEWCOIN-USDT-SWAP", "1.0", "500"))
    assert got == [] and feed.unsized_drops == {"okx": 2}
    assert feed.parse_errors.get("okx", 0) == 0  # well formed, just not sizable yet
    assert feed.get_stats()["unsized_drops"] == {"okx": 2}


@pytest.mark.parametrize("bk_px,sz", [("nan", "1"), ("1.0", "inf"), ("1.0", "-5"), ("-1.0", "5"), ("0", "5")])
async def test_liquidation_feed_drops_non_finite_or_non_positive_sizes(bk_px, sz):
    """float() accepts 'nan' and 'inf'; one such event made total_volume_usd nan for an hour."""
    from hyperdata_terminal.data_layer.liquidation_feed import OKXConnection

    feed = LiquidationFeed()
    got = []
    feed.on_liquidation(got.append)
    conn = OKXConnection(feed)
    conn._contracts = {"BTC-USDT-SWAP": ("linear", 0.01)}
    await conn._on_message(_okx_frame("BTC-USDT-SWAP", bk_px, sz))
    assert got == [] and feed.parse_errors == {"okx": 1}
    stats = feed.get_stats()
    assert stats["total_volume_usd"] == 0 and stats["total_count"] == 0


async def test_okx_loads_contract_values_from_instrument_list():
    from hyperdata_terminal.data_layer.liquidation_feed import OKXConnection

    body = {"code": "0", "data": [
        {"instId": "BTC-USDT-SWAP", "ctType": "linear", "ctVal": "0.01", "ctMult": "1"},
        {"instId": "BTC-USD-SWAP", "ctType": "inverse", "ctVal": "100", "ctMult": "1"},
        {"instId": "BAD-USDT-SWAP", "ctType": "linear", "ctVal": ""},
        "garbage",
    ]}

    class _Resp:
        status = 200

        async def json(self):
            return body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Session:
        closed = False

        def get(self, url, **kw):
            assert "instType=SWAP" in url
            return _Resp()

    conn = OKXConnection(LiquidationFeed())
    conn._session = _Session()
    await conn._load_contracts()
    assert conn._contracts == {"BTC-USDT-SWAP": ("linear", 0.01), "BTC-USD-SWAP": ("inverse", 100.0)}


def test_smart_money_ranked_count_matches_tiers():
    """'Ranked:10' with '0 smart / 0 dumb': the count was live, the tiers only set by rank_all."""
    from hyperdata_terminal.data_layer.smart_money import SmartMoneyEngine, WalletProfile

    engine = SmartMoneyEngine()
    for i in range(10):
        addr = f"0x{i:040x}"
        engine.wallets[addr] = WalletProfile(
            address=addr, discovered_at=0, last_seen=0, last_analyzed=0,
            total_trades=20, total_volume_usd=1e6, composite_score=1.0 - i / 10,
        )
    assert engine.get_stats()["ranked_wallets"] == 0  # qualified, not ranked yet
    engine.rank_all()
    stats = engine.get_stats()
    assert stats["ranked_wallets"] == 10 and stats["smart_wallets"] == 1 and stats["dumb_wallets"] == 1


async def test_smart_money_ranks_after_every_wallet(monkeypatch):
    import asyncio

    from hyperdata_terminal.data_layer.smart_money import SmartMoneyEngine, WalletProfile

    engine = SmartMoneyEngine()
    for i in range(12):
        addr = f"0x{i:040x}"
        engine.wallets[addr] = WalletProfile(address=addr, discovered_at=0, last_seen=i, last_analyzed=0)
    seen = []

    async def fake_analyze(address):
        seen.append(engine.get_stats()["ranked_wallets"])
        w = engine.wallets[address]
        w.total_trades, w.total_volume_usd = 20, 1e6
        w.composite_score = w.last_seen
        w.last_analyzed = time.time()
        return w

    async def stop_sleep(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(engine, "analyze_wallet", fake_analyze)
    monkeypatch.setattr(asyncio, "sleep", stop_sleep)
    engine._running = True
    with pytest.raises(asyncio.CancelledError):  # the sleep after the batch ends the test
        await engine._analysis_loop()
    assert seen == list(range(12))  # each wallet sees every earlier one ranked


@pytest.mark.parametrize("n,warming", [(0, True), (4, True), (50, False)])
def test_smart_money_panel_explains_warmup_until_tiers_exist(n, warming):
    """Under the weight budget tiers take minutes; 1 to 9 ranked showed bare '---' rows."""
    from unittest.mock import MagicMock

    from rich.console import Console

    from hyperdata_terminal.dashboards.hub_panels import HubSmartMoney
    from tests.test_review_fixes import TestC1Tiers

    engine = TestC1Tiers._engine_with(n)
    hub = MagicMock()
    hub.smart_money = engine
    hub.get_smart_money = engine.get_smart_money
    hub.get_dumb_money = engine.get_dumb_money
    hub.get_smart_money_signals = lambda n=50: []
    console = Console(record=True, width=160, force_terminal=False)
    console.print(HubSmartMoney(hub).build_compact())
    text = console.export_text()
    assert (f"tiers start at 10 ranked ({n} so far)" in text) is warming


@pytest.mark.parametrize("argv", [
    ["paper", "--balance", "0"], ["paper", "--balance=-5"], ["paper", "--balance", "abc"],
    ["paper", "--interval", "0"], ["paper", "--minutes", "0"],
    ["paper", "--balance", "inf"], ["paper", "--balance", "nan"], ["paper", "--minutes", "inf"],
])
def test_paper_rejects_non_positive_numbers(argv):
    """Codex P2: --balance 0 divided by zero on exit, before the hub was stopped."""
    from hyperdata_terminal.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


def test_paper_accepts_positive_numbers():
    from hyperdata_terminal.cli import build_parser

    args = build_parser().parse_args(["paper", "--balance", "500", "--interval", "5", "--minutes", "0.5"])
    assert (args.balance, args.interval, args.minutes) == (500.0, 5, 0.5)



async def test_smart_money_analyzes_new_wallets_before_busy_analyzed_ones(monkeypatch):
    """Sorted by recent activity alone, busy analyzed wallets were due again before new ones got a turn."""
    import asyncio

    from hyperdata_terminal.data_layer.smart_money import SmartMoneyEngine, WalletProfile

    engine = SmartMoneyEngine()
    now = time.time()
    for i in range(25):  # busy, analyzed 10 minutes ago, seen just now
        a = f"0xa{i:039x}"
        engine.wallets[a] = WalletProfile(address=a, discovered_at=0, last_seen=now, last_analyzed=now - 600)
    for i in range(5):   # new, never analyzed, seen a while ago
        a = f"0xb{i:039x}"
        engine.wallets[a] = WalletProfile(address=a, discovered_at=0, last_seen=now - 120 + i, last_analyzed=0)
    order = []

    async def fake_analyze(address):
        order.append(address)
        engine.wallets[address].last_analyzed = time.time()
        return engine.wallets[address]

    async def stop_sleep(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(engine, "analyze_wallet", fake_analyze)
    monkeypatch.setattr(asyncio, "sleep", stop_sleep)
    engine._running = True
    with pytest.raises(asyncio.CancelledError):
        await engine._analysis_loop()
    assert len(order) == engine.ANALYSIS_BATCH_SIZE
    assert order[:5] == [f"0xb{i:039x}" for i in (4, 3, 2, 1, 0)]  # new first, most recently seen first
