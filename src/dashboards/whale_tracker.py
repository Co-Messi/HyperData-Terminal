"""
Whale Tracker Dashboard -- Terminal Dashboard
Retro hacker aesthetic using rich + pyfiglet.

Tracks the largest open positions on Hyperliquid, showing size, entry,
unrealized PnL, and distance to liquidation for whale-sized positions.
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

from src.data_layer.position_scanner import PositionScanner, TrackedPosition  # noqa: E402
from config.settings import DASHBOARD_REFRESH_RATE  # noqa: E402


# -- Formatting helpers (imported from central helpers) ---------------------
from src.utils.helpers import format_usd as fmt_usd, format_price as fmt_price, format_pct_value as fmt_pct


def shorten_addr(address: str) -> str:
    """Shorten an Ethereum address to 0xAbCd...EfGh."""
    if len(address) > 12:
        return f"{address[:6]}...{address[-4:]}"
    return address


# -- Mock data generator ----------------------------------------------------

MOCK_SYMBOLS_PRICES: dict[str, tuple[float, float]] = {
    "BTC": (83_000.0, 84_000.0),
    "ETH": (3_400.0, 3_500.0),
    "SOL": (170.0, 185.0),
    "DOGE": (0.155, 0.175),
    "XRP": (0.58, 0.66),
    "AVAX": (35.0, 41.0),
    "LINK": (17.0, 20.0),
    "ARB": (1.20, 1.50),
    "WIF": (2.50, 3.10),
    "SUI": (3.10, 3.80),
}


def generate_mock_whale_positions(n: int = 30) -> list[TrackedPosition]:
    """Generate realistic-looking whale-sized positions."""
    positions: list[TrackedPosition] = []

    for _ in range(n):
        symbol = random.choice(list(MOCK_SYMBOLS_PRICES.keys()))
        price_low, price_high = MOCK_SYMBOLS_PRICES[symbol]
        current_price = random.uniform(price_low, price_high)

        side = random.choice(["long", "short"])
        leverage = random.choice([2, 3, 5, 10, 20, 25, 50])

        # Whale-sized: $100K to $20M
        size_roll = random.random()
        if size_roll < 0.40:
            size_usd = random.uniform(100_000, 500_000)
        elif size_roll < 0.70:
            size_usd = random.uniform(500_000, 2_000_000)
        elif size_roll < 0.90:
            size_usd = random.uniform(2_000_000, 8_000_000)
        else:
            size_usd = random.uniform(8_000_000, 25_000_000)

        # Entry price near current
        offset_pct = random.uniform(-5, 5)
        entry_price = current_price * (1 + offset_pct / 100)

        # Liquidation price
        mm_rate = 0.03
        if side == "long":
            liq_price = entry_price * (1 - 1 / leverage + mm_rate / leverage)
        else:
            liq_price = entry_price * (1 + 1 / leverage - mm_rate / leverage)

        distance_pct = abs(current_price - liq_price) / current_price * 100

        # PnL
        if side == "long":
            pnl_pct = (current_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - current_price) / entry_price
        unrealized_pnl = size_usd * pnl_pct

        positions.append(TrackedPosition(
            address=f"0x{''.join(random.choices('0123456789abcdef', k=40))}",
            symbol=symbol,
            side=side,
            size_usd=size_usd,
            entry_price=entry_price,
            current_price=current_price,
            liq_price=liq_price,
            distance_pct=distance_pct,
            leverage=leverage,
            unrealized_pnl=unrealized_pnl,
            margin_used=size_usd / leverage,
        ))

    # Sort by size descending (whales first)
    return sorted(positions, key=lambda p: p.size_usd, reverse=True)


# -- Dashboard class --------------------------------------------------------

class WhaleTrackerDashboard:
    """Rich-powered terminal dashboard for tracking whale positions."""

    def __init__(
        self,
        scanner: PositionScanner | None = None,
        demo: bool = False,
        refresh_rate: int = 15,
    ) -> None:
        self.console = Console()
        self.scanner = scanner
        self.demo = demo
        self.cycle = 0
        self.refresh_rate = refresh_rate
        self.positions: list[TrackedPosition] = []
        self.last_update: float = 0.0
        self.scan_error: str | None = None

    # -- Header ---------------------------------------------------------------

    def build_header(self) -> Text:
        """ASCII art header using pyfiglet."""
        ascii_art = pyfiglet.figlet_format("WHALES", font="small_slant")
        header = Text()
        for line in ascii_art.splitlines():
            header.append(line + "\n", style="bold bright_cyan")

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        mode_str = "DEMO" if self.demo else "LIVE"
        pos_count = len(self.positions)

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
        info_line.append(f"Positions: {pos_count}", style="bold bright_yellow")
        info_line.append("  |  ", style="dim white")
        info_line.append(now_str, style="dim bright_white")

        header.append("\n")
        header.append(info_line)
        return header

    # -- Whale positions table ------------------------------------------------

    def build_whale_table(self, positions: list[TrackedPosition], limit: int = 20) -> Table:
        """Table of the largest positions on Hyperliquid."""
        table = Table(
            title="\U0001f40b TOP WHALE POSITIONS",
            title_style="bold bright_cyan",
            box=box.HEAVY_EDGE,
            border_style="bright_cyan",
            header_style="bold bright_white",
            pad_edge=True,
            expand=True,
        )
        table.add_column("#", style="dim white", justify="right", width=3)
        table.add_column("SIDE", justify="center", min_width=6)
        table.add_column("SYMBOL", style="bold bright_white", justify="center", min_width=6)
        table.add_column("SIZE", justify="right", min_width=10)
        table.add_column("ENTRY", justify="right", min_width=12)
        table.add_column("PnL", justify="right", min_width=10)
        table.add_column("DIST%", justify="right", min_width=7)
        table.add_column("LVG", justify="right", min_width=5)
        table.add_column("ADDR", style="dim white", justify="center", min_width=14)

        for idx, pos in enumerate(positions[:limit], 1):
            # Side styling
            if pos.side == "long":
                side_text = Text("LONG", style="bold bright_green")
            else:
                side_text = Text("SHORT", style="bold bright_red")

            # PnL styling
            if pos.unrealized_pnl >= 0:
                pnl_style = "bold bright_green"
                pnl_str = f"+{fmt_usd(pos.unrealized_pnl)}"
            else:
                pnl_style = "bold bright_red"
                pnl_str = fmt_usd(pos.unrealized_pnl)

            # Distance styling
            if pos.distance_pct < 2.0:
                dist_style = "bold bright_red"
            elif pos.distance_pct < 5.0:
                dist_style = "bold yellow"
            elif pos.distance_pct < 10.0:
                dist_style = "white"
            else:
                dist_style = "dim white"

            # Size styling -- bigger = brighter
            if pos.size_usd >= 5_000_000:
                size_style = "bold bright_yellow"
            elif pos.size_usd >= 1_000_000:
                size_style = "bold bright_white"
            else:
                size_style = "bright_white"

            table.add_row(
                str(idx),
                side_text,
                pos.symbol,
                Text(fmt_usd(pos.size_usd), style=size_style),
                fmt_price(pos.entry_price),
                Text(pnl_str, style=pnl_style),
                Text(fmt_pct(pos.distance_pct), style=dist_style),
                Text(f"{pos.leverage:.0f}x", style="dim bright_white"),
                shorten_addr(pos.address),
            )

        if not positions:
            table.add_row(
                "--", "--", "--",
                Text("Scanning...", style="bold yellow"),
                "--", "--", "--", "--", "--",
            )

        return table

    # -- Whale summary --------------------------------------------------------

    def build_summary(self) -> Text:
        """Summary stats bar."""
        if not self.positions:
            return Text("  Scanning for whale positions...", style="dim bright_white")

        total_value = sum(p.size_usd for p in self.positions)
        total_pnl = sum(p.unrealized_pnl for p in self.positions)
        longs = [p for p in self.positions if p.side == "long"]
        shorts = [p for p in self.positions if p.side == "short"]
        long_value = sum(p.size_usd for p in longs)
        short_value = sum(p.size_usd for p in shorts)

        bar = Text()
        bar.append("  TOTAL: ", style="dim bright_white")
        bar.append(fmt_usd(total_value), style="bold bright_yellow")
        bar.append("  |  ", style="dim white")
        bar.append("LONG: ", style="dim bright_white")
        bar.append(f"{len(longs)} ({fmt_usd(long_value)})", style="bright_green")
        bar.append("  |  ", style="dim white")
        bar.append("SHORT: ", style="dim bright_white")
        bar.append(f"{len(shorts)} ({fmt_usd(short_value)})", style="bright_red")
        bar.append("  |  ", style="dim white")
        bar.append("NET PnL: ", style="dim bright_white")
        pnl_style = "bold bright_green" if total_pnl >= 0 else "bold bright_red"
        pnl_sign = "+" if total_pnl >= 0 else ""
        bar.append(f"{pnl_sign}{fmt_usd(total_pnl)}", style=pnl_style)

        if self.scan_error:
            bar.append("  |  ", style="dim white")
            bar.append(f"ERR: {self.scan_error}", style="bold red")

        return bar

    # -- Symbol breakdown panel -----------------------------------------------

    def build_symbol_breakdown(self) -> Table:
        """Show position count and value per symbol."""
        if not self.positions:
            return Table()

        # Aggregate by symbol
        by_symbol: dict[str, dict] = {}
        for p in self.positions:
            entry = by_symbol.setdefault(p.symbol, {
                "count": 0, "value": 0.0, "long_count": 0, "short_count": 0,
            })
            entry["count"] += 1
            entry["value"] += p.size_usd
            if p.side == "long":
                entry["long_count"] += 1
            else:
                entry["short_count"] += 1

        sorted_symbols = sorted(by_symbol.items(), key=lambda x: x[1]["value"], reverse=True)

        table = Table(
            title="\U0001f4c8 WHALE EXPOSURE BY SYMBOL",
            title_style="bold bright_cyan",
            box=box.HEAVY_EDGE,
            border_style="bright_cyan",
            header_style="bold bright_white",
            pad_edge=True,
            expand=True,
        )
        table.add_column("SYMBOL", style="bold bright_white", justify="center", min_width=7)
        table.add_column("POSITIONS", style="bright_white", justify="right", min_width=10)
        table.add_column("TOTAL VALUE", style="bold bright_yellow", justify="right", min_width=12)
        table.add_column("LONGS", style="bright_green", justify="right", min_width=6)
        table.add_column("SHORTS", style="bright_red", justify="right", min_width=6)

        for symbol, data in sorted_symbols[:10]:
            table.add_row(
                symbol,
                str(data["count"]),
                fmt_usd(data["value"]),
                str(data["long_count"]),
                str(data["short_count"]),
            )

        return table

    # -- Full dashboard -------------------------------------------------------

    def build_dashboard(self) -> Panel:
        """Compose the full dashboard layout."""
        header = self.build_header()

        if not self.positions:
            scanning = Text()
            scanning.append("\n\n")
            scanning.append(
                "    \U0001f50d  Scanning Hyperliquid for whale positions...\n\n",
                style="bold bright_yellow",
            )
            scanning.append(
                "    Discovering addresses and fetching open positions.\n",
                style="dim bright_white",
            )
            scanning.append(
                "    Looking for positions > $100K in size.\n\n",
                style="dim bright_white",
            )
            content = Group(Align.center(header), Text(""), scanning)
        else:
            whale_table = self.build_whale_table(self.positions, limit=20)
            breakdown = self.build_symbol_breakdown()
            summary = self.build_summary()

            content = Group(
                Align.center(header),
                Text(""),
                whale_table,
                Text(""),
                breakdown,
                Text(""),
                summary,
                Text(""),
            )

        return Panel(
            content,
            title="[bold bright_cyan]\U0001f40b HYPERDATA \u2014 WHALE TRACKER \U0001f40b[/]",
            subtitle="[dim bright_white]Ctrl+C to exit[/]",
            border_style="bright_cyan",
            box=box.DOUBLE_EDGE,
            padding=(1, 2),
        )

    # -- Compact build (for combined dashboard) --------------------------------

    def build_compact(self) -> Panel:
        """Compact version for multi-panel combined view."""
        if not self.positions:
            content = Text("  Scanning for whales...", style="dim bright_yellow")
            return Panel(
                content,
                title="[bold bright_cyan]\U0001f40b WHALES[/]",
                border_style="bright_cyan",
                box=box.ROUNDED,
                padding=(0, 1),
            )

        table = Table(
            box=box.SIMPLE_HEAVY,
            border_style="bright_cyan",
            header_style="bold bright_white",
            expand=True,
            padding=(0, 1),
        )
        table.add_column("SIDE", justify="center", width=5)
        table.add_column("SYM", style="bright_white", justify="center", width=5)
        table.add_column("SIZE", justify="right", min_width=8)
        table.add_column("PnL", justify="right", min_width=8)
        table.add_column("DIST", justify="right", min_width=6)

        for pos in self.positions[:8]:
            side_text = Text(
                pos.side.upper()[:1],
                style="bold bright_green" if pos.side == "long" else "bold bright_red",
            )
            if pos.unrealized_pnl >= 0:
                pnl_str = Text(f"+{fmt_usd(pos.unrealized_pnl)}", style="bright_green")
            else:
                pnl_str = Text(fmt_usd(pos.unrealized_pnl), style="bright_red")

            dist_style = "bright_red" if pos.distance_pct < 5 else "white"

            table.add_row(
                side_text,
                pos.symbol,
                fmt_usd(pos.size_usd),
                pnl_str,
                Text(fmt_pct(pos.distance_pct), style=dist_style),
            )

        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        mode = "DEMO" if self.demo else "LIVE"

        return Panel(
            table,
            title=f"[bold bright_cyan]\U0001f40b WHALES ({mode})[/]",
            subtitle=f"[dim]{now_str}  #{self.cycle}[/]",
            border_style="bright_cyan",
            box=box.ROUNDED,
            padding=(0, 1),
        )

    # -- Data update ----------------------------------------------------------

    async def update_data(self) -> None:
        """Fetch new data or generate mock positions."""
        if self.demo:
            self.positions = generate_mock_whale_positions(n=30)
            self.last_update = time.time()
            return

        if self.scanner is None:
            return

        try:
            self.scan_error = None
            all_positions = await self.scanner.scan()
            # Filter to whale-sized positions (> $100K) and sort by size
            whales = [p for p in all_positions if p.size_usd >= 100_000]
            self.positions = sorted(whales, key=lambda p: p.size_usd, reverse=True)
            self.last_update = time.time()
        except Exception as exc:
            self.scan_error = str(exc)[:60]

    # -- Main loop ------------------------------------------------------------

    async def run(self) -> None:
        """Main loop: refresh and display."""
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
        description="Whale Tracker -- Terminal Dashboard",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Connect to live Hyperliquid API (default: demo mode)",
    )
    parser.add_argument(
        "--refresh",
        type=int,
        default=15,
        help="Refresh interval in seconds (default: 15)",
    )
    args = parser.parse_args()

    demo = not args.live
    scanner = None if demo else PositionScanner()

    dashboard = WhaleTrackerDashboard(
        scanner=scanner,
        demo=demo,
        refresh_rate=args.refresh,
    )

    try:
        asyncio.run(dashboard.run())
    except KeyboardInterrupt:
        Console().print("\n[bold bright_cyan]Dashboard stopped.[/]")


if __name__ == "__main__":
    main()
