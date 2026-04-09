#!/usr/bin/env python3
"""
HyperData Terminal — Live Market Data

Just type: hyperdata
Pick a dashboard from the interactive menu. Press q to go back.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import sys
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "src"))

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich import box

from src.dashboards.boot import DASHBOARD_INFO, print_boot_sequence
from src.data_layer.hub import HyperDataHub
from src.dashboards.liquidation_watch import LiquidationWatchDashboard
from src.dashboards.liquidation_stream import LiquidationStreamDashboard
from src.dashboards.cvd_dashboard import CVDDashboard
from src.dashboards.market_overview import MarketOverviewDashboard
from src.dashboards.whale_tracker import WhaleTrackerDashboard
from src.dashboards.liquidation_heatmap import LiquidationHeatmapDashboard


def _build_menu(console: Console) -> None:
    """Show the interactive dashboard picker."""
    console.clear()

    logo = Text()
    logo.append("\n  ⚡ HYPERDATA TERMINAL ⚡\n", style="bold bright_cyan")
    logo.append("  Live crypto market data from 5 exchanges\n\n", style="dim bright_white")

    console.print(Panel(logo, border_style="bright_cyan", box=box.DOUBLE_EDGE, padding=(0, 3)))
    console.print()

    menu_items = list(DASHBOARD_INFO.items())
    for i, (key, info) in enumerate(menu_items, 1):
        num_style = "bold bright_yellow"
        name_style = f"bold {info['color']}"
        console.print(f"  [{num_style}][{i}][/{num_style}]  [{name_style}]{info['name']:<22}[/{name_style}]  [dim]{info['desc']}[/]")

    console.print()
    console.print(f"  [bold bright_yellow][0][/]  [bold bright_white]{'All Dashboards':<22}[/]  [dim]Combined view — everything at once[/]")
    console.print()
    console.print("  [dim]Press a number to select, or [bold]q[/bold] to quit[/]")
    console.print()


async def run_dashboard(hub: HyperDataHub, key: str) -> None:
    """Run a single full-screen dashboard. Returns when user presses Ctrl+C."""
    from src.dashboards.combined_dashboard import CombinedDashboard

    dashboard_map = {
        "liq": lambda: LiquidationWatchDashboard(scanner=hub.positions, refresh_rate=5),
        "stream": lambda: LiquidationStreamDashboard(feed=hub.liquidations, refresh_rate=5),
        "heatmap": lambda: LiquidationHeatmapDashboard(scanner=hub.positions, refresh_rate=5),
        "cvd": lambda: CVDDashboard(engine=hub.orderflow, market_data=hub.market, symbol="BTC"),
        "market": lambda: MarketOverviewDashboard(market_data=hub.market, refresh_rate=10),
        "whale": lambda: WhaleTrackerDashboard(scanner=hub.positions, refresh_rate=15),
        "all": lambda: CombinedDashboard(hub, refresh_rate=1),
    }
    create_fn = dashboard_map.get(key)
    if create_fn:
        await create_fn().run()


async def run_interactive(api_port: int | None = None) -> None:
    """Boot → menu → pick dashboard → run → back to menu on Ctrl+C."""
    console = Console()

    hub = HyperDataHub(demo=False, api_port=api_port)
    await print_boot_sequence(console, "LIVE", list(DASHBOARD_INFO.keys()), hub)

    menu_keys = list(DASHBOARD_INFO.keys())

    try:
        while True:
            _build_menu(console)

            # Read user choice
            try:
                choice = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("  Enter choice: ").strip().lower()
                )
            except (EOFError, KeyboardInterrupt):
                break

            if choice in ("q", "quit", "exit"):
                break

            # Map number to dashboard key
            selected = None
            if choice == "0":
                selected = "all"
            elif choice.isdigit() and 1 <= int(choice) <= len(menu_keys):
                selected = menu_keys[int(choice) - 1]
            elif choice in menu_keys:
                selected = choice
            else:
                console.print(f"  [red]Invalid choice: {choice}[/]")
                await asyncio.sleep(1)
                continue

            # Run the selected dashboard
            console.clear()
            console.print(f"\n  [bold bright_cyan]Loading {selected}... Press Ctrl+C to return to menu[/]\n")
            try:
                await run_dashboard(hub, selected)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass  # Back to menu

    finally:
        await hub.stop()
        console.print("\n[bold bright_cyan]HyperData stopped. See you next time.[/]")


def main() -> None:
    parser = argparse.ArgumentParser(description="HyperData Terminal — Live Market Data")
    parser.add_argument("--api-port", type=int, default=None,
                        help="Start REST API on this port (e.g. 8420)")
    args = parser.parse_args()

    # File handler only — no console output while Rich Live owns the terminal.
    log_dir = Path(_PROJECT_ROOT) / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "hyperdata.log", maxBytes=5 * 1024 * 1024, backupCount=3,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.basicConfig(level=logging.DEBUG, handlers=[file_handler])

    try:
        api_port = args.api_port or int(os.environ.get("HYPERDATA_API_PORT", "0")) or None
        asyncio.run(run_interactive(api_port=api_port))
    except KeyboardInterrupt:
        Console().print("\n[bold bright_cyan]HyperData stopped.[/]")


if __name__ == "__main__":
    main()
