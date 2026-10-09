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


def test_hl_large_print_socket_skips_unlisted_coins():
    from hyperdata_terminal.data_layer.liquidation_feed import HyperliquidConnection

    conn = HyperliquidConnection(LiquidationFeed())

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return {"universe": [{"name": n} for n in ("BTC", "ETH", "SOL", "kPEPE")]}

    class _Session:
        def post(self, *a, **k):
            return _Resp()

    conn._session = _Session()
    import asyncio

    coins = asyncio.run(conn._listed_coins())
    assert coins == ["BTC", "ETH", "SOL"] and "PEPE" not in coins


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

    def test_aum_source_switch_does_not_fake_session_pnl(self):
        from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

        tracker = HLPTracker()
        states = [_state(79_000_000, [])]
        tracker.record_snapshot(HLPTracker.build_snapshot(states))                      # vaultDetails down
        tracker.record_snapshot(HLPTracker.build_snapshot(states, reported_aum=179_900_000))  # it came back
        tracker.record_snapshot(HLPTracker.build_snapshot(states, reported_aum=179_950_000))
        pnl = [s.session_pnl for s in tracker.snapshots]
        assert pnl == [0.0, 0.0, pytest.approx(50_000)]  # never +$100M
        assert tracker.get_stats()["aum_source"] == "vaultDetails"

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
        assert resume == int((tracker._started_at - HLPTracker.NOTIFY_BACKFILL_SECONDS) * 1000)
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


def test_env_file_sets_the_data_dir(tmp_path, monkeypatch):
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
    assert out.stdout.strip() == str(target)


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
