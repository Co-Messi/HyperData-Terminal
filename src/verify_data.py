#!/usr/bin/env python3
"""HyperData Terminal — data integrity verification CLI.

Starts a hub, lets it collect data for a few seconds, then runs the
DataHealthMonitor against live external sources (Binance spot/premium/LSR,
Deribit) and per-feed staleness, and prints a PASS/WARN/FAIL report.

These are the same checks the running service exposes at /v1/health and that
drive the dashboard's live/stale/drift badge — this CLI just runs them once.

Usage:
    python3 src/verify_data.py
    python3 src/verify_data.py --wait 30   # longer warm-up
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Path setup — same as run_dashboard.py
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from rich.console import Console

from src.data_layer.hub import HyperDataHub

console = Console()

_STATUS_STYLE = {"pass": "bold bright_green", "warn": "bold yellow", "fail": "bold bright_red"}
_STATUS_MARK = {"pass": "✅ PASS", "warn": "⚠️  WARN", "fail": "❌ FAIL"}


async def run_audit(wait_secs: int) -> int:
    console.print("\n[bold bright_cyan]═══════════════════════════════════════════[/]")
    console.print("[bold bright_cyan] HYPERDATA TERMINAL — INTEGRITY REPORT[/]")
    console.print("[bold bright_cyan]═══════════════════════════════════════════[/]\n")
    console.print(f"[dim]Starting hub and collecting data for {wait_secs}s...[/]")

    hub = HyperDataHub()
    await hub.start()
    try:
        await asyncio.sleep(wait_secs)
        result = await hub.health.run_checks()
    finally:
        await hub.stop()

    by_cat: dict[str, list[dict]] = {}
    for c in result["checks"]:
        by_cat.setdefault(c["category"], []).append(c)

    for cat, items in by_cat.items():
        console.print(f"\n[bold bright_yellow]{cat.upper()} CHECKS:[/]")
        for c in items:
            mark = _STATUS_MARK.get(c["status"], c["status"])
            style = _STATUS_STYLE.get(c["status"], "white")
            console.print(f"  [{style}]{mark}[/]  {c['name']}: {c['detail']}")

    counts = result["counts"]
    total = counts["pass"] + counts["warn"] + counts["fail"]
    console.print("\n[bold bright_cyan]═══════════════════════════════════════════[/]")
    console.print(
        f"[bold]OVERALL: {result['overall'].upper()} — "
        f"{counts['pass']}/{total} pass, {counts['warn']} warn, {counts['fail']} fail[/]"
    )
    console.print("[bold bright_cyan]═══════════════════════════════════════════[/]\n")

    # Non-zero exit if anything actively failed (handy for CI / scripts).
    return 1 if counts["fail"] else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="HyperData Terminal data integrity verification")
    parser.add_argument("--wait", type=int, default=15, help="Seconds to collect before checking (default: 15)")
    args = parser.parse_args()

    rc = 0
    try:
        rc = asyncio.run(run_audit(args.wait))
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/]")
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
