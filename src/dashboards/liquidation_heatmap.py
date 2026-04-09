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
    """Full-screen multi-asset liquidation heatmap with live updates.

    Displays heatmaps for all assets with tracked positions.
    Auto-discovers assets from live data — not hardcoded to BTC.
    """

    # Assets to always show (even with 0 positions) if price exists
    PRIORITY_ASSETS = ["BTC", "ETH", "SOL"]

    def __init__(
        self,
        scanner: PositionScanner | None = None,
        refresh_rate: int = 5,
        symbol: str | None = None,
        n_buckets: int = 30,
        range_pct: float = 12.0,
    ):
        self.console = Console()
        self.scanner = scanner
        self.refresh_rate = refresh_rate
        self.single_symbol = symbol  # None = show all assets
        self.n_buckets = n_buckets
        self.range_pct = range_pct
        self.positions: list[TrackedPosition] = []
        self.market_prices: dict[str, float] = {}
        self.cycle = 0

    def _discover_assets(self) -> list[str]:
        """Find all assets that have tracked positions, plus priority assets."""
        assets_with_positions = set()
        for p in self.positions:
            assets_with_positions.add(p.symbol.upper())

        # Priority assets first, then others sorted by position count
        result = []
        for a in self.PRIORITY_ASSETS:
            if a in assets_with_positions or self.market_prices.get(a, 0) > 0:
                result.append(a)
                assets_with_positions.discard(a)
        for a in sorted(assets_with_positions):
            result.append(a)
        return result

    async def update_data(self) -> None:
        """Fetch latest positions from scanner."""
        if self.scanner:
            self.positions = list(self.scanner.positions)
            self.market_prices = dict(self.scanner.market_prices)

    def _build_asset_heatmap(self, symbol: str, current_price: float, n_buckets: int, bar_width: int = 18) -> Table | None:
        """Build a heatmap table for a single asset."""
        buckets = compute_heatmap_buckets(
            self.positions, current_price,
            symbol=symbol, n_buckets=n_buckets, range_pct=self.range_pct,
        )
        if not buckets or current_price <= 0:
            return None

        max_long = max((b.long_usd for b in buckets), default=1)
        max_short = max((b.short_usd for b in buckets), default=1)
        max_val = max(max_long, max_short, 1)

        total_long = sum(b.long_usd for b in buckets)
        total_short = sum(b.short_usd for b in buckets)
        long_count = sum(b.long_count for b in buckets)
        short_count = sum(b.short_count for b in buckets)

        # Header with asset name and stats
        header = Text()
        header.append(f"  {symbol} ", style="bold bright_white")
        header.append(f"${current_price:,.0f}", style=CURRENT_STYLE)
        header.append(f"  │  ", style="dim")
        header.append(f"L: {_format_usd(total_long)}({long_count})", style=LONG_STYLE)
        header.append(f"  ", style="dim")
        header.append(f"S: {_format_usd(total_short)}({short_count})", style=SHORT_STYLE)

        table = Table(
            box=None, show_header=False, padding=(0, 1), expand=True,
            title=header, title_style="",
        )
        table.add_column("Price", justify="right", width=10)
        table.add_column("Longs ↓", justify="right", width=bar_width + 6)
        table.add_column("", justify="center", width=2)
        table.add_column("Shorts ↑", justify="left", width=bar_width + 6)

        for bucket in buckets:
            is_current = bucket.price_low <= current_price <= bucket.price_high
            price_style = CURRENT_STYLE if is_current else "dim"

            # Price — use appropriate formatting
            if current_price > 1000:
                price_str = f"${bucket.mid:,.0f}"
            elif current_price > 10:
                price_str = f"${bucket.mid:,.1f}"
            else:
                price_str = f"${bucket.mid:.3f}"

            # Long bar
            long_text = Text()
            if bucket.long_usd > 0:
                n = max(1, int(bucket.long_usd / max_val * bar_width))
                label = _format_usd(bucket.long_usd)
                if label:
                    long_text.append(f"{label} ", style="dim red")
                long_text.append("█" * n, style=LONG_STYLE)

            marker = Text("◄►", style=CURRENT_STYLE) if is_current else Text("│", style="dim")

            # Short bar
            short_text = Text()
            if bucket.short_usd > 0:
                n = max(1, int(bucket.short_usd / max_val * bar_width))
                short_text.append("█" * n, style=SHORT_STYLE)
                label = _format_usd(bucket.short_usd)
                if label:
                    short_text.append(f" {label}", style="dim green")

            table.add_row(Text(price_str, style=price_style), long_text, marker, short_text)

        return table

    def build_heatmap(self) -> Panel:
        """Build multi-asset heatmap visualization."""
        from rich.console import Group

        assets = [self.single_symbol] if self.single_symbol else self._discover_assets()

        if not assets:
            return Panel(
                Text("Waiting for position data...", style="dim"),
                title="Liquidation Heatmap",
                border_style="bright_cyan",
            )

        sections: list = []
        for symbol in assets[:6]:  # Max 6 assets to fit terminal
            price = self.market_prices.get(symbol, 0.0)
            if price <= 0:
                continue

            # Fewer buckets per asset when showing multiple
            n = self.n_buckets if self.single_symbol else max(10, self.n_buckets // len(assets))
            table = self._build_asset_heatmap(symbol, price, n_buckets=n)
            if table:
                sections.append(table)
                sections.append(Text(""))  # spacer

        if not sections:
            return Panel(
                Text("Waiting for position data...", style="dim"),
                title="Liquidation Heatmap",
                border_style="bright_cyan",
            )

        asset_label = assets[0] if self.single_symbol else f"{len(assets)} assets"
        return Panel(
            Group(*sections),
            title=f"[bold bright_cyan]Liquidation Heatmap — {asset_label}[/]",
            subtitle=f"[dim]Live from Hyperliquid │ ◄Red=Long risk  Green=Short risk► │ Cycle {self.cycle}[/]",
            border_style="bright_cyan",
            box=box.DOUBLE_EDGE,
            padding=(1, 1),
        )

    def build_compact(self) -> Panel:
        """Compact version for combined dashboard — shows top 3 assets."""
        assets = [self.single_symbol] if self.single_symbol else self._discover_assets()[:3]
        lines = Text()

        for symbol in assets:
            price = self.market_prices.get(symbol, 0.0)
            if price <= 0:
                continue

            buckets = compute_heatmap_buckets(
                self.positions, price, symbol=symbol, n_buckets=8, range_pct=8.0,
            )
            if not buckets:
                continue

            max_val = max(max(b.total_usd for b in buckets), 1)
            bar_w = 10

            lines.append(f" {symbol} ${price:,.0f}\n", style="bold bright_white")
            for bucket in buckets:
                is_current = bucket.price_low <= price <= bucket.price_high
                style = CURRENT_STYLE if is_current else "dim"

                if price > 1000:
                    lines.append(f"${bucket.mid:>7,.0f} ", style=style)
                else:
                    lines.append(f"${bucket.mid:>7,.1f} ", style=style)

                if bucket.long_usd > 0:
                    n = max(1, int(bucket.long_usd / max_val * bar_w))
                    lines.append("█" * n, style=LONG_STYLE)
                if bucket.short_usd > 0:
                    n = max(1, int(bucket.short_usd / max_val * bar_w))
                    lines.append("█" * n, style=SHORT_STYLE)
                if is_current:
                    lines.append(" ◄", style=CURRENT_STYLE)
                lines.append("\n")
            lines.append("\n")

        if not lines.plain.strip():
            lines.append("Waiting...", style="dim")

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
