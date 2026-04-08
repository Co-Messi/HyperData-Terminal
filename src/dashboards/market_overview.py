"""
Market Overview Dashboard -- Terminal Dashboard
Retro hacker aesthetic using rich + pyfiglet.

Shows funding rates, open interest, prices, and extreme funding for all
Hyperliquid assets in real-time.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pyfiglet
from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# Resolve project root so imports work when run as a script
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.data_layer.market_data import AssetInfo, MarketData  # noqa: E402
from config.settings import DASHBOARD_REFRESH_RATE, DEFAULT_SYMBOLS  # noqa: E402


# -- Formatting helpers (imported from central helpers) ---------------------
from src.utils.helpers import format_usd as fmt_usd, format_price as fmt_price, format_pct as fmt_pct


def fmt_funding(value: float) -> str:
    """Format a funding rate (hourly) as a percentage with sign."""
    pct = value * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.4f}%"


def make_bar(magnitude: float, max_magnitude: float, width: int = 20) -> str:
    """Create a visual bar using block chars."""
    if max_magnitude <= 0:
        return ""
    fill = int(min(abs(magnitude) / max_magnitude, 1.0) * width)
    return "\u2588" * fill + "\u2591" * (width - fill)


# -- Mock data generator ----------------------------------------------------

def generate_mock_assets(n: int = 25) -> list[AssetInfo]:
    """Generate realistic-looking mock asset data for demo mode."""
    mock_data = [
        ("BTC", 83_500.0, 500.0),
        ("ETH", 3_450.0, 50.0),
        ("SOL", 178.0, 5.0),
        ("DOGE", 0.165, 0.005),
        ("XRP", 0.62, 0.02),
        ("AVAX", 38.0, 1.5),
        ("LINK", 18.50, 0.5),
        ("ARB", 1.35, 0.1),
        ("WIF", 2.80, 0.15),
        ("PEPE", 0.0000125, 0.000002),
        ("SUI", 3.45, 0.2),
        ("APT", 11.80, 0.5),
        ("OP", 3.20, 0.15),
        ("SEI", 0.85, 0.05),
        ("TIA", 12.50, 0.8),
        ("JUP", 1.15, 0.08),
        ("ONDO", 1.45, 0.1),
        ("RENDER", 8.90, 0.4),
        ("INJ", 28.50, 1.5),
        ("FET", 2.35, 0.15),
        ("NEAR", 7.20, 0.3),
        ("FIL", 6.80, 0.25),
        ("ATOM", 11.50, 0.5),
        ("BONK", 0.000028, 0.000003),
        ("FLOKI", 0.00022, 0.00002),
    ]

    assets: list[AssetInfo] = []
    for symbol, base_price, jitter in mock_data[:n]:
        price = base_price + random.uniform(-jitter, jitter)
        funding = random.uniform(-0.0005, 0.0005)
        # Occasionally spike the funding
        if random.random() < 0.15:
            funding = random.choice([-1, 1]) * random.uniform(0.001, 0.005)

        oi_usd = random.uniform(5_000_000, 2_000_000_000)
        volume = random.uniform(10_000_000, 5_000_000_000)
        change = random.uniform(-0.08, 0.08)

        assets.append(AssetInfo(
            symbol=symbol,
            price=price,
            funding_rate=funding,
            open_interest=oi_usd,
            volume_24h=volume,
            price_change_24h_pct=change,
            mark_price=price * (1 + random.uniform(-0.001, 0.001)),
            index_price=price * (1 + random.uniform(-0.002, 0.002)),
        ))

    return sorted(assets, key=lambda a: a.volume_24h, reverse=True)


# -- Dashboard class --------------------------------------------------------

class MarketOverviewDashboard:
    """Rich-powered terminal dashboard for Hyperliquid market overview."""

    def __init__(
        self,
        market_data: MarketData | None = None,
        demo: bool = False,
        refresh_rate: int = 10,
    ) -> None:
        self.console = Console()
        self.market_data = market_data
        self.demo = demo
        self.cycle = 0
        self.refresh_rate = refresh_rate
        self.assets: list[AssetInfo] = []
        self.last_update: float = 0.0
        self.update_error: str | None = None

    # -- Header ---------------------------------------------------------------

    def build_header(self) -> Text:
        """ASCII art header using pyfiglet."""
        ascii_art = pyfiglet.figlet_format("MARKET", font="small_slant")
        header = Text()
        for line in ascii_art.splitlines():
            header.append(line + "\n", style="bold bright_magenta")

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        mode_str = "DEMO" if self.demo else "LIVE"
        asset_count = len(self.assets)

        info_line = Text()
        info_line.append(f"  Cycle: #{self.cycle}", style="bright_white")
        info_line.append("  |  ", style="dim white")
        info_line.append(f"Refresh: {self.refresh_rate}s", style="bright_white")
        info_line.append("  |  ", style="dim white")
        info_line.append(
            f"Mode: {mode_str}",
            style="bold bright_green" if self.demo else "bold bright_red",
        )
        info_line.append("  |  ", style="dim white")
        info_line.append(f"Assets: {asset_count}", style="bold bright_yellow")
        info_line.append("  |  ", style="dim white")
        info_line.append(now_str, style="dim bright_white")

        header.append("\n")
        header.append(info_line)
        return header

    # -- Top assets table -----------------------------------------------------

    def build_assets_table(self, assets: list[AssetInfo], limit: int = 20) -> Table:
        """Top N assets by volume. Columns: symbol, price, 24h change, funding, OI."""
        table = Table(
            title="\U0001f4ca TOP ASSETS BY VOLUME",
            title_style="bold bright_magenta",
            box=box.HEAVY_EDGE,
            border_style="bright_magenta",
            header_style="bold bright_white",
            pad_edge=True,
            expand=True,
        )
        table.add_column("#", style="dim white", justify="right", width=3)
        table.add_column("SYMBOL", style="bold bright_white", justify="center", min_width=7)
        table.add_column("PRICE", style="bright_white", justify="right", min_width=12)
        table.add_column("24H CHG", justify="right", min_width=9)
        table.add_column("FUNDING", justify="right", min_width=10)
        table.add_column("OI", style="bright_white", justify="right", min_width=10)
        table.add_column("24H VOL", style="bright_white", justify="right", min_width=10)

        for idx, asset in enumerate(assets[:limit], 1):
            # Price change coloring
            if asset.price_change_24h_pct >= 0:
                chg_style = "bold bright_green"
            else:
                chg_style = "bold bright_red"

            # Funding rate coloring (positive = longs pay, green; negative = shorts pay, red)
            if asset.funding_rate >= 0:
                fund_style = "bright_green"
            else:
                fund_style = "bright_red"

            table.add_row(
                str(idx),
                asset.symbol,
                fmt_price(asset.price),
                Text(fmt_pct(asset.price_change_24h_pct), style=chg_style),
                Text(fmt_funding(asset.funding_rate), style=fund_style),
                fmt_usd(asset.open_interest),
                fmt_usd(asset.volume_24h),
            )

        if not assets:
            table.add_row(
                "--", "--",
                Text("Fetching...", style="bold yellow"),
                "--", "--", "--", "--",
            )

        return table

    # -- Extreme funding table ------------------------------------------------

    def build_extreme_funding(self, assets: list[AssetInfo], limit: int = 10) -> Table:
        """Assets with extreme funding rates. Show annualized rate with visual bar."""
        # Sort by absolute funding rate descending
        by_funding = sorted(assets, key=lambda a: abs(a.funding_rate), reverse=True)
        # Filter to meaningful funding (annualized > ~10%)
        extreme = [
            a for a in by_funding
            if abs(a.funding_rate) * 8760 >= 0.10
        ][:limit]

        table = Table(
            title="\U0001f525 EXTREME FUNDING RATES",
            title_style="bold bright_magenta",
            box=box.HEAVY_EDGE,
            border_style="bright_magenta",
            header_style="bold bright_white",
            pad_edge=True,
            expand=True,
        )
        table.add_column("SYMBOL", style="bold bright_white", justify="center", min_width=7)
        table.add_column("HOURLY", justify="right", min_width=11)
        table.add_column("ANNUALIZED", justify="right", min_width=12)
        table.add_column("VISUAL", justify="left", min_width=22)

        if not extreme:
            table.add_row(
                "--",
                Text("No extreme rates", style="dim white"),
                "--", "--",
            )
            return table

        max_annualized = max(abs(a.funding_rate) * 8760 for a in extreme) if extreme else 1.0

        for asset in extreme:
            annualized = asset.funding_rate * 8760
            abs_ann = abs(annualized)

            if asset.funding_rate >= 0:
                rate_style = "bold bright_green"
                bar_char = "\U0001f7e9"  # green square
            else:
                rate_style = "bold bright_red"
                bar_char = "\U0001f7e5"  # red square

            bar = make_bar(abs_ann, max_annualized, width=20)

            table.add_row(
                asset.symbol,
                Text(fmt_funding(asset.funding_rate), style=rate_style),
                Text(fmt_pct(annualized, include_sign=True), style=rate_style),
                Text(bar, style=rate_style),
            )

        return table

    # -- Stats bar ------------------------------------------------------------

    def build_stats_bar(self) -> Text:
        """One-line summary of market stats."""
        if not self.assets:
            return Text("  Loading market data...", style="dim bright_white")

        total_oi = sum(a.open_interest for a in self.assets)
        total_vol = sum(a.volume_24h for a in self.assets)
        avg_funding = (
            sum(a.funding_rate for a in self.assets) / len(self.assets)
            if self.assets else 0.0
        )
        positive_count = sum(1 for a in self.assets if a.price_change_24h_pct >= 0)
        negative_count = len(self.assets) - positive_count

        bar = Text()
        bar.append("  TOTAL OI: ", style="dim bright_white")
        bar.append(fmt_usd(total_oi), style="bold bright_yellow")
        bar.append("  |  ", style="dim white")
        bar.append("24H VOL: ", style="dim bright_white")
        bar.append(fmt_usd(total_vol), style="bold bright_yellow")
        bar.append("  |  ", style="dim white")
        bar.append("AVG FUNDING: ", style="dim bright_white")
        avg_fund_style = "bright_green" if avg_funding >= 0 else "bright_red"
        bar.append(fmt_funding(avg_funding), style=avg_fund_style)
        bar.append("  |  ", style="dim white")
        bar.append(f"{positive_count} ", style="bright_green")
        bar.append("green", style="dim bright_white")
        bar.append("  ", style="dim white")
        bar.append(f"{negative_count} ", style="bright_red")
        bar.append("red", style="dim bright_white")

        if self.update_error:
            bar.append("  |  ", style="dim white")
            bar.append(f"ERR: {self.update_error}", style="bold red")

        return bar

    # -- Full dashboard -------------------------------------------------------

    def build_dashboard(self) -> Panel:
        """Compose the full dashboard layout."""
        header = self.build_header()

        if not self.assets:
            scanning = Text()
            scanning.append("\n\n")
            scanning.append(
                "    \U0001f50d  Fetching market data from Hyperliquid...\n\n",
                style="bold bright_yellow",
            )
            scanning.append(
                "    Loading prices, funding rates, and open interest.\n",
                style="dim bright_white",
            )
            scanning.append(
                "    This may take a moment on the first run.\n\n",
                style="dim bright_white",
            )
            content = Group(Align.center(header), Text(""), scanning)
        else:
            assets_table = self.build_assets_table(self.assets, limit=20)
            extreme_table = self.build_extreme_funding(self.assets, limit=10)
            stats = self.build_stats_bar()

            content = Group(
                Align.center(header),
                Text(""),
                assets_table,
                Text(""),
                extreme_table,
                Text(""),
                stats,
                Text(""),
            )

        return Panel(
            content,
            title="[bold bright_magenta]\U0001f4ca HYPERDATA \u2014 MARKET OVERVIEW \U0001f4ca[/]",
            subtitle="[dim bright_white]Ctrl+C to exit[/]",
            border_style="bright_magenta",
            box=box.DOUBLE_EDGE,
            padding=(1, 2),
        )

    # -- Compact build (for combined dashboard) --------------------------------

    def build_compact(self) -> Panel:
        """Compact version for the multi-panel combined view."""
        if not self.assets:
            content = Text("  Loading market data...", style="dim bright_yellow")
            return Panel(
                content,
                title="[bold bright_magenta]\U0001f4ca MARKET[/]",
                border_style="bright_magenta",
                box=box.ROUNDED,
                padding=(0, 1),
            )

        # Compact table: top 8 assets, fewer columns
        table = Table(
            box=box.SIMPLE_HEAVY,
            border_style="bright_magenta",
            header_style="bold bright_white",
            expand=True,
            padding=(0, 1),
        )
        table.add_column("SYM", style="bold bright_white", justify="center", width=5)
        table.add_column("PRICE", justify="right", min_width=10)
        table.add_column("CHG", justify="right", min_width=8)
        table.add_column("FUND", justify="right", min_width=9)
        table.add_column("OI", justify="right", min_width=8)

        for asset in self.assets[:8]:
            chg_style = "bright_green" if asset.price_change_24h_pct >= 0 else "bright_red"
            fund_style = "bright_green" if asset.funding_rate >= 0 else "bright_red"

            table.add_row(
                asset.symbol,
                fmt_price(asset.price),
                Text(fmt_pct(asset.price_change_24h_pct), style=chg_style),
                Text(fmt_funding(asset.funding_rate), style=fund_style),
                fmt_usd(asset.open_interest),
            )

        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        mode = "DEMO" if self.demo else "LIVE"

        return Panel(
            table,
            title=f"[bold bright_magenta]\U0001f4ca MARKET ({mode})[/]",
            subtitle=f"[dim]{now_str}  #{self.cycle}[/]",
            border_style="bright_magenta",
            box=box.ROUNDED,
            padding=(0, 1),
        )

    # -- Data update ----------------------------------------------------------

    async def update_data(self) -> None:
        """Fetch new data or generate mock data."""
        if self.demo:
            self.assets = generate_mock_assets(n=25)
            self.last_update = time.time()
            return

        if self.market_data is None:
            return

        try:
            self.update_error = None
            self.assets = await self.market_data.get_all()
            self.last_update = time.time()
        except Exception as exc:
            self.update_error = str(exc)[:60]

    # -- Main loop ------------------------------------------------------------

    async def run(self) -> None:
        """Main loop: refresh market data and update display."""
        self.console.clear()
        with Live(
            self.build_dashboard(),
            console=self.console,
            refresh_per_second=2,
            screen=True,
        ) as live:
            try:
                while True:
                    self.cycle += 1
                    await self.update_data()
                    live.update(self.build_dashboard())
                    await asyncio.sleep(self.refresh_rate)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass


# -- CLI entrypoint ---------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Market Overview -- Terminal Dashboard",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Connect to live Hyperliquid API (default: demo mode with mock data)",
    )
    parser.add_argument(
        "--refresh",
        type=int,
        default=10,
        help="Refresh interval in seconds (default: 10)",
    )
    args = parser.parse_args()

    demo = not args.live
    market_data = None if demo else MarketData()

    dashboard = MarketOverviewDashboard(
        market_data=market_data,
        demo=demo,
        refresh_rate=args.refresh,
    )

    try:
        asyncio.run(dashboard.run())
    except KeyboardInterrupt:
        Console().print("\n[bold bright_magenta]Dashboard stopped.[/]")


if __name__ == "__main__":
    main()
