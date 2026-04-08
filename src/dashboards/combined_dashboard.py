"""Combined multi-panel dashboard — all panels in a single terminal view."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from src.data_layer.hub import HyperDataHub
from src.dashboards.hub_panels import (
    HubCVD,
    HubHLP,
    HubLiqStream,
    HubLiqWatch,
    HubMarket,
    HubMarketIntel,
    HubSmartMoney,
    HubStatusPanel,
    HubWhales,
)


class CombinedDashboard:
    """All dashboards in a unified layout, powered by one hub."""

    def __init__(self, hub: HyperDataHub, refresh_rate: float = 1.0) -> None:
        self.hub = hub
        self.console = Console()
        self.refresh_rate = refresh_rate
        self.cycle = 0

        self.liq_watch = HubLiqWatch(hub)
        self.liq_stream = HubLiqStream(hub)
        self.cvd = HubCVD(hub)
        self.hlp = HubHLP(hub)
        self.market = HubMarket(hub)
        self.smart_money = HubSmartMoney(hub)
        self.whales = HubWhales(hub)
        self.status_panel = HubStatusPanel(hub)
        self.market_intel = HubMarketIntel(hub)

    def build(self) -> Layout:
        self.cycle += 1
        for panel in [self.liq_watch, self.liq_stream, self.cvd, self.hlp, self.market, self.smart_money, self.whales, self.market_intel]:
            panel.cycle = self.cycle

        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
        )

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        header = Text()
        header.append("  \u26a1 HYPERDATA COMMAND CENTER \u26a1", style="bold bright_cyan")
        header.append("  |  ", style="dim")
        header.append(f"Cycle #{self.cycle}", style="bright_white")
        header.append("  |  ", style="dim")
        mode = self.hub.status.mode.upper()
        header.append(mode, style="bold bright_green" if mode == "DEMO" else "bold bright_red")
        header.append("  |  ", style="dim")
        header.append(f"Liqs: {self.hub.status.total_liquidations:,}", style="bright_yellow")
        header.append("  |  ", style="dim")
        header.append(f"Trades: {self.hub.status.total_trades_processed:,}", style="bright_green")
        header.append("  |  ", style="dim")
        header.append(now_str, style="dim bright_white")

        layout["header"].update(Panel(header, box=box.SIMPLE, border_style="dim"))

        layout["body"].split_row(
            Layout(name="left", ratio=2),
            Layout(name="center", ratio=2),
            Layout(name="right", ratio=2),
        )

        layout["left"].split_column(
            Layout(name="liq_watch", ratio=1),
            Layout(name="liq_stream", ratio=1),
        )
        layout["liq_watch"].update(self.liq_watch.build_compact())
        layout["liq_stream"].update(self.liq_stream.build_compact())

        layout["center"].split_column(
            Layout(name="cvd", ratio=2),
            Layout(name="hlp", ratio=2),
            Layout(name="status", ratio=1),
        )
        layout["cvd"].update(self.cvd.build_compact())
        layout["hlp"].update(self.hlp.build_compact())
        layout["status"].update(self.status_panel.build())

        layout["right"].split_column(
            Layout(name="market", ratio=1),
            Layout(name="market_intel", ratio=1),
            Layout(name="smart_money", ratio=1),
            Layout(name="whales", ratio=1),
        )
        layout["market"].update(self.market.build_compact())
        layout["market_intel"].update(self.market_intel.build_compact())
        layout["smart_money"].update(self.smart_money.build_compact())
        layout["whales"].update(self.whales.build_compact())

        return layout

    async def run(self) -> None:
        with Live(self.build(), console=self.console, refresh_per_second=4, screen=True, vertical_overflow="visible") as live:
            try:
                while True:
                    live.update(self.build())
                    await asyncio.sleep(self.refresh_rate)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
