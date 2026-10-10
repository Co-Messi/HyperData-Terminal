"""Liquidation window totals must say when they do not span the window (H3):
the dashboard panels, the stream dashboard and the MCP tool."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from rich.console import Console

from hyperdata_terminal.data_layer.liquidation_feed import LiquidationEvent, LiquidationFeed


def _render(renderable) -> str:
    console = Console(record=True, width=160, color_system=None)
    console.print(renderable)
    return console.export_text()


def _young_feed(uptime_s: float = 600) -> LiquidationFeed:
    feed = LiquidationFeed(max_events=100)
    feed.started_at = time.time() - uptime_s
    asyncio.run(feed.emit(LiquidationEvent(time.time() - 30, "okx", "BTC", "long", 10_000, 80_000, 0.125)))
    return feed


def test_stream_dashboard_marks_partial_windows():
    from hyperdata_terminal.dashboards.liquidation_stream import LiquidationStreamDashboard

    text = _render(LiquidationStreamDashboard(feed=_young_feed()).build_time_window_table())
    started = time.strftime("%H:%M", time.gmtime(time.time() - 600))
    # The 1h, 4h and 24h rows cover ten minutes; the 10 minute row is complete.
    assert text.count(f"since {started}") >= 3


def test_combined_panel_marks_partial_windows():
    from hyperdata_terminal.dashboards.hub_panels import HubLiqStream

    hub = SimpleNamespace(liquidations=_young_feed(), status=SimpleNamespace(total_liquidations=1))
    text = _render(HubLiqStream(hub).build_compact())
    assert "since" in text


def test_full_windows_carry_no_partial_label():
    from hyperdata_terminal.dashboards.liquidation_stream import LiquidationStreamDashboard

    text = _render(LiquidationStreamDashboard(feed=_young_feed(uptime_s=2 * 86_400)).build_time_window_table())
    assert "since" not in text


def test_mcp_liquidations_reports_coverage_and_warns(tmp_path, monkeypatch):
    from hyperdata_terminal.mcp_server import HubTools

    feed = _young_feed()
    hub = SimpleNamespace(
        liquidations=feed,
        status=SimpleNamespace(started_at=time.time() - 600),
        market=SimpleNamespace(assets={"BTC": object()}),
        positions=SimpleNamespace(positions=[object()]),
        hlp=SimpleNamespace(get_latest_snapshot=lambda: object()),
    )
    out = HubTools(hub).liquidations(minutes=1440)
    assert out["window_coverage"] == pytest.approx(600 / 86_400, rel=0.02)
    assert out["truncated"] is False
    assert any("cover" in w and "1440" in w for w in out["meta"]["warnings"])
