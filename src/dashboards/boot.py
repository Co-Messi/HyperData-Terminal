"""Boot sequence and utility screens for HyperData."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.data_layer.hub import HyperDataHub

DASHBOARD_INFO = {
    "liq":     {"name": "Liquidation Watch",   "desc": "BTC positions closest to liquidation",     "color": "bright_cyan"},
    "stream":  {"name": "Liquidation Stream",  "desc": "Multi-exchange liquidation feed",          "color": "bright_cyan"},
    "heatmap": {"name": "Liquidation Heatmap", "desc": "Price-level liquidation risk visualization","color": "bright_red"},
    "cvd":     {"name": "CVD Order Flow",      "desc": "BTC cumulative volume delta & signals",    "color": "bright_green"},
    "market":  {"name": "Market Overview",     "desc": "Funding rates, OI, prices for all assets", "color": "bright_magenta"},
    "whale":   {"name": "Whale Tracker",       "desc": "Largest open positions on Hyperliquid",    "color": "bright_cyan"},
}


async def print_boot_sequence(console: Console, mode: str, dashboards: list[str], hub: HyperDataHub) -> None:
    """Institutional-grade boot sequence with animated component initialization."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    bar_width = 50

    console.clear()
    logo_lines = [
        "\u2588\u2588\u2557  \u2588\u2588\u2557\u2588\u2588\u2557   \u2588\u2588\u2557\u2588\u2588\u2588\u2588\u2588\u2588\u2557 \u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557\u2588\u2588\u2588\u2588\u2588\u2588\u2557 \u2588\u2588\u2588\u2588\u2588\u2588\u2557  \u2588\u2588\u2588\u2588\u2588\u2557 \u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557 \u2588\u2588\u2588\u2588\u2588\u2557 ",
        "\u2588\u2588\u2551  \u2588\u2588\u2551\u255a\u2588\u2588\u2557 \u2588\u2588\u2554\u255d\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2554\u2550\u2550\u2550\u2550\u255d\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u255a\u2550\u2550\u2588\u2588\u2554\u2550\u2550\u255d\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557",
        "\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2551 \u255a\u2588\u2588\u2588\u2588\u2554\u255d \u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d\u2588\u2588\u2588\u2588\u2588\u2557  \u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d\u2588\u2588\u2551  \u2588\u2588\u2551\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2551   \u2588\u2588\u2551   \u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2551",
        "\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2551  \u255a\u2588\u2588\u2554\u255d  \u2588\u2588\u2554\u2550\u2550\u2550\u255d \u2588\u2588\u2554\u2550\u2550\u255d  \u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2557\u2588\u2588\u2551  \u2588\u2588\u2551\u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2551   \u2588\u2588\u2551   \u2588\u2588\u2554\u2550\u2550\u2588\u2588\u2551",
        "\u2588\u2588\u2551  \u2588\u2588\u2551   \u2588\u2588\u2551   \u2588\u2588\u2551     \u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557\u2588\u2588\u2551  \u2588\u2588\u2551\u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d\u2588\u2588\u2551  \u2588\u2588\u2551   \u2588\u2588\u2551   \u2588\u2588\u2551  \u2588\u2588\u2551",
        "\u255a\u2550\u255d  \u255a\u2550\u255d   \u255a\u2550\u255d   \u255a\u2550\u255d     \u255a\u2550\u2550\u2550\u2550\u2550\u2550\u255d\u255a\u2550\u255d  \u255a\u2550\u255d\u255a\u2550\u2550\u2550\u2550\u2550\u255d \u255a\u2550\u255d  \u255a\u2550\u255d   \u255a\u2550\u255d   \u255a\u2550\u255d  \u255a\u2550\u255d",
    ]

    logo = Text()
    logo.append("\n", style="")
    for line in logo_lines:
        logo.append(line + "\n", style="bold bright_cyan")

    logo.append("\n  " + "=" * 58 + "\n", style="bright_blue")
    logo.append("  HYPERLIQUID TRADING DATA TERMINAL", style="bold bright_white")
    logo.append("     v1.0\n", style="dim bright_cyan")
    logo.append("  " + "=" * 58 + "\n\n", style="bright_blue")

    mode_style = "bold bright_green on dark_green" if mode == "DEMO" else "bold bright_red on dark_red"
    logo.append("  MODE  ", style="bold white on grey30")
    logo.append(f"  {mode}  ", style=mode_style)
    logo.append("   ", style="")
    logo.append("ARCHITECTURE  ", style="bold white on grey30")
    logo.append("  Single Hub \u2192 Unified Data  \n\n", style="bold bright_white")

    logo.append(f"  Session: {now_str}\n", style="dim bright_white")
    logo.append(f"  PID: {os.getpid()}\n\n", style="dim")

    console.print(Panel(logo, border_style="bright_cyan", box=box.DOUBLE_EDGE, padding=(1, 3)))

    components = [
        ("Liquidation Feed", "4 exchanges: Hyperliquid, Binance, Bybit, OKX", "bright_cyan"),
        ("Order Flow Engine", f"WebSocket trades for {len(hub.symbols[:8])} symbols", "bright_green"),
        ("Position Scanner", "Whale tracking + liquidation distance calc", "bright_yellow"),
        ("Market Data", f"{len(hub.symbols)} assets: prices, funding, OI", "bright_magenta"),
    ]

    console.print()
    nw = 22  # name column width (same for both boxes)
    bw = 80  # box inner width

    console.print(f"  [bold bright_cyan]\u250c\u2500 INITIALIZING COMPONENTS " + "\u2500" * (bw - 26) + "\u2510[/]")
    console.print(f"  [bright_cyan]\u2502[/]{' ' * bw}[bright_cyan]\u2502[/]")

    for name, desc, color in components:
        content = f"  \u25cb  {name:<{nw}}{desc}"
        pad = bw - len(content)
        pad_ck = bw - len(content) - 2  # reserve space for " ✓"
        rich = f"  [{color}]\u25cb[/]  [{color}]{name:<{nw}}[/][dim]{desc}[/]"
        console.print(f"  [bright_cyan]\u2502[/]{rich}{' ' * max(pad, 0)}[bright_cyan]\u2502[/]")
        await asyncio.sleep(0.3)
        console.file.write("\x1b[1A\x1b[2K")
        console.print(f"  [bright_cyan]\u2502[/]{rich}{' ' * max(pad_ck, 0)} [bold bright_green]\u2713[/][bright_cyan]\u2502[/]")

    console.print(f"  [bright_cyan]\u2502[/]{' ' * bw}[bright_cyan]\u2502[/]")
    console.print(f"  [bold bright_cyan]\u2514" + "\u2500" * bw + "\u2518[/]")

    console.print()
    console.print(f"  [bold bright_white]\u250c\u2500 ACTIVE DASHBOARDS " + "\u2500" * (bw - 21) + "\u2510[/]")
    for d in dashboards:
        info = DASHBOARD_INFO.get(d, {"name": d, "desc": "", "color": "white"})
        content = f"  \u25b8  {info['name']:<{nw}}{info['desc']}"
        pad = bw - len(content)
        rich = f"  [{info['color']}]\u25b8[/]  [{info['color']}]{info['name']:<{nw}}[/][dim]{info['desc']}[/]"
        console.print(f"  [bright_white]\u2502[/]{rich}{' ' * max(pad, 0)}[bright_white]\u2502[/]")
        await asyncio.sleep(0.15)
    console.print(f"  [bold bright_white]\u2514" + "\u2500" * bw + "\u2518[/]")

    console.print()
    for i in range(bar_width + 1):
        filled = "\u2588" * i
        empty = "\u2591" * (bar_width - i)
        pct = int(i / bar_width * 100)
        if i > 0:
            console.file.write("\x1b[1A\x1b[2K")
        console.print(f"  [bright_cyan]Starting hub:[/]  [bright_green]{filled}[/][dim]{empty}[/]  [bold bright_white]{pct}%[/]")
        await asyncio.sleep(0.02)

    await hub.start()
    await asyncio.sleep(1.5)

    console.print()
    console.print("  [bold bright_green]\u2588\u2588 ALL SYSTEMS ONLINE \u2588\u2588[/]  [dim]Press Ctrl+C to exit[/]")
    console.print()
    await asyncio.sleep(1)


def list_dashboards(console: Console) -> None:
    table = Table(
        title="\U0001f4cb Available Dashboards", title_style="bold bright_cyan",
        box=box.HEAVY_EDGE, border_style="bright_cyan", header_style="bold bright_white",
    )
    table.add_column("KEY", style="bold bright_yellow", justify="center", min_width=8)
    table.add_column("NAME", style="bold bright_white", min_width=20)
    table.add_column("DESCRIPTION", style="dim bright_white", min_width=40)
    for key, info in DASHBOARD_INFO.items():
        table.add_row(key, info["name"], info["desc"])
    console.print(table)
    console.print("\n[dim]Usage: python run_dashboard.py -d <KEY>[/]")
