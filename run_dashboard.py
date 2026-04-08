#!/usr/bin/env python3
"""
HyperData — Terminal Trading Intelligence

Usage:
  python run_dashboard.py                    # All dashboards, live mode
  python run_dashboard.py -d liq             # Single dashboard
  python run_dashboard.py --demo             # Mock data mode
  python run_dashboard.py --list             # Show available dashboards
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

from src.dashboards.boot import DASHBOARD_INFO, list_dashboards, print_boot_sequence
from src.dashboards.combined_dashboard import CombinedDashboard
from src.data_layer.hub import HyperDataHub
from src.dashboards.liquidation_watch import LiquidationWatchDashboard
from src.dashboards.liquidation_stream import LiquidationStreamDashboard
from src.dashboards.cvd_dashboard import CVDDashboard
from src.dashboards.market_overview import MarketOverviewDashboard
from src.dashboards.whale_tracker import WhaleTrackerDashboard


async def run_single(hub: HyperDataHub, name: str) -> None:
    """Run a single full-screen dashboard, connected to the hub."""
    dashboard_map = {
        "liq": lambda: LiquidationWatchDashboard(scanner=hub.positions, demo=hub.demo, refresh_rate=5),
        "stream": lambda: LiquidationStreamDashboard(feed=hub.liquidations, demo=hub.demo, refresh_rate=5),
        "cvd": lambda: CVDDashboard(engine=hub.orderflow, market_data=hub.market, demo=hub.demo, symbol="BTC"),
        "market": lambda: MarketOverviewDashboard(market_data=hub.market, demo=hub.demo, refresh_rate=10),
        "whale": lambda: WhaleTrackerDashboard(scanner=hub.positions, demo=hub.demo, refresh_rate=15),
    }
    create_fn = dashboard_map.get(name)
    if not create_fn:
        Console().print(f"[red]Unknown dashboard: {name}[/red]")
        return
    await create_fn().run()


async def run(dashboard_name: str | None, live: bool, api_port: int | None = None) -> None:
    console = Console()
    mode = "LIVE" if live else "DEMO"
    active = [dashboard_name] if dashboard_name else list(DASHBOARD_INFO.keys())

    hub = HyperDataHub(demo=not live, api_port=api_port)
    await print_boot_sequence(console, mode, active, hub)

    try:
        if dashboard_name:
            await run_single(hub, dashboard_name)
        else:
            await CombinedDashboard(hub, refresh_rate=1).run()
    finally:
        await hub.stop()
        console.print("\n[bold bright_cyan]HyperData stopped. See you next time.[/]")


def main() -> None:
    parser = argparse.ArgumentParser(description="HyperData — Terminal Trading Intelligence")
    parser.add_argument("-d", "--dashboard", choices=list(DASHBOARD_INFO.keys()), default=None,
                        help="Run a single dashboard (default: all combined)")
    parser.add_argument("--live", action="store_true", default=True,
                        help="Use real exchange data (default)")
    parser.add_argument("--demo", action="store_true", help="Use mock data instead of live")
    parser.add_argument("--api-port", type=int, default=None,
                        help="Start REST API on this port (e.g. 8420)")
    parser.add_argument("--list", action="store_true", help="List available dashboards")
    args = parser.parse_args()

    if args.list:
        list_dashboards(Console())
        return

    use_live = not args.demo

    # File handler only — no console output while Rich Live owns the terminal.
    # Console logging (even WARNING) causes screen flashes/glitches.
    # All logs go to data/logs/hyperdata.log instead.
    log_dir = Path(_PROJECT_ROOT) / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "hyperdata.log", maxBytes=5 * 1024 * 1024, backupCount=3,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    logging.basicConfig(
        level=logging.DEBUG,
        handlers=[file_handler],
    )

    try:
        api_port = args.api_port or int(os.environ.get("HYPERDATA_API_PORT", "0")) or None
        asyncio.run(run(args.dashboard, use_live, api_port=api_port))
    except KeyboardInterrupt:
        Console().print("\n[bold bright_cyan]HyperData stopped.[/]")


if __name__ == "__main__":
    main()
