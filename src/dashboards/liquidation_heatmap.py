"""
Liquidation Heatmap Dashboard — text-based visualization of liquidation risk by price level.

Shows where liquidations are concentrated across price levels for BTC (and other assets).
Like Coinglass's liquidation heatmap, but in your terminal.

Usage:
  hyperdata -d heatmap
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass

from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.data_layer.position_scanner import PositionScanner, TrackedPosition

logger = logging.getLogger(__name__)

# Block characters for heatmap intensity (low → high)
BLOCKS = " ░▒▓█"
LONG_STYLE = "bright_red"
SHORT_STYLE = "bright_green"
CURRENT_STYLE = "bold bright_yellow"


@dataclass
class PriceBucket:
    """A price level bucket aggregating liquidation risk."""
    price_low: float
    price_high: float
    mid: float
    long_usd: float = 0.0     # USD of long positions liquidated if price drops here
    short_usd: float = 0.0    # USD of short positions liquidated if price rises here
    long_count: int = 0
    short_count: int = 0

    @property
    def total_usd(self) -> float:
        return self.long_usd + self.short_usd


def compute_heatmap_buckets(
    positions: list[TrackedPosition],
    current_price: float,
    symbol: str = "BTC",
    n_buckets: int = 40,
    range_pct: float = 15.0,
) -> list[PriceBucket]:
    """Compute liquidation heatmap buckets around current price.

    Args:
        positions: All tracked positions.
        current_price: Current market price.
        symbol: Filter to this symbol.
        n_buckets: Number of price level rows.
        range_pct: Percentage above and below current price to show.

    Returns:
        List of PriceBucket sorted from highest to lowest price.
    """
    if current_price <= 0:
        return []

    # Filter to symbol
    sym_positions = [p for p in positions if p.symbol.upper() == symbol.upper()]
    if not sym_positions:
        return []

    # Price range
    price_low = current_price * (1 - range_pct / 100)
    price_high = current_price * (1 + range_pct / 100)
    bucket_size = (price_high - price_low) / n_buckets

    # Create buckets
    buckets = []
    for i in range(n_buckets):
        lo = price_low + i * bucket_size
        hi = lo + bucket_size
        buckets.append(PriceBucket(
            price_low=lo,
            price_high=hi,
            mid=(lo + hi) / 2,
        ))

    # Assign positions to buckets based on their liquidation price
    for pos in sym_positions:
        liq = pos.liq_price
        if liq <= 0 or liq < price_low or liq > price_high:
            continue

        idx = int((liq - price_low) / bucket_size)
        idx = max(0, min(idx, n_buckets - 1))

        if pos.side.lower() == "long":
            buckets[idx].long_usd += pos.size_usd
            buckets[idx].long_count += 1
        else:
            buckets[idx].short_usd += pos.size_usd
            buckets[idx].short_count += 1

    # Return sorted highest price first (top of screen = high prices)
    return list(reversed(buckets))


def _intensity_char(value: float, max_value: float) -> str:
    """Map a value to a block character based on intensity."""
    if max_value <= 0 or value <= 0:
        return BLOCKS[0]
    ratio = min(value / max_value, 1.0)
    idx = int(ratio * (len(BLOCKS) - 1))
    return BLOCKS[idx]


def _format_usd(value: float) -> str:
    """Format USD value compactly."""
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    if value > 0:
        return f"${value:.0f}"
    return ""


class LiquidationHeatmapDashboard:
    """Full-screen liquidation heatmap with live updates."""

    def __init__(
        self,
        scanner: PositionScanner | None = None,
        refresh_rate: int = 5,
        symbol: str = "BTC",
        n_buckets: int = 35,
        range_pct: float = 12.0,
    ):
        self.console = Console()
        self.scanner = scanner
        self.refresh_rate = refresh_rate
        self.symbol = symbol
        self.n_buckets = n_buckets
        self.range_pct = range_pct
        self.positions: list[TrackedPosition] = []
        self.current_price: float = 0.0
        self.cycle = 0

    async def update_data(self) -> None:
        """Fetch latest positions from scanner."""
        if self.scanner:
            self.positions = list(self.scanner.positions)
            self.current_price = self.scanner.market_prices.get(self.symbol, 0.0)

    def build_heatmap(self) -> Panel:
        """Build the text-based heatmap visualization."""
        buckets = compute_heatmap_buckets(
            self.positions, self.current_price,
            symbol=self.symbol, n_buckets=self.n_buckets, range_pct=self.range_pct,
        )

        if not buckets or self.current_price <= 0:
            return Panel(
                Text("Waiting for position data...", style="dim"),
                title="Liquidation Heatmap",
                border_style="bright_cyan",
            )

        # Find max values for intensity scaling
        max_long = max((b.long_usd for b in buckets), default=1)
        max_short = max((b.short_usd for b in buckets), default=1)
        max_val = max(max_long, max_short, 1)

        # Heatmap bar width
        bar_width = 20

        # Build table
        table = Table(
            box=None, show_header=True, header_style="bold bright_white",
            padding=(0, 1), expand=True,
        )
        table.add_column("Price", style="bright_white", justify="right", width=10)
        table.add_column("Longs (liquidated if ↓)", justify="right", width=bar_width + 8)
        table.add_column("", justify="center", width=3)  # marker column
        table.add_column("Shorts (liquidated if ↑)", justify="left", width=bar_width + 8)

        for bucket in buckets:
            price = bucket.mid
            is_current = bucket.price_low <= self.current_price <= bucket.price_high

            # Price label
            price_str = f"${price:,.0f}"
            price_style = CURRENT_STYLE if is_current else "dim"

            # Long bar (right-aligned, red)
            long_chars = ""
            if max_long > 0 and bucket.long_usd > 0:
                n_blocks = max(1, int(bucket.long_usd / max_val * bar_width))
                long_chars = "█" * n_blocks
            long_label = _format_usd(bucket.long_usd)
            long_text = Text()
            if long_label:
                long_text.append(f"{long_label} ", style="dim red")
            long_text.append(long_chars, style=LONG_STYLE)

            # Center marker
            marker = Text("◄►", style=CURRENT_STYLE) if is_current else Text("│", style="dim")

            # Short bar (left-aligned, green)
            short_chars = ""
            if max_short > 0 and bucket.short_usd > 0:
                n_blocks = max(1, int(bucket.short_usd / max_val * bar_width))
                short_chars = "█" * n_blocks
            short_text = Text()
            short_text.append(short_chars, style=SHORT_STYLE)
            short_label = _format_usd(bucket.short_usd)
            if short_label:
                short_text.append(f" {short_label}", style="dim green")

            table.add_row(
                Text(price_str, style=price_style),
                long_text,
                marker,
                short_text,
            )

        # Summary stats
        total_long = sum(b.long_usd for b in buckets)
        total_short = sum(b.short_usd for b in buckets)
        long_count = sum(b.long_count for b in buckets)
        short_count = sum(b.short_count for b in buckets)

        footer = Text()
        footer.append(f"\n  {self.symbol} ", style="bold bright_white")
        footer.append(f"${self.current_price:,.0f}", style=CURRENT_STYLE)
        footer.append(f"  │  ", style="dim")
        footer.append(f"Longs at risk: ", style="dim")
        footer.append(f"{_format_usd(total_long)} ({long_count})", style=LONG_STYLE)
        footer.append(f"  │  ", style="dim")
        footer.append(f"Shorts at risk: ", style="dim")
        footer.append(f"{_format_usd(total_short)} ({short_count})", style=SHORT_STYLE)
        footer.append(f"  │  ", style="dim")
        footer.append(f"Range: ±{self.range_pct:.0f}%", style="dim")

        from rich.console import Group
        content = Group(table, footer)

        return Panel(
            content,
            title=f"[bold bright_cyan]Liquidation Heatmap — {self.symbol}[/]",
            subtitle=f"[dim]Live from Hyperliquid │ Cycle {self.cycle}[/]",
            border_style="bright_cyan",
            box=box.DOUBLE_EDGE,
            padding=(1, 2),
        )

    def build_compact(self) -> Panel:
        """Compact version for combined dashboard."""
        buckets = compute_heatmap_buckets(
            self.positions, self.current_price,
            symbol=self.symbol, n_buckets=15, range_pct=10.0,
        )

        if not buckets or self.current_price <= 0:
            return Panel("Waiting...", title="Liq Heatmap", border_style="bright_cyan")

        max_val = max(max(b.total_usd for b in buckets), 1)
        bar_w = 12

        lines = Text()
        for bucket in buckets:
            is_current = bucket.price_low <= self.current_price <= bucket.price_high
            price_style = CURRENT_STYLE if is_current else "dim"

            lines.append(f"${bucket.mid:>8,.0f} ", style=price_style)

            if bucket.long_usd > 0:
                n = max(1, int(bucket.long_usd / max_val * bar_w))
                lines.append("█" * n, style=LONG_STYLE)
            if bucket.short_usd > 0:
                n = max(1, int(bucket.short_usd / max_val * bar_w))
                lines.append("█" * n, style=SHORT_STYLE)

            if is_current:
                lines.append(" ◄", style=CURRENT_STYLE)
            lines.append("\n")

        return Panel(
            lines,
            title="[bold]Liq Heatmap[/]",
            border_style="bright_cyan",
            box=box.ROUNDED,
        )

    async def run(self) -> None:
        """Main loop — full-screen heatmap with live updates."""
        with Live(
            self.build_heatmap(),
            console=self.console,
            refresh_per_second=2,
            screen=True,
        ) as live:
            try:
                while True:
                    self.cycle += 1
                    await self.update_data()
                    live.update(self.build_heatmap())
                    await asyncio.sleep(self.refresh_rate)
            except KeyboardInterrupt:
                pass
