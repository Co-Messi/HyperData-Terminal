"""Multi-Exchange Liquidation Stream Dashboard.

Real-time terminal dashboard showing liquidation totals across all exchanges
using the rich library. Styled after Moon Dev's top-right panel.

Usage:
    python -m src.dashboards.liquidation_stream          # demo mode (mock data)
    python -m src.dashboards.liquidation_stream --live    # live WebSocket feeds
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyfiglet
from rich.align import Align
from rich.box import DOUBLE_EDGE, HEAVY, SIMPLE_HEAVY
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# Ensure project root is importable when running as a script
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.data_layer.liquidation_feed import LiquidationEvent, LiquidationFeed

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & colour map
# ---------------------------------------------------------------------------
EXCHANGE_COLORS: dict[str, str] = {
    "hyperliquid": "magenta",
    "binance": "yellow",
    "bybit": "green",
    "okx": "blue",
}

EXCHANGE_ICONS: dict[str, str] = {
    "hyperliquid": "\U0001f7e3",  # purple circle
    "binance": "\U0001f7e1",      # yellow circle
    "bybit": "\U0001f7e2",        # green circle
    "okx": "\U0001f535",          # blue circle
}

SIDE_LONG = "\U0001f7e2"   # green circle
SIDE_SHORT = "\U0001f534"  # red circle

TIME_WINDOWS: list[tuple[str, int]] = [
    ("10 MIN", 10),
    ("1 HOUR", 60),
    ("4 HOUR", 240),
    ("24 HOUR", 1440),
]


# ---------------------------------------------------------------------------
# Formatting helpers (imported from central helpers)
# ---------------------------------------------------------------------------
from src.utils.helpers import format_usd as fmt_usd


def fmt_number(value: int) -> str:
    """Format an integer with comma separators."""
    return f"{value:,}"


# ---------------------------------------------------------------------------
# Dashboard class
# ---------------------------------------------------------------------------

class LiquidationStreamDashboard:
    """Terminal dashboard for multi-exchange liquidation monitoring."""

    def __init__(self, feed: LiquidationFeed | None = None, demo: bool = False, refresh_rate: int = 5) -> None:
        self.console = Console()
        self.feed = feed or LiquidationFeed()
        self.demo = demo
        self.cycle: int = 0
        self.refresh_rate: int = refresh_rate

    # ----- Data update -----

    async def update_data(self) -> None:
        """Generate mock events when in demo mode; no-op in live mode."""
        if not self.demo:
            return

        exchanges = ["binance", "bybit", "okx", "hyperliquid"]
        exchange_weights = [0.30, 0.35, 0.20, 0.15]
        symbols_prices: dict[str, tuple[float, float]] = {
            "BTC": (60_000.0, 75_000.0),
            "ETH": (2_800.0, 4_200.0),
            "SOL": (100.0, 250.0),
            "DOGE": (0.08, 0.25),
            "XRP": (0.45, 1.20),
        }
        symbols = list(symbols_prices.keys())

        for _ in range(random.randint(3, 8)):
            exchange = random.choices(exchanges, weights=exchange_weights, k=1)[0]
            symbol = random.choice(symbols)
            price_low, price_high = symbols_prices[symbol]
            price = random.uniform(price_low, price_high)

            size_roll = random.random()
            if size_roll < 0.60:
                size_usd = random.uniform(500, 10_000)
            elif size_roll < 0.85:
                size_usd = random.uniform(10_000, 100_000)
            elif size_roll < 0.97:
                size_usd = random.uniform(100_000, 500_000)
            else:
                size_usd = random.uniform(500_000, 2_000_000)

            quantity = size_usd / price if price > 0 else 0.0

            event = LiquidationEvent(
                timestamp=time.time(),
                exchange=exchange,
                symbol=symbol,
                side=random.choice(["long", "short"]),
                size_usd=size_usd,
                price=price,
                quantity=quantity,
            )
            await self.feed.emit(event)

    # ----- Header -----

    def build_header(self) -> Text:
        """Render an ASCII-art 'LIQ STREAM' header via pyfiglet."""
        ascii_art = pyfiglet.figlet_format("LIQ STREAM", font="small")
        header = Text(ascii_art, style="bright_cyan bold")
        return header

    # ----- Time-window table -----

    def build_time_window_table(self) -> Table:
        """Table showing liquidation stats for 10min / 1hr / 4hr / 24hr windows."""
        table = Table(
            title="TIME WINDOWS",
            title_style="bright_cyan bold",
            box=HEAVY,
            border_style="bright_cyan",
            header_style="bold bright_white",
            expand=True,
            padding=(0, 1),
        )

        table.add_column("WINDOW", style="bold white", justify="center", min_width=10)
        table.add_column("LIQS LONG", style="green", justify="right", min_width=8)
        table.add_column("VOL LONG", style="green", justify="right", min_width=10)
        table.add_column("LIQS SHORT", style="red", justify="right", min_width=8)
        table.add_column("VOL SHORT", style="red", justify="right", min_width=10)

        for label, minutes in TIME_WINDOWS:
            stats = self.feed.get_stats(window_minutes=minutes)
            table.add_row(
                label,
                fmt_number(stats["long_count"]),
                fmt_usd(stats["long_volume_usd"]),
                fmt_number(stats["short_count"]),
                fmt_usd(stats["short_volume_usd"]),
            )

        return table

    # ----- Totals panel -----

    def build_totals_panel(self) -> Panel:
        """Summary panel: total counts, long/short split, per-exchange breakdown."""
        stats = self.feed.get_stats(window_minutes=60)

        lines: list[Text] = []

        # Overall totals
        total_line = Text()
        total_line.append("Total Liquidations: ", style="bold white")
        total_line.append(fmt_number(stats["total_count"]), style="bright_white bold")
        lines.append(total_line)

        vol_line = Text()
        vol_line.append("Total Volume: ", style="bold white")
        vol_line.append(fmt_usd(stats["total_volume_usd"]), style="bright_white bold")
        lines.append(vol_line)

        lines.append(Text())  # spacer

        # Long / Short split
        long_line = Text()
        long_line.append(f"{SIDE_LONG} Long Liquidations: ", style="green")
        long_line.append(
            f"{fmt_number(stats['long_count'])} ({fmt_usd(stats['long_volume_usd'])})",
            style="bold green",
        )
        lines.append(long_line)

        short_line = Text()
        short_line.append(f"{SIDE_SHORT} Short Liquidations: ", style="red")
        short_line.append(
            f"{fmt_number(stats['short_count'])} ({fmt_usd(stats['short_volume_usd'])})",
            style="bold red",
        )
        lines.append(short_line)

        lines.append(Text())  # spacer

        # Per-exchange breakdown
        ex_header = Text("By Exchange:", style="bold bright_cyan underline")
        lines.append(ex_header)

        all_exchanges = ["hyperliquid", "binance", "bybit", "okx"]
        by_exchange: dict[str, dict[str, Any]] = stats.get("by_exchange", {})

        for ex_name in all_exchanges:
            ex_data = by_exchange.get(ex_name, {"count": 0, "volume_usd": 0.0})
            color = EXCHANGE_COLORS.get(ex_name, "white")
            icon = EXCHANGE_ICONS.get(ex_name, "")
            ex_line = Text()
            ex_line.append(f"  {icon} ", style=color)
            ex_line.append(f"{ex_name.capitalize():<15}", style=f"bold {color}")
            ex_line.append(f"{fmt_number(ex_data['count']):>6} liqs", style="white")
            ex_line.append("    ", style="white")
            ex_line.append(fmt_usd(ex_data["volume_usd"]), style=f"bold {color}")
            lines.append(ex_line)

        return Panel(
            Group(*lines),
            title="[bright_cyan bold]TOTALS (1 HOUR)[/bright_cyan bold]",
            border_style="bright_cyan",
            box=HEAVY,
            expand=True,
        )

    # ----- Recent feed -----

    def build_recent_feed(self, limit: int = 15) -> Table:
        """Live feed table of the most recent liquidation events."""
        table = Table(
            title="RECENT LIQUIDATIONS (Live Feed)",
            title_style="bright_cyan bold",
            box=HEAVY,
            border_style="bright_cyan",
            header_style="bold bright_white",
            expand=True,
            padding=(0, 1),
        )

        table.add_column("TIME", style="dim white", justify="center", min_width=8)
        table.add_column("EXCHANGE", justify="center", min_width=12)
        table.add_column("SYMBOL", style="bright_white bold", justify="center", min_width=6)
        table.add_column("SIDE", justify="center", min_width=10)
        table.add_column("SIZE", justify="right", min_width=12)

        recent = self.feed.get_recent(minutes=60)[:limit]

        if not recent:
            table.add_row(
                "--:--:--",
                "---",
                "---",
                "---",
                "---",
            )
        else:
            for ev in recent:
                ts_str = datetime.fromtimestamp(ev.timestamp, tz=timezone.utc).strftime("%H:%M:%S")
                ex_color = EXCHANGE_COLORS.get(ev.exchange, "white")
                exchange_text = Text(ev.exchange.capitalize(), style=f"bold {ex_color}")

                if ev.side == "long":
                    side_text = Text(f"{SIDE_LONG} LONG", style="bold green")
                else:
                    side_text = Text(f"{SIDE_SHORT} SHORT", style="bold red")

                size_text = Text(fmt_usd(ev.size_usd), style="bold bright_white")

                table.add_row(ts_str, exchange_text, ev.symbol, side_text, size_text)

        return table

    # ----- Compact build (for combined dashboard) -----

    def build_compact(self) -> Panel:
        """Compact version for the multi-panel combined view."""
        # Compact time-window summary
        table = Table(
            box=SIMPLE_HEAVY,
            border_style="bright_cyan",
            header_style="bold bright_white",
            expand=True,
            padding=(0, 1),
        )
        table.add_column("WINDOW", style="bold white", justify="center", min_width=8)
        table.add_column("LONG", style="green", justify="right", min_width=8)
        table.add_column("SHORT", style="red", justify="right", min_width=8)

        for label, minutes in TIME_WINDOWS[:3]:  # 10min, 1hr, 4hr
            stats = self.feed.get_stats(window_minutes=minutes)
            table.add_row(
                label,
                fmt_usd(stats["long_volume_usd"]),
                fmt_usd(stats["short_volume_usd"]),
            )

        # Add last 3 recent events
        recent = self.feed.get_recent(minutes=60)[:3]
        recent_lines = Text()
        if recent:
            for ev in recent:
                ts_str = datetime.fromtimestamp(ev.timestamp, tz=timezone.utc).strftime("%H:%M:%S")
                side_icon = SIDE_LONG if ev.side == "long" else SIDE_SHORT
                side_style = "green" if ev.side == "long" else "red"
                recent_lines.append(f" {ts_str} ", style="dim")
                recent_lines.append(f"{side_icon} ", style=side_style)
                recent_lines.append(f"{ev.symbol} ", style="bright_white")
                recent_lines.append(f"{fmt_usd(ev.size_usd)}\n", style="bold white")

        from rich.console import Group as _Group
        content = _Group(table, Text(""), recent_lines) if recent else table

        now_str = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")

        return Panel(
            content,
            title="[bold bright_cyan]\U0001f4a5 LIQ STREAM[/]",
            subtitle=f"[dim]{now_str}  #{self.cycle}[/]",
            border_style="bright_cyan",
            box=DOUBLE_EDGE,
            padding=(0, 1),
        )

    # ----- Full dashboard composition -----

    def build_dashboard(self) -> Panel:
        """Compose the full dashboard layout."""
        now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        subtitle = f"Cycle #{self.cycle}  |  {now_str}"

        header = self.build_header()
        title_text = Text(
            "  BTC LIQUIDATION TOTALS  ALL EXCHANGES  ",
            style="bold bright_cyan on grey11",
        )

        time_table = self.build_time_window_table()
        totals = self.build_totals_panel()
        recent = self.build_recent_feed(limit=15)

        content = Group(
            Align.center(header),
            Align.center(title_text),
            Text(),
            time_table,
            Text(),
            totals,
            Text(),
            recent,
        )

        return Panel(
            content,
            title="[bold bright_cyan]LIQ STREAM[/bold bright_cyan]",
            subtitle=f"[dim]{subtitle}[/dim]",
            border_style="bright_cyan",
            box=DOUBLE_EDGE,
            expand=True,
            padding=(1, 2),
        )

    # ----- Main loop -----

    async def run(self) -> None:
        """Main async loop: refresh the dashboard on a timer."""
        with Live(
            self.build_dashboard(),
            console=self.console,
            refresh_per_second=4,
            screen=True,
        ) as live:
            try:
                while True:
                    self.cycle += 1
                    live.update(self.build_dashboard())
                    await asyncio.sleep(self.refresh_rate)
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# Mock data generator (demo mode)
# ---------------------------------------------------------------------------

async def generate_mock_events(feed: LiquidationFeed) -> None:
    """Generate realistic-looking mock liquidation events for demo mode."""
    exchanges = ["binance", "bybit", "okx", "hyperliquid"]
    symbols_prices: dict[str, tuple[float, float]] = {
        "BTC": (60_000.0, 75_000.0),
        "ETH": (2_800.0, 4_200.0),
        "SOL": (100.0, 250.0),
        "DOGE": (0.08, 0.25),
        "XRP": (0.45, 1.20),
        "SUI": (1.00, 5.00),
        "ARB": (0.50, 2.50),
        "AVAX": (20.0, 60.0),
        "LINK": (10.0, 25.0),
        "PEPE": (0.000005, 0.00003),
    }
    symbols = list(symbols_prices.keys())

    # Weight exchanges so Binance/Bybit get more events (realistic)
    exchange_weights = [0.30, 0.35, 0.20, 0.15]  # binance, bybit, okx, hyperliquid

    while True:
        exchange = random.choices(exchanges, weights=exchange_weights, k=1)[0]
        symbol = random.choice(symbols)
        price_low, price_high = symbols_prices[symbol]
        price = random.uniform(price_low, price_high)

        # Skew sizes: lots of small liqs, occasional whale
        size_roll = random.random()
        if size_roll < 0.60:
            size_usd = random.uniform(500, 10_000)
        elif size_roll < 0.85:
            size_usd = random.uniform(10_000, 100_000)
        elif size_roll < 0.97:
            size_usd = random.uniform(100_000, 500_000)
        else:
            size_usd = random.uniform(500_000, 2_000_000)

        quantity = size_usd / price if price > 0 else 0.0

        event = LiquidationEvent(
            timestamp=time.time(),
            exchange=exchange,
            symbol=symbol,
            side=random.choice(["long", "short"]),
            size_usd=size_usd,
            price=price,
            quantity=quantity,
        )
        await feed.emit(event)
        await asyncio.sleep(random.uniform(0.3, 2.5))


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

async def _main_async(live: bool = False) -> None:
    """Async entry-point for both demo and live modes."""
    feed = LiquidationFeed()
    dashboard = LiquidationStreamDashboard(feed=feed, refresh_rate=5)

    tasks: list[asyncio.Task[Any]] = []

    if live:
        logger.info("Starting LIVE liquidation feeds...")
        await feed.start()
    else:
        logger.info("Starting DEMO mode with mock events...")
        tasks.append(asyncio.create_task(generate_mock_events(feed), name="mock-generator"))

    tasks.append(asyncio.create_task(dashboard.run(), name="dashboard"))

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for t in tasks:
            t.cancel()
        if live:
            await feed.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-Exchange Liquidation Stream Dashboard")
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Connect to live WebSocket feeds (default: demo mode with mock data)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        asyncio.run(_main_async(live=args.live))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
