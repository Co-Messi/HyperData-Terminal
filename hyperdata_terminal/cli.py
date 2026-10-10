"""The ``hyperdata`` command.

    hyperdata                      interactive dashboard menu
    hyperdata heatmap [--symbol]   open one dashboard directly (liq, stream,
                                   heatmap, cvd, market, whales, all)
    hyperdata api [--port]         headless REST + WebSocket API
    hyperdata paper -s NAME|FILE   paper trade strategies on live data
    hyperdata verify [--wait]      one-shot data integrity report
    hyperdata mcp                  MCP server (stdio) for AI agents
    hyperdata alerts [--test]      alert settings; --test sends a test alert
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import math
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path

from hyperdata_terminal import __version__

# hyperdata_terminal.paths resolves the data dir once, at import, from
# HYPERDATA_DATA_DIR. It is imported lazily, after .env is loaded, so the
# variable works from a .env file too.

DASHBOARD_HELP = {
    "liq": "BTC positions closest to liquidation",
    "stream": "multi-exchange liquidation feed",
    "heatmap": "price levels where liquidations cluster",
    "cvd": "cumulative volume delta and order flow signals",
    "market": "prices, funding, open interest for every asset",
    "whale": "largest open positions on Hyperliquid",
    "all": "everything at once",
}
# Command name on the CLI when it differs from the internal dashboard key, and extra aliases.
DASHBOARD_CLI_NAME = {"whale": "whales"}
DASHBOARD_CLI_ALIASES = {"whale": ["whale"], "all": ["combined"], "stream": ["liquidations"], "cvd": ["orderflow"]}


def _setup_logging() -> Path:
    """File logging only: Rich owns the terminal (and stdout is the protocol for MCP)."""
    from hyperdata_terminal.paths import LOG_DIR

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "hyperdata.log"
    handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.basicConfig(level=logging.DEBUG, handlers=[handler], force=True)
    return log_file


def _load_env() -> None:
    """Read ``.env`` from the working directory (LLM keys, alert webhooks)."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a hard dependency
        return
    load_dotenv(Path.cwd() / ".env", override=False)


def _api_port(args: argparse.Namespace) -> int | None:
    port = getattr(args, "api_port", None)
    if port:
        return port
    try:
        return int(os.environ.get("HYPERDATA_API_PORT", "0")) or None
    except ValueError:
        return None


def _positive(kind: type) -> Callable[[str], float]:
    """argparse type: a number above zero (a zero balance divided by zero on exit)."""
    def parse(text: str) -> float:
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
        if not (math.isfinite(value) and value > 0):
            raise argparse.ArgumentTypeError(f"must be a finite number above zero, got {text}")
        return value
    return parse


def build_parser() -> argparse.ArgumentParser:
    from hyperdata_terminal.paths import DATA_DIR

    parser = argparse.ArgumentParser(
        prog="hyperdata",
        description="HyperData Terminal: live crypto market data from Hyperliquid, Binance, Bybit, OKX and Deribit.",
        epilog=f"State and logs live in {DATA_DIR} (override with HYPERDATA_DATA_DIR).",
    )
    parser.add_argument("--version", action="version", version=f"hyperdata-terminal {__version__}")
    parser.add_argument("--no-boot", action="store_true", help="skip the boot animation")
    parser.add_argument("--api-port", type=int, default=None, help="also serve the REST API on this port")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    for key, help_text in DASHBOARD_HELP.items():
        p = sub.add_parser(
            DASHBOARD_CLI_NAME.get(key, key), aliases=DASHBOARD_CLI_ALIASES.get(key, []),
            help=f"dashboard: {help_text}",
        )
        p.set_defaults(dashboard=key)
        # SUPPRESS so a flag given before the subcommand is not reset by the subparser's default.
        p.add_argument("--no-boot", action="store_true", default=argparse.SUPPRESS, help="skip the boot animation")
        p.add_argument("--api-port", type=int, default=argparse.SUPPRESS, help="also serve the REST API")
        if key in ("cvd", "heatmap"):
            p.add_argument("--symbol", "-s", default=None, help="asset to follow (default BTC)")

    api = sub.add_parser("api", help="run the headless REST + WebSocket API")
    api.add_argument(
        "--port", type=int, default=None, help="port to bind (default: --api-port, HYPERDATA_API_PORT or 8420)",
    )

    paper = sub.add_parser(
        "paper", help="paper trade strategies on live data",
        description="Run strategies against live market data with fake money. Trades are logged "
                    f"to {DATA_DIR / 'paper_trades.db'}.",
    )
    paper.add_argument(
        "--strategy", "-s", action="append", dest="strategies", metavar="SPEC",
        help="built-in name (cvd_momentum, funding_rate_arb, liquidation_cascade, whale_follow, "
             "llm_agent), a path to your own .py file, or module:Class. Repeatable. Default: cvd_momentum",
    )
    paper.add_argument("--interval", type=_positive(int), default=30, help="seconds between evaluations (default 30)")
    paper.add_argument("--balance", type=_positive(float), default=10_000.0, help="starting paper balance in USD")
    paper.add_argument("--reverse", action="store_true", help="an opposite signal closes AND reverses")
    paper.add_argument("--minutes", type=_positive(float), default=None, help="stop after this many minutes")

    verify = sub.add_parser("verify", help="one-shot data integrity report (exit 1 on failure)")
    verify.add_argument("--wait", type=int, default=15, help="seconds to collect before checking")

    sub.add_parser("mcp", help="serve live market data to AI agents over MCP (stdio)")

    alerts = sub.add_parser(
        "alerts", help="show what alerts fire and where, or send a test alert",
        description="Liquidation cascade alerts go to Telegram (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) "
                    "and/or Discord (DISCORD_WEBHOOK_URL) while any hyperdata command is running.",
    )
    alerts.add_argument("--test", action="store_true", help="send one test message to every configured channel")
    return parser


async def _run_api(port: int) -> int:
    from hyperdata_terminal.data_layer.hub import HyperDataHub

    hub = HyperDataHub(demo=False, api_port=port)
    await hub.start()
    if "api_server" in hub.status.failed_components:
        await hub.stop()
        print(f"hyperdata: could not start the API on port {port} (in use? see the log)", file=sys.stderr)
        return 1
    print(f"HyperData API on http://127.0.0.1:{port}/v1/health  (Ctrl+C to stop)", flush=True)
    logging.getLogger(__name__).info("HyperData API running on port %d (headless)", port)

    # Wait for SIGINT/SIGTERM so `kill <pid>` (what process managers send)
    # triggers a clean hub.stop(): flushing persistence and closing sockets.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # add_signal_handler is unavailable on Windows
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await hub.stop()
    return 0


async def _run_paper(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.table import Table

    from hyperdata_terminal.data_layer.hub import HyperDataHub
    from hyperdata_terminal.strategies import PaperTrader
    from hyperdata_terminal.strategies.loader import StrategyLoadError, load_strategies

    console = Console()
    try:
        strategies = load_strategies(args.strategies or ["cvd_momentum"])
    except StrategyLoadError as exc:
        console.print(f"[bold red]error:[/] {exc}")
        return 2

    hub = HyperDataHub(demo=False)
    console.print("[bright_cyan]Connecting to exchanges... strategies run every "
                  f"{args.interval}s once data arrives. Ctrl+C prints the portfolio and exits.[/]")
    await hub.start()
    trader = PaperTrader(
        hub, strategies, check_interval=args.interval,
        starting_balance=args.balance, reverse_on_opposite_signal=args.reverse,
    )
    await trader.start()
    try:
        if args.minutes:
            await asyncio.sleep(args.minutes * 60)
        else:
            await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        portfolio = trader.get_portfolio()
        await trader.stop()
        await hub.stop()

        table = Table(title="Paper portfolio", show_header=False)
        table.add_row("Balance", f"${portfolio['balance']:,.2f}")
        table.add_row("Open positions", str(len(portfolio["positions"])))
        table.add_row("Total value", f"${portfolio['total_value']:,.2f}")
        table.add_row("Total PnL", f"${portfolio['total_pnl']:+,.2f} ({portfolio['total_pnl_pct']:+.2f}%)")
        table.add_row("Trades this run", str(len(trader.trades)))
        table.add_row("Trade log", str(trader.db_path))
        console.print(table)
    return 0


async def _run_alerts(args: argparse.Namespace) -> int:
    from hyperdata_terminal.data_layer.alerts import AlertManager, fmt_usd

    mgr = AlertManager()
    print("Liquidation cascade alerts (confirmed liquidations, all symbols):")
    for rule in mgr.rules:
        print(f"  {fmt_usd(rule.threshold_usd)} or more in {rule.label}, "
              f"at most once every {rule.cooldown_seconds / 60:.0f} minutes")
    channels = mgr.channels()
    print(f"Channels: {', '.join(channels) if channels else 'none configured'}")
    if not args.test:
        return 0
    if not channels:
        print("hyperdata: no alert channel configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, "
              "or DISCORD_WEBHOOK_URL (see .env.example).", file=sys.stderr)
        return 2
    try:
        results = await mgr.send_test()
    finally:
        await mgr.stop()
    for channel, ok in results.items():
        print(f"  {channel}: {'delivered' if ok else 'FAILED (see the log)'}")
    return 0 if results and all(results.values()) else 1


def main(argv: list[str] | None = None) -> None:
    _load_env()
    args = build_parser().parse_args(argv)
    _setup_logging()
    boot = not getattr(args, "no_boot", False)
    command = args.command

    try:
        if command is None:
            from hyperdata_terminal.terminal import run_interactive

            asyncio.run(run_interactive(api_port=_api_port(args), boot=boot))
        elif getattr(args, "dashboard", None):
            from hyperdata_terminal.terminal import run_single

            symbol = (getattr(args, "symbol", None) or "").strip().upper() or None
            if symbol and args.dashboard == "cvd":
                from hyperdata_terminal.config.settings import DEFAULT_SYMBOLS

                if symbol not in DEFAULT_SYMBOLS:
                    print(f"hyperdata: order flow is streamed for {', '.join(DEFAULT_SYMBOLS)}; "
                          f"{symbol} is not one of them", file=sys.stderr)
                    sys.exit(2)
            asyncio.run(run_single(args.dashboard, symbol=symbol, api_port=_api_port(args), boot=boot))
        elif command == "api":
            sys.exit(asyncio.run(_run_api(args.port or _api_port(args) or 8420)))
        elif command == "paper":
            sys.exit(asyncio.run(_run_paper(args)))
        elif command == "verify":
            from hyperdata_terminal.verify_data import run_audit

            sys.exit(asyncio.run(run_audit(args.wait)))
        elif command == "mcp":
            from hyperdata_terminal.mcp_server import serve

            serve()
        elif command == "alerts":
            sys.exit(asyncio.run(_run_alerts(args)))
    except KeyboardInterrupt:
        if command not in ("mcp", "paper", "api"):
            from rich.console import Console

            Console().print("\n[bold bright_cyan]HyperData stopped.[/]")
        sys.exit(130 if command in ("verify",) else 0)


if __name__ == "__main__":
    main()
