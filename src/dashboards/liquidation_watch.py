"""
BTC Liquidation Watch — Terminal Dashboard
Moon Dev style retro hacker aesthetic using rich + pyfiglet.

Displays Hyperliquid BTC positions closest to liquidation in real-time.
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
from config.settings import (  # noqa: E402
    DASHBOARD_REFRESH_RATE,
    LIQ_ZONE_1_PCT,
    LIQ_ZONE_2_PCT,
    LIQ_ZONE_5_PCT,
)


# ── Formatting helpers (imported from central helpers) ────────────────────
from src.utils.helpers import format_usd as fmt_usd, format_price as fmt_price, format_pct_value as fmt_pct


# ── Zone summary with long/short breakdown ────────────────────────────────

def compute_zone_breakdown(positions: list[TrackedPosition]) -> list[dict]:
    """Return zone data split by long/short for the dashboard table.

    Each entry: {label, emoji, style, long_count, long_value, short_count, short_value, total_value}
    """
    zones = [
        {"threshold": LIQ_ZONE_1_PCT, "label": f"< {LIQ_ZONE_1_PCT}%", "emoji": "\U0001f525", "style": "bold red"},
        {"threshold": LIQ_ZONE_2_PCT, "label": f"< {LIQ_ZONE_2_PCT}%", "emoji": "\U0001f525\U0001f525", "style": "bold yellow"},
        {"threshold": LIQ_ZONE_5_PCT, "label": f"< {LIQ_ZONE_5_PCT}%", "emoji": "\U0001f525\U0001f525\U0001f525", "style": "bold bright_yellow"},
    ]

    results: list[dict] = []
    for z in zones:
        lc = lv = sc = sv = 0
        for p in positions:
            if p.distance_pct <= z["threshold"]:
                if p.side == "long":
                    lc += 1
                    lv += p.size_usd
                else:
                    sc += 1
                    sv += p.size_usd
        results.append({
            "label": f'{z["emoji"]} {z["label"]}',
            "style": z["style"],
            "long_count": lc,
            "long_value": lv,
            "short_count": sc,
            "short_value": sv,
            "total_value": lv + sv,
        })
    return results


# ── Mock data generator ───────────────────────────────────────────────────

def generate_mock_positions(n: int = 40) -> list[TrackedPosition]:
    """Generate realistic-looking mock BTC positions for demo mode."""
    btc_price = 83_500.0 + random.uniform(-500, 500)
    positions: list[TrackedPosition] = []
    for _ in range(n):
        side = random.choice(["long", "short"])
        leverage = random.choice([5, 10, 20, 25, 50, 100])
        size_usd = random.uniform(10_000, 8_000_000)

        # Bias entry prices to be near current price
        offset_pct = random.uniform(-3, 3)
        entry_price = btc_price * (1 + offset_pct / 100)

        # Calculate a realistic liquidation price
        mm_rate = 0.03
        if side == "long":
            liq_price = entry_price * (1 - 1 / leverage + mm_rate / leverage)
        else:
            liq_price = entry_price * (1 + 1 / leverage - mm_rate / leverage)

        distance_pct = abs(btc_price - liq_price) / btc_price * 100

        # PnL based on entry vs current
        if side == "long":
            pnl_pct = (btc_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - btc_price) / entry_price
        unrealized_pnl = size_usd * pnl_pct

        positions.append(TrackedPosition(
            address=f"0x{''.join(random.choices('0123456789abcdef', k=40))}",
            symbol="BTC",
            side=side,
            size_usd=size_usd,
            entry_price=entry_price,
            current_price=btc_price,
            liq_price=liq_price,
            distance_pct=distance_pct,
            leverage=leverage,
            unrealized_pnl=unrealized_pnl,
            margin_used=size_usd / leverage,
        ))

    return sorted(positions, key=lambda p: p.distance_pct)


# ── Dashboard ─────────────────────────────────────────────────────────────

class LiquidationWatchDashboard:
    """Rich-powered terminal dashboard for BTC liquidation monitoring."""

    def __init__(
        self,
        scanner: PositionScanner | None = None,
        demo: bool = False,
        refresh_rate: int = DASHBOARD_REFRESH_RATE,
    ) -> None:
        self.console = Console()
        self.scanner = scanner
        self.demo = demo
        self.cycle = 0
        self.refresh_rate = refresh_rate
        self.positions: list[TrackedPosition] = []
        self.btc_price: float = 0.0
        self.last_scan_time: float = 0.0
        self.scan_error: str | None = None

    # ── Header ────────────────────────────────────────────────────

    def build_header(self) -> Text:
        """ASCII art header using pyfiglet."""
        ascii_art = pyfiglet.figlet_format("BTC LIQ WATCH", font="small_slant")
        header = Text()
        for line in ascii_art.splitlines():
            header.append(line + "\n", style="bold bright_cyan")

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        mode_str = "DEMO" if self.demo else "LIVE"
        price_str = fmt_price(self.btc_price) if self.btc_price > 0 else "---"

        info_line = Text()
        info_line.append(f"  Cycle: #{self.cycle}", style="bright_white")
        info_line.append("  |  ", style="dim white")
        info_line.append(f"Refresh: {self.refresh_rate}s", style="bright_white")
        info_line.append("  |  ", style="dim white")
        info_line.append(f"Mode: {mode_str}", style="bold bright_green" if self.demo else "bold bright_red")
        info_line.append("  |  ", style="dim white")
        info_line.append(f"BTC: {price_str}", style="bold bright_yellow")
        info_line.append("  |  ", style="dim white")
        info_line.append(now_str, style="dim bright_white")

        header.append("\n")
        header.append(info_line)
        return header

    # ── Zone table ────────────────────────────────────────────────

    def build_zone_table(self) -> Table:
        """Build the liquidation zones table with long/short breakdown."""
        zones = compute_zone_breakdown(self.positions)

        table = Table(
            title="\U0001f4a5 LIQUIDATION ZONES",
            title_style="bold bright_cyan",
            box=box.HEAVY_EDGE,
            border_style="bright_blue",
            header_style="bold bright_cyan",
            pad_edge=True,
            expand=True,
        )
        table.add_column("ZONE", style="bold white", justify="center", min_width=12)
        table.add_column("LONG #", style="bright_green", justify="right", min_width=7)
        table.add_column("LONG VAL", style="bright_green", justify="right", min_width=10)
        table.add_column("SHORT #", style="bright_red", justify="right", min_width=7)
        table.add_column("SHORT VAL", style="bright_red", justify="right", min_width=10)
        table.add_column("TOTAL VAL", style="bold bright_yellow", justify="right", min_width=10)

        for z in zones:
            table.add_row(
                Text(z["label"], style=z["style"]),
                str(z["long_count"]),
                fmt_usd(z["long_value"]),
                str(z["short_count"]),
                fmt_usd(z["short_value"]),
                fmt_usd(z["total_value"]),
            )

        return table

    # ── Closest positions table ───────────────────────────────────

    def build_closest_positions_table(self, n: int = 3) -> Table:
        """Build the closest-to-liquidation table (N longs + N shorts)."""
        longs = [p for p in self.positions if p.side == "long"][:n]
        shorts = [p for p in self.positions if p.side == "short"][:n]

        table = Table(
            title=f"\U0001f3af {n} CLOSEST LONGS + {n} CLOSEST SHORTS TO LIQUIDATION",
            title_style="bold bright_cyan",
            box=box.HEAVY_EDGE,
            border_style="bright_blue",
            header_style="bold bright_cyan",
            pad_edge=True,
            expand=True,
        )
        table.add_column("SIDE", justify="center", min_width=6)
        table.add_column("SIZE", justify="right", min_width=10)
        table.add_column("ENTRY", justify="right", min_width=12)
        table.add_column("LIQ PRICE", justify="right", min_width=12)
        table.add_column("DISTANCE", justify="right", min_width=9)
        table.add_column("PnL", justify="right", min_width=10)
        table.add_column("LVG", justify="right", min_width=5)

        def _add_row(pos: TrackedPosition) -> None:
            side_style = "bold bright_green" if pos.side == "long" else "bold bright_red"
            side_text = Text(pos.side.upper(), style=side_style)

            # Distance color coding
            if pos.distance_pct < 0.5:
                dist_style = "bold bright_red"
            elif pos.distance_pct < 1.0:
                dist_style = "bold yellow"
            else:
                dist_style = "white"

            # PnL color coding
            if pos.unrealized_pnl >= 0:
                pnl_style = "bright_green"
                pnl_str = f"+{fmt_usd(pos.unrealized_pnl)}"
            else:
                pnl_style = "bright_red"
                pnl_str = fmt_usd(pos.unrealized_pnl)

            table.add_row(
                side_text,
                Text(fmt_usd(pos.size_usd), style="bright_white"),
                Text(fmt_price(pos.entry_price), style="bright_white"),
                Text(fmt_price(pos.liq_price), style="bright_white"),
                Text(fmt_pct(pos.distance_pct), style=dist_style),
                Text(pnl_str, style=pnl_style),
                Text(f"{pos.leverage:.0f}x", style="dim bright_white"),
            )

        for p in longs:
            _add_row(p)

        if longs and shorts:
            table.add_row(
                Text("---", style="dim"), Text("---", style="dim"),
                Text("---", style="dim"), Text("---", style="dim"),
                Text("---", style="dim"), Text("---", style="dim"),
                Text("---", style="dim"),
            )

        for p in shorts:
            _add_row(p)

        if not longs and not shorts:
            table.add_row(
                Text("--", style="dim"), Text("--", style="dim"),
                Text("Scanning...", style="bold yellow"),
                Text("--", style="dim"), Text("--", style="dim"),
                Text("--", style="dim"), Text("--", style="dim"),
            )

        return table

    # ── Stats bar ─────────────────────────────────────────────────

    def build_stats_bar(self) -> Text:
        """One-line summary stats below the tables."""
        total = len(self.positions)
        total_longs = sum(1 for p in self.positions if p.side == "long")
        total_shorts = total - total_longs
        total_value = sum(p.size_usd for p in self.positions)

        bar = Text()
        bar.append("  TRACKED: ", style="dim bright_white")
        bar.append(f"{total}", style="bold bright_white")
        bar.append(" positions", style="dim bright_white")
        bar.append("  |  ", style="dim white")
        bar.append(f"{total_longs} ", style="bright_green")
        bar.append("longs", style="dim bright_white")
        bar.append("  |  ", style="dim white")
        bar.append(f"{total_shorts} ", style="bright_red")
        bar.append("shorts", style="dim bright_white")
        bar.append("  |  ", style="dim white")
        bar.append("Total Value: ", style="dim bright_white")
        bar.append(fmt_usd(total_value), style="bold bright_yellow")

        if self.scan_error:
            bar.append("  |  ", style="dim white")
            bar.append(f"ERR: {self.scan_error}", style="bold red")

        return bar

    # ── Compact build (for combined dashboard) ────────────────────

    def build_compact(self) -> Panel:
        """Compact version for the multi-panel combined view."""
        if not self.positions:
            content = Text("  Scanning positions...", style="dim bright_yellow")
            return Panel(
                content,
                title="[bold bright_cyan]\U0001f525 LIQ WATCH[/]",
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
        table.add_column("SIZE", justify="right", min_width=8)
        table.add_column("DIST", justify="right", min_width=6)
        table.add_column("PnL", justify="right", min_width=8)
        table.add_column("LVG", justify="right", width=4)

        longs = [p for p in self.positions if p.side == "long"][:3]
        shorts = [p for p in self.positions if p.side == "short"][:3]

        for pos in longs + shorts:
            side_style = "bold bright_green" if pos.side == "long" else "bold bright_red"
            pnl_style = "bright_green" if pos.unrealized_pnl >= 0 else "bright_red"
            pnl_sign = "+" if pos.unrealized_pnl >= 0 else ""
            dist_style = "bright_red" if pos.distance_pct < 2 else "white"

            table.add_row(
                Text(pos.side.upper()[:1], style=side_style),
                fmt_usd(pos.size_usd),
                Text(fmt_pct(pos.distance_pct), style=dist_style),
                Text(f"{pnl_sign}{fmt_usd(pos.unrealized_pnl)}", style=pnl_style),
                f"{pos.leverage:.0f}x",
            )

        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        mode = "DEMO" if self.demo else "LIVE"
        price_str = fmt_price(self.btc_price) if self.btc_price > 0 else "---"

        return Panel(
            table,
            title=f"[bold bright_cyan]\U0001f525 BTC LIQ ({mode}) {price_str}[/]",
            subtitle=f"[dim]{now_str}  #{self.cycle}[/]",
            border_style="bright_cyan",
            box=box.ROUNDED,
            padding=(0, 1),
        )

    # ── Full dashboard ────────────────────────────────────────────

    def build_dashboard(self) -> Panel:
        """Compose the full dashboard layout."""
        header = self.build_header()

        if not self.positions:
            # No data yet — show a scanning message
            scanning_text = Text()
            scanning_text.append("\n\n")
            scanning_text.append("    \U0001f50d  Scanning Hyperliquid for BTC positions...\n\n", style="bold bright_yellow")
            scanning_text.append("    Discovering addresses and fetching open positions.\n", style="dim bright_white")
            scanning_text.append("    This may take a moment on the first run.\n\n", style="dim bright_white")

            content = Group(
                Align.center(header),
                Text(""),
                scanning_text,
            )
        else:
            zone_table = self.build_zone_table()
            closest_table = self.build_closest_positions_table(n=3)
            stats_bar = self.build_stats_bar()

            content = Group(
                Align.center(header),
                Text(""),
                zone_table,
                Text(""),
                closest_table,
                Text(""),
                stats_bar,
                Text(""),
            )

        return Panel(
            content,
            title="[bold bright_cyan]\U0001f525 HYPERDATA \u2014 BTC LIQUIDATION WATCH \U0001f525[/]",
            subtitle="[dim bright_white]Ctrl+C to exit[/]",
            border_style="bright_blue",
            box=box.DOUBLE_EDGE,
            padding=(1, 2),
        )

    # ── Data update ───────────────────────────────────────────────

    async def update_data(self) -> None:
        """Fetch new position data from the scanner or generate mock data."""
        if self.demo:
            self.positions = generate_mock_positions(n=40)
            if self.positions:
                self.btc_price = self.positions[0].current_price
            self.last_scan_time = time.time()
            return

        if self.scanner is None:
            return

        try:
            self.scan_error = None
            all_positions = await self.scanner.scan()
            # Filter to BTC only for this dashboard
            btc_positions = [p for p in all_positions if p.symbol == "BTC"]
            self.positions = sorted(btc_positions, key=lambda p: p.distance_pct)
            self.btc_price = self.scanner.market_prices.get("BTC", 0.0)
            self.last_scan_time = time.time()
        except Exception as exc:
            self.scan_error = str(exc)[:60]

    # ── Main loop ─────────────────────────────────────────────────

    async def run(self) -> None:
        """Main loop: scan positions, update display every refresh_rate seconds."""
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
            except KeyboardInterrupt:
                pass


# ── CLI entrypoint ────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="BTC Liquidation Watch — Terminal Dashboard",
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
        default=DASHBOARD_REFRESH_RATE,
        help=f"Refresh interval in seconds (default: {DASHBOARD_REFRESH_RATE})",
    )
    args = parser.parse_args()

    demo = not args.live

    if demo:
        scanner = None
    else:
        scanner = PositionScanner()

    dashboard = LiquidationWatchDashboard(
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
