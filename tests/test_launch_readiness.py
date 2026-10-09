"""Tests for the installable package, the CLI, and the first-launch data fixes.

Covers: the `hyperdata` argument parser, --strategy loading, the data-dir
resolution, HLP child-vault aggregation and liquidation detection, confirmed
vs estimated liquidation totals, spot/L-S source fallback, CVD window
coverage, the distance formatter, and the MCP tool layer.
"""
from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path

import pytest

from hyperdata_terminal.data_layer import address_store

# ── helpers ────────────────────────────────────────────────────────────────


def _isolated_hub(tmp_path, monkeypatch):
    """A HyperDataHub whose SQLite files live in tmp_path (no network)."""
    from hyperdata_terminal.data_layer import persistence

    monkeypatch.setattr(persistence, "DB_PATH", tmp_path / "hub.db")
    monkeypatch.setattr(address_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(address_store, "DB_PATH", tmp_path / "hub.db")
    monkeypatch.setattr(address_store, "LEGACY_JSON", tmp_path / "legacy.json")
    monkeypatch.setattr(address_store, "_initialized", False)
    from hyperdata_terminal.data_layer.hub import HyperDataHub

    return HyperDataHub()


def _position(symbol="BTC", side="long", size=1_000_000.0, price=80_000.0, liq=76_000.0, address="0xabc"):
    from hyperdata_terminal.data_layer.position_scanner import TrackedPosition

    return TrackedPosition(
        address=address, symbol=symbol, side=side, size_usd=size, entry_price=price,
        current_price=price, liq_price=liq, distance_pct=abs(price - liq) / price * 100,
        leverage=10.0, unrealized_pnl=0.0, margin_used=size / 10, scanned_at=time.time(),
    )


def _asset(symbol="BTC", price=80_000.0, funding=0.0000125, change=-0.01):
    from hyperdata_terminal.data_layer.market_data import AssetInfo

    return AssetInfo(
        symbol=symbol, price=price, funding_rate=funding, open_interest=1e9, volume_24h=2e9,
        price_change_24h_pct=change, mark_price=price, index_price=price,
    )


# ── CLI ────────────────────────────────────────────────────────────────────


class TestCli:
    def _parse(self, *argv):
        from hyperdata_terminal.cli import build_parser

        return build_parser().parse_args(list(argv))

    def test_no_command_is_the_menu(self):
        args = self._parse()
        assert args.command is None and not args.no_boot

    @pytest.mark.parametrize("cmd,key", [
        ("heatmap", "heatmap"), ("whales", "whale"), ("whale", "whale"), ("all", "all"),
        ("combined", "all"), ("liquidations", "stream"), ("orderflow", "cvd"), ("liq", "liq"),
    ])
    def test_dashboard_commands_and_aliases(self, cmd, key):
        assert self._parse(cmd).dashboard == key

    def test_flags_before_or_after_the_subcommand(self):
        before = self._parse("--no-boot", "--api-port", "8421", "heatmap")
        after = self._parse("heatmap", "--no-boot", "--api-port", "8421")
        for args in (before, after):
            assert args.no_boot is True and args.api_port == 8421

    def test_symbol_only_on_symbol_dashboards(self):
        assert self._parse("cvd", "--symbol", "ETH").symbol == "ETH"
        with pytest.raises(SystemExit):
            self._parse("market", "--symbol", "ETH")

    def test_paper_options(self):
        args = self._parse("paper", "-s", "cvd_momentum", "-s", "./mine.py", "--interval", "5", "--minutes", "1")
        assert args.strategies == ["cvd_momentum", "./mine.py"]
        assert args.interval == 5 and args.minutes == 1.0 and args.reverse is False

    def test_version(self, capsys):
        from hyperdata_terminal import __version__

        with pytest.raises(SystemExit):
            self._parse("--version")
        assert __version__ in capsys.readouterr().out


# ── strategy loader ────────────────────────────────────────────────────────


class TestStrategyLoader:
    def test_builtin_names(self):
        from hyperdata_terminal.strategies.loader import BUILTIN_NAMES, load_strategies

        names = [s.name for s in load_strategies(list(BUILTIN_NAMES))]
        assert names == list(BUILTIN_NAMES)

    def test_user_file_outside_the_package(self, tmp_path: Path):
        from hyperdata_terminal.strategies.loader import load_strategies

        f = tmp_path / "my_strat.py"
        f.write_text(
            "from hyperdata_terminal.strategies import Signal, Strategy\n"
            "class Mine(Strategy):\n"
            "    name = 'mine'\n"
            "    def evaluate(self, hub):\n"
            "        return Signal('BTC', 'BUY')\n"
            "class Helper:\n"
            "    pass\n"
        )
        loaded = load_strategies([str(f), "cvd_momentum", str(f)])
        assert [s.name for s in loaded] == ["mine", "cvd_momentum"]  # deduped by name

    def test_file_without_a_strategy_is_an_error(self, tmp_path: Path):
        from hyperdata_terminal.strategies.loader import StrategyLoadError, load_strategies

        f = tmp_path / "empty.py"
        f.write_text("x = 1\n")
        with pytest.raises(StrategyLoadError, match="no Strategy subclass"):
            load_strategies([str(f)])

    def test_unknown_name_lists_the_builtins(self):
        from hyperdata_terminal.strategies.loader import StrategyLoadError, load_strategies

        with pytest.raises(StrategyLoadError, match="cvd_momentum"):
            load_strategies(["nope"])

    def test_module_colon_class(self):
        from hyperdata_terminal.strategies.loader import load_strategies

        loaded = load_strategies(["hyperdata_terminal.strategies.examples:WhaleFollow"])
        assert loaded[0].name == "whale_follow"


# ── data dir ──────────────────────────────────────────────────────────────


class TestDataDir:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        from hyperdata_terminal import paths

        monkeypatch.setenv("HYPERDATA_DATA_DIR", str(tmp_path / "x"))
        assert paths.resolve_data_dir() == tmp_path / "x"

    def test_installed_package_uses_the_user_dir(self, monkeypatch, tmp_path):
        from hyperdata_terminal import paths

        monkeypatch.delenv("HYPERDATA_DATA_DIR", raising=False)
        # Looks like site-packages: no pyproject.toml next to the package.
        monkeypatch.setattr(paths, "_PACKAGE_DIR", tmp_path / "site-packages" / "hyperdata_terminal")
        resolved = paths.resolve_data_dir()
        assert resolved == paths._platform_data_dir()
        assert "site-packages" not in str(resolved)

    def test_existing_checkout_keeps_its_data_dir(self, monkeypatch, tmp_path):
        from hyperdata_terminal import paths

        monkeypatch.delenv("HYPERDATA_DATA_DIR", raising=False)
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "data").mkdir()
        monkeypatch.setattr(paths, "_PACKAGE_DIR", tmp_path / "hyperdata_terminal")
        assert paths.resolve_data_dir() == tmp_path / "data"


# ── HLP ───────────────────────────────────────────────────────────────────


def _state(account_value, positions):
    return {
        "marginSummary": {"accountValue": str(account_value), "totalMarginUsed": "0"},
        "assetPositions": [
            {"position": {"coin": c, "szi": str(szi), "positionValue": str(abs(val)), "entryPx": str(px),
                          "unrealizedPnl": str(pnl), "leverage": {"value": 5}}}
            for c, szi, val, px, pnl in positions
        ],
    }


class TestHLP:
    def test_snapshot_aggregates_parent_and_children(self):
        from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

        snap = HLPTracker.build_snapshot([
            _state(40_000_000, []),                                  # parent: cash only
            _state(3_000_000, [("BTC", 1.0, 80_000, 79_000, 1_000), ("ETH", -10, 25_000, 2_400, -100)]),
            _state(3_000_000, [("BTC", 0.5, 40_000, 81_000, 500)]),  # same coin, other child
        ])
        assert snap.account_value == pytest.approx(46_000_000)
        assert snap.num_positions == 2
        btc = next(p for p in snap.positions if p.symbol == "BTC")
        assert btc.size == pytest.approx(1.5) and btc.size_usd == pytest.approx(120_000)
        assert btc.entry_price == pytest.approx((79_000 * 1.0 + 81_000 * 0.5) / 1.5)
        assert snap.net_delta_usd == pytest.approx(120_000 - 25_000)
        assert snap.total_exposure_usd == pytest.approx(145_000)

    def test_children_that_net_out_are_dropped(self):
        from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

        snap = HLPTracker.build_snapshot([
            _state(1, [("SOL", 10, 1_000, 100, 0)]), _state(1, [("SOL", -10, 1_000, 100, 0)]),
        ])
        assert snap.num_positions == 0

    def test_liquidation_comes_from_the_fill_flag_and_fills_never_repeat(self):
        from hyperdata_terminal.data_layer.hlp_tracker import HLPTracker

        tracker = HLPTracker()
        seen = []
        tracker.on_hlp_trade(seen.append)
        liq_fill = {"coin": "ETH", "px": "2000", "sz": "1", "side": "A", "time": 1_700_000_000_000, "tid": 2,
                    "dir": "Liquidated Isolated Short", "closedPnl": "0", "crossed": True,
                    "liquidation": {"liquidatedUser": "0x1", "markPx": "2001", "method": "backstop"}}
        plain_fill = {"coin": "ETH", "px": "2000", "sz": "1", "side": "B", "time": 1_699_999_999_000, "tid": 1,
                      "dir": "Open Long", "closedPnl": "0.0", "crossed": True}
        new = tracker.process_fills("0xv", [liq_fill, plain_fill])  # API order: newest first
        assert [t.is_liquidation for t in new] == [False, True]     # processed oldest first
        assert new[1].liquidation_method == "backstop" and new[1].liquidated_side == "short"
        # crossed + Open with zero PnL used to count as an absorption: it must not.
        assert tracker.get_stats()["liquidation_absorptions"] == 1
        # The next poll returns the same fills plus one new one: only the new one is processed.
        newer = dict(plain_fill, tid=3, time=1_700_000_001_000)
        assert [t.timestamp for t in tracker.process_fills("0xv", [newer, liq_fill, plain_fill])] == [1_700_000_001.0]
        assert len(seen) == 3

    async def test_hub_emits_confirmed_liquidations_only_for_this_session(self, tmp_path, monkeypatch):
        from hyperdata_terminal.data_layer.hlp_tracker import HLPTrade

        hub = _isolated_hub(tmp_path, monkeypatch)
        hub.status.started_at = time.time()

        def trade(ts):
            return HLPTrade(timestamp=ts, symbol="BTC", side="buy", price=80_000, size=2, size_usd=160_000,
                            direction="Liquidated Cross Long", closed_pnl=0, is_liquidation=True,
                            liquidation_method="backstop")

        hub._handle_hlp_trade(trade(time.time() - 86_400))  # history from the first poll
        hub._handle_hlp_trade(trade(time.time()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        events = list(hub.liquidations.events)
        assert len(events) == 1
        ev = events[0]
        assert ev.exchange == "hyperliquid" and ev.confirmed and ev.side == "long" and ev.size_usd == 160_000
        assert hub.status.total_liquidations == 1


# ── liquidation totals ───────────────────────────────────────────────────


async def test_estimated_large_prints_stay_out_of_confirmed_totals(tmp_path, monkeypatch):
    from hyperdata_terminal.data_layer.liquidation_feed import LiquidationEvent

    hub = _isolated_hub(tmp_path, monkeypatch)
    now = time.time()
    await hub.liquidations.emit(LiquidationEvent(now, "okx", "BTC", "long", 50_000, 80_000, 0.6))
    await hub.liquidations.emit(LiquidationEvent(now, "hyperliquid", "BTC", "short", 900_000, 80_000, 11,
                                                 confirmed=False))
    confirmed = hub.liquidations.get_stats(60, include_estimated=False)
    blended = hub.liquidations.get_stats(60)
    assert confirmed["total_count"] == 1 and confirmed["total_volume_usd"] == 50_000
    assert confirmed["short_count"] == 0 and "hyperliquid" not in confirmed["by_exchange"]
    assert confirmed["heuristic_count"] == 1 and confirmed["heuristic_volume_usd"] == 900_000
    assert blended["total_count"] == 2  # the API default is unchanged
    assert hub.status.total_liquidations == 1 and hub.status.total_estimated_liquidations == 1


# ── spot / L-S fallback ──────────────────────────────────────────────────


async def test_spot_falls_back_and_cools_down_a_blocked_source(monkeypatch):
    from hyperdata_terminal.data_layer import spot_prices

    collector = spot_prices.SpotPriceCollector()
    collector._get_perp_price = lambda sym: _asset(sym, price={"BTC": 80_080, "ETH": 2_000, "SOL": 100}[sym])
    calls = []

    async def fake_fetch(session, source, symbols):
        calls.append(source)
        if source == "binance":
            raise RuntimeError("451")
        return {s: {"BTC": 80_000.0, "ETH": 2_000.0, "SOL": 100.0}[s] for s in symbols}

    monkeypatch.setattr(collector, "_fetch_source", fake_fetch)
    await collector._fetch(session=None)
    await collector._fetch(session=None)
    assert calls == ["binance", "coinbase", "coinbase"]  # binance skipped while cooling down
    snap = collector.get_latest("BTC")
    assert snap.source == "coinbase" and snap.basis_pct == pytest.approx(0.1)
    assert collector.active_source == "coinbase"


async def test_long_short_falls_back_to_okx(monkeypatch):
    from hyperdata_terminal.data_layer import long_short_ratio

    collector = long_short_ratio.LongShortCollector(symbols=["BTC"])
    tried = []

    async def fake_fetch(symbol, source="binance"):
        tried.append(source)
        if source != "okx":
            raise RuntimeError("blocked")
        collector._parse_okx(symbol, {"data": [["1791516600000", "1.5"]]})

    monkeypatch.setattr(collector, "_fetch_symbol", fake_fetch)
    await collector._poll_once()
    assert tried == ["binance", "bybit", "okx"]
    snap = collector.get_latest("BTC")
    assert snap.source == "okx" and snap.long_short_ratio == pytest.approx(1.5)
    assert snap.long_ratio + snap.short_ratio == pytest.approx(1.0)


def test_bybit_ratio_parse():
    from hyperdata_terminal.data_layer.long_short_ratio import LongShortCollector

    c = LongShortCollector()
    c._parse_bybit("ETH", {"result": {"list": [{"buyRatio": "0.6", "sellRatio": "0.4", "timestamp": "1700000000000"}]}})
    assert c.get_latest("ETH").long_short_ratio == pytest.approx(1.5)
    assert c.get_latest("ETH").source == "bybit"


# ── CVD coverage ─────────────────────────────────────────────────────────


def test_cvd_windows_report_coverage_and_warmup():
    from hyperdata_terminal.data_layer.orderflow_engine import OrderFlowEngine, Trade

    engine = OrderFlowEngine(symbols=["BTC"])
    now = time.time()
    engine._process_trade(Trade(timestamp=now - 90, symbol="BTC", side="buy", price=1, size=1, size_usd=1))
    engine._process_trade(Trade(timestamp=now, symbol="BTC", side="buy", price=1, size=1, size_usd=1))
    snaps = engine.get_all_snapshots("BTC")
    assert snaps["1m"].coverage == pytest.approx(1.0) and not snaps["1m"].warming_up
    assert snaps["5m"].coverage == pytest.approx(90 / 300, abs=0.01) and snaps["5m"].warming_up
    assert engine.display_signal("BTC") == "WARMING_UP"
    # The raw engine API keeps its verdict for strategies and existing callers.
    assert engine.get_multi_timeframe_signal("BTC") == "STRONG_BULL"


def test_empty_bucket_has_zero_coverage():
    from hyperdata_terminal.data_layer.orderflow_engine import OrderFlowEngine

    assert OrderFlowEngine(symbols=["BTC"]).get_snapshot("BTC", "1h").coverage == 0.0


# ── formatting ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("value,expected", [
    (0.053, "0.05%"), (154.638, "154.64%"), (999.0, "999.00%"), (970.58, "970.58%"),
    (1500.0, ">999%"), (math.inf, ">999%"), (math.nan, ">999%"),
])
def test_distance_formatter(value, expected):
    from hyperdata_terminal.utils.helpers import format_distance_pct

    assert format_distance_pct(value) == expected


# ── MCP tool layer ───────────────────────────────────────────────────────


class TestMcpTools:
    def _tools(self, tmp_path, monkeypatch):
        from hyperdata_terminal.mcp_server import HubTools

        hub = _isolated_hub(tmp_path, monkeypatch)
        hub.status.started_at = time.time() - 60
        hub.market.assets = {"BTC": _asset("BTC"), "ETH": _asset("ETH", price=2_000, funding=-0.0002)}
        hub.positions.positions = [
            _position("BTC", "long", 2_000_000, liq=79_500),
            _position("BTC", "short", 500_000, liq=83_000, address="0xdef"),
            _position("ETH", "long", 50_000, price=2_000, liq=1_900),
        ]
        return HubTools(hub)

    def test_market_overview_and_meta(self, tmp_path, monkeypatch):
        out = self._tools(tmp_path, monkeypatch).market_overview(limit=1, sort_by="funding")
        assert [a["symbol"] for a in out["assets"]] == ["ETH"]
        assert out["assets"][0]["funding_annualized_pct"] == pytest.approx(-175.2)
        assert out["meta"]["uptime_seconds"] >= 59
        assert any("HLP" in w for w in out["meta"]["warnings"])

    def test_heatmap_splits_longs_below_and_shorts_above(self, tmp_path, monkeypatch):
        out = self._tools(tmp_path, monkeypatch).liquidation_heatmap("btc", buckets=10, range_pct=5)
        assert out["long_liquidations_below_price_usd"] == 2_000_000
        assert out["short_liquidations_above_price_usd"] == 500_000
        assert out["largest_clusters"][0]["side"] == "longs"

    def test_whales_and_near_liquidation(self, tmp_path, monkeypatch):
        tools = self._tools(tmp_path, monkeypatch)
        whales = tools.whale_positions(min_size_usd=400_000)
        assert whales["count"] == 2 and whales["positions"][0]["size_usd"] == 2_000_000
        near = tools.near_liquidation(max_distance_pct=1.0)
        assert near["count"] == 1 and near["positions"][0]["distance_to_liquidation_pct"] == pytest.approx(0.625)

    def test_unknown_symbols_are_errors_not_exceptions(self, tmp_path, monkeypatch):
        tools = self._tools(tmp_path, monkeypatch)
        assert "error" in tools.asset("NOPE")
        assert "error" in tools.order_flow("NOPE")
        assert "error" in tools.liquidation_heatmap("NOPE")

    def test_json_safe_numbers(self):
        from hyperdata_terminal.mcp_server import _num

        assert _num(math.inf) is None and _num(float("nan")) is None and _num("x") is None
        assert _num(1.23456, 2) == 1.23

    def test_server_registers_every_tool(self):
        pytest.importorskip("mcp")
        from hyperdata_terminal.mcp_server import build_server

        server = build_server(hub_factory=lambda: None)
        names = {t.name for t in asyncio.run(server.list_tools())}
        assert names == {
            "get_market_overview", "get_asset", "get_liquidations", "get_liquidation_heatmap",
            "get_whale_positions", "get_positions_near_liquidation", "get_order_flow", "get_hlp_vault",
            "get_funding_extremes", "get_data_health",
        }
