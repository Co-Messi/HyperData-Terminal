"""Terminal front end: boot screen, dashboard menu, single-dashboard mode."""
from __future__ import annotations

import asyncio
import threading

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from hyperdata_terminal.dashboards.boot import DASHBOARD_INFO, print_boot_sequence
from hyperdata_terminal.data_layer.hub import HyperDataHub

# Accepted on the command line in addition to the DASHBOARD_INFO keys.
DASHBOARD_ALIASES = {
    "whales": "whale",
    "liquidations": "stream",
    "orderflow": "cvd",
    "combined": "all",
}
# Dashboards that can follow a symbol other than the default.
SYMBOL_DASHBOARDS = {"cvd", "heatmap"}


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
        console.print(
            f"  [{num_style}][{i}][/{num_style}]  [{name_style}]{info['name']:<22}[/{name_style}]  "
            f"[dim]{info['desc']}[/]"
        )

    console.print()
    console.print(
        f"  [bold bright_yellow][0][/]  [bold bright_white]{'All Dashboards':<22}[/]  "
        "[dim]Combined view — everything at once[/]"
    )
    console.print()
    console.print("  [dim]Press a number to select, or [bold]q[/bold] to quit[/]")
    console.print(
        "  [dim]Skip this menu next time: [bold]hyperdata heatmap[/bold], [bold]hyperdata whales[/bold], ...[/]"
    )
    console.print()


async def run_dashboard(hub: HyperDataHub, key: str, symbol: str | None = None) -> None:
    """Run one full-screen dashboard until the user presses Ctrl+C."""
    from hyperdata_terminal.dashboards.combined_dashboard import CombinedDashboard
    from hyperdata_terminal.dashboards.cvd_dashboard import CVDDashboard
    from hyperdata_terminal.dashboards.liquidation_heatmap import LiquidationHeatmapDashboard
    from hyperdata_terminal.dashboards.liquidation_stream import LiquidationStreamDashboard
    from hyperdata_terminal.dashboards.liquidation_watch import LiquidationWatchDashboard
    from hyperdata_terminal.dashboards.market_overview import MarketOverviewDashboard
    from hyperdata_terminal.dashboards.whale_tracker import WhaleTrackerDashboard

    dashboard_map = {
        "liq": lambda: LiquidationWatchDashboard(scanner=hub.positions, refresh_rate=5),
        "stream": lambda: LiquidationStreamDashboard(feed=hub.liquidations, refresh_rate=5),
        "heatmap": lambda: LiquidationHeatmapDashboard(scanner=hub.positions, refresh_rate=5, symbol=symbol),
        "cvd": lambda: CVDDashboard(engine=hub.orderflow, market_data=hub.market, symbol=symbol or "BTC"),
        "market": lambda: MarketOverviewDashboard(market_data=hub.market, refresh_rate=10),
        "whale": lambda: WhaleTrackerDashboard(scanner=hub.positions, refresh_rate=15),
        "all": lambda: CombinedDashboard(hub, refresh_rate=1),
    }
    create_fn = dashboard_map.get(key)
    if create_fn:
        await create_fn().run()


async def _ainput(prompt: str) -> str:
    """Read one line of input without blocking the event loop.

    Uses a daemon thread per prompt (human-speed churn only) so a read that
    is still pending at exit can never wedge interpreter shutdown — the
    failure mode of parking input() inside a ThreadPoolExecutor, whose
    non-daemon workers are joined at exit.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()

    def _set(value=None, exc=None):
        if fut.done():
            return
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(value)

    def _worker():
        try:
            line = input(prompt)
        except BaseException as e:  # EOFError / KeyboardInterrupt in the thread
            loop.call_soon_threadsafe(_set, None, e)
        else:
            loop.call_soon_threadsafe(_set, line)

    threading.Thread(target=_worker, daemon=True, name="menu-input").start()
    return await fut


async def _start_hub(console: Console, hub: HyperDataHub, dashboards: list[str], boot: bool) -> None:
    if boot:
        await print_boot_sequence(console, "LIVE", dashboards, hub)
    else:
        console.print("  [bright_cyan]Connecting to Hyperliquid, Binance, Bybit, OKX and Deribit...[/]")
        await hub.start()


async def run_interactive(api_port: int | None = None, boot: bool = True) -> None:
    """Boot → menu → pick dashboard → run → back to menu on Ctrl+C."""
    console = Console()

    hub = HyperDataHub(demo=False, api_port=api_port)
    menu_keys = list(DASHBOARD_INFO.keys())

    try:
        # Inside the try: Ctrl+C during the boot animation must still stop the hub.
        await _start_hub(console, hub, menu_keys, boot)
        while True:
            _build_menu(console)

            try:
                choice = (await _ainput("  Enter choice: ")).strip().lower()
            except (EOFError, KeyboardInterrupt):
                break

            if choice in ("q", "quit", "exit"):
                break

            choice = DASHBOARD_ALIASES.get(choice, choice)
            selected = None
            if choice == "0":
                selected = "all"
            elif choice.isdigit() and 1 <= int(choice) <= len(menu_keys):
                selected = menu_keys[int(choice) - 1]
            elif choice in menu_keys or choice == "all":
                selected = choice
            else:
                console.print(f"  [red]Invalid choice: {choice}[/]")
                await asyncio.sleep(1)
                continue

            console.clear()
            console.print(f"\n  [bold bright_cyan]Loading {selected}... Press Ctrl+C to return to menu[/]\n")
            try:
                await run_dashboard(hub, selected)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass  # Back to menu

    finally:
        await hub.stop()
        console.print("\n[bold bright_cyan]HyperData stopped. See you next time.[/]")


async def run_single(
    key: str, symbol: str | None = None, api_port: int | None = None, boot: bool = True,
) -> None:
    """Open one dashboard directly (``hyperdata heatmap``); Ctrl+C exits."""
    console = Console()
    # Only the combined view shows smart money; the other dashboards skip
    # the engine (and its Hyperliquid weight).
    hub = HyperDataHub(demo=False, api_port=api_port, smart_money=(key == "all"))
    try:
        await _start_hub(console, hub, [key] if key in DASHBOARD_INFO else list(DASHBOARD_INFO), boot)
        console.clear()
        await run_dashboard(hub, key, symbol=symbol)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await hub.stop()
        console.print("\n[bold bright_cyan]HyperData stopped.[/]")
