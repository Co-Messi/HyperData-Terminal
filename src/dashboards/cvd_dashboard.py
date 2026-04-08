"""
BTC CVD (Cumulative Volume Delta) Dashboard.

Real-time terminal dashboard showing order flow analysis with
multi-timeframe signals, live trade tape, and visual bars —
Moon Dev style using rich + pyfiglet.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import time
from collections import deque
from datetime import datetime

import pyfiglet
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.data_layer.orderflow_engine import (
    CVDSnapshot,
    OrderFlowEngine,
    Trade,
)
from src.data_layer.market_data import MarketData

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Signal colour mapping
# ---------------------------------------------------------------------------

SIGNAL_STYLES: dict[str, str] = {
    "STRONG_BULL": "bold bright_green",
    "BULLISH": "green",
    "NEUTRAL": "white",
    "CONTESTED": "yellow",
    "BEARISH": "red",
    "STRONG_BEAR": "bold bright_red",
}

SIGNAL_EMOJI: dict[str, str] = {
    "STRONG_BULL": "\U0001f7e2",   # green circle
    "BULLISH": "\U0001f7e1",       # yellow circle (used as warm-green)
    "NEUTRAL": "\u26aa",           # white circle
    "CONTESTED": "\u26aa",         # white circle
    "BEARISH": "\U0001f7e1",       # yellow circle (used as warm-red)
    "STRONG_BEAR": "\U0001f534",   # red circle
}

# Timeframes to display (ordered short -> long).
DISPLAY_TFS: list[str] = ["1m", "5m", "15m", "1h", "4h"]


# ---------------------------------------------------------------------------
# CVDDashboard
# ---------------------------------------------------------------------------

class CVDDashboard:
    """Terminal dashboard for BTC Cumulative Volume Delta analysis."""

    def __init__(
        self,
        engine: OrderFlowEngine | None = None,
        market_data: MarketData | None = None,
        symbol: str = "BTC",
        demo: bool = False,
    ) -> None:
        self.console = Console()
        self.engine = engine
        self.market_data = market_data
        self.symbol = symbol.upper()
        self.demo = demo
        self.cycle: int = 0
        self.refresh_rate: float = 1.0  # seconds between redraws
        self.recent_trades: deque[Trade] = deque(maxlen=20)

        # Auto-create engine when demo mode and none provided.
        if self.demo and self.engine is None:
            self.engine = OrderFlowEngine(symbols=[self.symbol])

        # If the engine exists, register a callback so we capture trades live.
        if self.engine is not None:
            self.engine.on_trade(self._on_trade)

        # Cached price / pct change from MarketData (updated each cycle).
        self._cached_price: float = 0.0
        self._cached_pct: float = 0.0

    # -- trade callback -----------------------------------------------------

    def _on_trade(self, trade: Trade) -> None:
        if trade.symbol == self.symbol:
            self.recent_trades.append(trade)

    # -- data update --------------------------------------------------------

    async def update_data(self) -> None:
        """Generate mock trades when in demo mode; no-op in live mode."""
        if not self.demo:
            return

        if self.engine is None:
            return

        base_price = 71_500.0 + random.uniform(-2000, 2000)
        for _ in range(random.randint(20, 50)):
            base_price += random.uniform(-50, 50)
            side = random.choices(["buy", "sell"], weights=[0.48, 0.52])[0]
            size = random.uniform(0.001, 0.5)
            trade = Trade(
                timestamp=time.time(),
                symbol=self.symbol,
                side=side,
                price=round(base_price, 2),
                size=round(size, 6),
                size_usd=round(size * base_price, 2),
            )
            self.engine._process_trade(trade)

    # -- visual helpers -----------------------------------------------------

    @staticmethod
    def _make_bar(ratio: float, width: int = 12) -> Text:
        """Create a visual bar from Unicode block characters.

        *ratio*: 0.0 (all sells / bearish) to 1.0 (all buys / bullish).
        Filled portion is green, empty portion is red.
        """
        ratio = max(0.0, min(1.0, ratio))
        filled = int(ratio * width)
        empty = width - filled
        text = Text()
        text.append("\u2588" * filled, style="green")
        text.append("\u2591" * empty, style="red")
        return text

    @staticmethod
    def _signal_color(signal: str) -> str:
        """Map a signal string to a rich style."""
        return SIGNAL_STYLES.get(signal, "white")

    # -- header -------------------------------------------------------------

    def build_header(self) -> Text:
        """Big ASCII art 'BTC CVD' header rendered with pyfiglet."""
        ascii_art = pyfiglet.figlet_format("BTC  CVD", font="big")
        header = Text()
        header.append(ascii_art, style="bold bright_cyan")
        header.append(
            "   Bitcoin Order Flow Alpha | Tick-Level CVD | Multi-Timeframe\n",
            style="italic bright_white",
        )
        return header

    # -- signal legend ------------------------------------------------------

    def build_signal_legend(self) -> Text:
        """Render the colour-coded signal legend row."""
        legend = Text("  SIGNALS:  ")
        entries = [
            ("STRONG_BULL", "\U0001f7e2 STRONG BULL"),
            ("BULLISH", "\U0001f7e1 BULLISH"),
            ("CONTESTED", "\u26aa CONTESTED"),
            ("BEARISH", "\U0001f7e1 BEARISH"),
            ("STRONG_BEAR", "\U0001f534 STRONG BEAR"),
        ]
        for idx, (sig, label) in enumerate(entries):
            legend.append(label, style=self._signal_color(sig))
            if idx < len(entries) - 1:
                legend.append("  ")

        # Divergence note
        legend.append("\n            ")
        legend.append("DIVERGENCE ", style="bold magenta")
        legend.append("(alpha!)", style="italic magenta")
        legend.append("\n")
        return legend

    # -- price bar ----------------------------------------------------------

    def build_price_bar(self) -> Text:
        """Current price, % change, cumulative CVD, trades/sec."""
        sym = self.symbol

        # Price data — prefer MarketData cache; fall back to engine trade price.
        price = self._cached_price
        pct = self._cached_pct

        if price == 0.0 and self.engine is not None:
            trades = self.engine.recent_trades.get(sym)
            if trades:
                price = trades[-1].price

        cvd_val = 0.0
        tps = 0.0
        if self.engine is not None:
            cvd_val = self.engine.cumulative_cvd.get(sym, 0.0)
            tps = self.engine.get_trades_per_second(sym)

        bar = Text()
        bar.append("  \u20bf ", style="bold bright_yellow")
        bar.append(f"BITCOIN  ", style="bold white")
        bar.append(f"${price:,.2f}", style="bold bright_white")
        bar.append("  ")

        pct_style = "green" if pct >= 0 else "red"
        pct_sign = "+" if pct >= 0 else ""
        bar.append(f"{pct_sign}{pct:.3f}%", style=pct_style)
        bar.append("  ")

        cvd_style = "green" if cvd_val >= 0 else "red"
        bar.append(f"CVD: {cvd_val:+,.0f}", style=cvd_style)
        bar.append("\n")
        bar.append(f"             {tps:.1f} trades/sec\n", style="dim")
        return bar

    # -- multi-timeframe table ----------------------------------------------

    def build_timeframe_table(self) -> Table:
        """Multi-timeframe table with visual bars and signals."""
        table = Table(
            title="\u26a1 BTC CVD ACROSS TIMEFRAMES \u26a1",
            title_style="bold bright_yellow",
            border_style="bright_blue",
            show_lines=True,
            expand=True,
            padding=(0, 1),
        )
        table.add_column("TF", style="bold cyan", justify="center", width=5)
        table.add_column("PRICE ACTION", justify="center", width=16)
        table.add_column("CVD FLOW", justify="center", width=14)
        table.add_column("SIGNAL", justify="center", width=14)

        if self.engine is None:
            # No engine — show placeholder rows.
            for tf in DISPLAY_TFS:
                table.add_row(tf, "---", "---", "---")
            return table

        sym = self.symbol
        snapshots = self.engine.get_all_snapshots(sym)

        for tf in DISPLAY_TFS:
            snap: CVDSnapshot | None = snapshots.get(tf)
            if snap is None or (snap.buy_volume + snap.sell_volume) == 0:
                table.add_row(tf, "---", "---", "---")
                continue

            total = snap.buy_volume + snap.sell_volume
            buy_ratio = snap.buy_volume / total if total > 0 else 0.5
            price_bar = self._make_bar(buy_ratio, width=12)

            # CVD flow bar: map OFI from [-1, 1] to [0, 1].
            cvd_ratio = (snap.ofi + 1.0) / 2.0
            cvd_bar = self._make_bar(cvd_ratio, width=10)

            sig_text = Text(snap.signal, style=self._signal_color(snap.signal))
            table.add_row(tf, price_bar, cvd_bar, sig_text)

        return table

    # -- trade tape ---------------------------------------------------------

    def build_trade_tape(self, limit: int = 15) -> Panel:
        """Live scrolling trade tape (most recent at top)."""
        lines = Text()

        # Prefer our own deque (populated via callback); fall back to engine's buffer.
        source = self.recent_trades
        if not source and self.engine is not None:
            source = self.engine.recent_trades.get(self.symbol, deque())

        trades = list(source)[-limit:]
        trades.reverse()

        if not trades:
            lines.append("  Waiting for trades...\n", style="dim italic")
        else:
            for t in trades:
                side_str = "BUY " if t.side == "buy" else "SELL"
                side_style = "bold green" if t.side == "buy" else "bold red"
                ts_str = datetime.fromtimestamp(t.timestamp).strftime("%H:%M:%S")
                lines.append(f"  {side_str}", style=side_style)
                lines.append(f" {t.symbol} ", style="white")
                lines.append(f"{ts_str} ", style="dim")
                lines.append(f"${t.size_usd:,.0f}\n", style="bright_white")

        return Panel(
            lines,
            title="LIVE TRADE TAPE",
            title_align="left",
            border_style="bright_blue",
            padding=(0, 1),
        )

    # -- divergence line ----------------------------------------------------

    def _build_divergence_line(self) -> Text | None:
        """Check for CVD vs price divergence and return a styled Text or None."""
        if self.engine is None:
            return None

        sym = self.symbol
        trades = self.engine.recent_trades.get(sym)
        if not trades or len(trades) < 5:
            return None

        prices = [t.price for t in trades]
        div = self.engine.detect_divergence(sym, prices)
        if div is None:
            return None

        line = Text()
        if div == "BULLISH_DIVERGENCE":
            line.append(
                "  \u26a0 BULLISH DIVERGENCE DETECTED — price falling, CVD rising!\n",
                style="bold bright_green on dark_green",
            )
        else:
            line.append(
                "  \u26a0 BEARISH DIVERGENCE DETECTED — price rising, CVD falling!\n",
                style="bold bright_red on dark_red",
            )
        return line

    # -- aggregate signal ---------------------------------------------------

    def _build_aggregate_signal(self) -> Text:
        """Multi-timeframe aggregate signal line."""
        if self.engine is None:
            return Text("  Aggregate: ---\n", style="dim")

        agg = self.engine.get_multi_timeframe_signal(self.symbol)
        line = Text("  Aggregate Signal: ")
        emoji = SIGNAL_EMOJI.get(agg, "")
        line.append(f"{emoji} {agg}", style=self._signal_color(agg))
        line.append("\n")
        return line

    # -- compact build (for combined dashboard) ----------------------------

    def build_compact(self) -> Panel:
        """Compact version of the CVD dashboard for combined multi-panel view."""
        from rich import box as _box

        if self.engine is None and not hasattr(self, '_mock_snapshots'):
            content = Text("  Waiting for trade data...", style="dim bright_yellow")
            return Panel(
                content,
                title="[bold bright_green]\U0001f4c8 CVD[/]",
                border_style="bright_green",
                box=_box.ROUNDED,
                padding=(0, 1),
            )

        table = Table(
            box=_box.SIMPLE_HEAVY,
            border_style="bright_green",
            header_style="bold bright_white",
            expand=True,
            padding=(0, 1),
        )
        table.add_column("TF", style="bold cyan", justify="center", width=4)
        table.add_column("FLOW", justify="center", min_width=12)
        table.add_column("SIGNAL", justify="center", min_width=10)

        if self.engine is not None:
            sym = self.symbol
            snapshots = self.engine.get_all_snapshots(sym)
            for tf in DISPLAY_TFS:
                snap = snapshots.get(tf)
                if snap is None or (snap.buy_volume + snap.sell_volume) == 0:
                    table.add_row(tf, "---", "---")
                    continue
                total = snap.buy_volume + snap.sell_volume
                buy_ratio = snap.buy_volume / total if total > 0 else 0.5
                flow_bar = self._make_bar(buy_ratio, width=10)
                sig = Text(snap.signal[:6], style=self._signal_color(snap.signal))
                table.add_row(tf, flow_bar, sig)
        else:
            for tf in DISPLAY_TFS:
                table.add_row(tf, "---", "---")

        now_str = datetime.now().strftime("%H:%M:%S")
        return Panel(
            table,
            title=f"[bold bright_green]\U0001f4c8 BTC CVD[/]",
            subtitle=f"[dim]{now_str}  #{self.cycle}[/]",
            border_style="bright_green",
            box=_box.ROUNDED,
            padding=(0, 1),
        )

    # -- compose dashboard --------------------------------------------------

    def build_dashboard(self) -> Panel:
        """Compose the full layout into a single Panel."""
        self.cycle += 1

        parts: list[Text | Table | Panel] = [
            self.build_header(),
            self.build_signal_legend(),
            Text("\n"),
            self.build_price_bar(),
            self._build_aggregate_signal(),
        ]

        div_line = self._build_divergence_line()
        if div_line is not None:
            parts.append(div_line)

        parts.append(Text("\n"))
        parts.append(self.build_timeframe_table())
        parts.append(Text("\n"))
        parts.append(self.build_trade_tape())

        # Cycle counter / timestamp footer.
        footer = Text(
            f"  cycle {self.cycle} | "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
            f"refresh {self.refresh_rate}s",
            style="dim",
        )
        parts.append(footer)

        return Panel(
            Group(*parts),
            title="[bold bright_cyan]Moon Dev[/bold bright_cyan] "
                  "[dim]|[/dim] "
                  "[bold bright_yellow]BTC CVD Dashboard[/bold bright_yellow]",
            border_style="bright_blue",
            padding=(1, 2),
        )

    # -- main loop ----------------------------------------------------------

    async def _refresh_market_data(self) -> None:
        """Refresh cached price / pct change from MarketData if available."""
        if self.market_data is None:
            return
        try:
            asset = await self.market_data.get_asset(self.symbol)
            self._cached_price = asset.price
            self._cached_pct = asset.price_change_24h_pct * 100.0
        except Exception:
            pass  # non-critical; we'll show engine price if available

    async def run(self) -> None:
        """Main loop — refresh the dashboard every *refresh_rate* seconds."""
        with Live(
            self.build_dashboard(),
            console=self.console,
            refresh_per_second=2,
            screen=True,
        ) as live:
            while True:
                await self._refresh_market_data()
                live.update(self.build_dashboard())
                await asyncio.sleep(self.refresh_rate)


# ---------------------------------------------------------------------------
# Mock data generator (demo mode)
# ---------------------------------------------------------------------------

async def generate_mock_trades(engine: OrderFlowEngine) -> None:
    """Generate realistic mock BTC trades at high frequency."""
    base_price: float = 71_500.0
    while True:
        base_price += random.uniform(-50, 50)
        # Slight sell-pressure bias to make divergence easier to trigger.
        side = random.choices(["buy", "sell"], weights=[0.48, 0.52])[0]
        size = random.uniform(0.001, 0.5)
        trade = Trade(
            timestamp=time.time(),
            symbol="BTC",
            side=side,
            price=round(base_price, 2),
            size=round(size, 6),
            size_usd=round(size * base_price, 2),
        )
        engine._process_trade(trade)
        await asyncio.sleep(random.uniform(0.01, 0.2))


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

async def _main(live: bool = False) -> None:
    engine = OrderFlowEngine(symbols=["BTC"])
    market_data = MarketData()

    dashboard = CVDDashboard(engine=engine, market_data=market_data, symbol="BTC")

    if live:
        # Real data from Hyperliquid WebSocket.
        await engine.start()
        try:
            await dashboard.run()
        finally:
            await engine.stop()
    else:
        # Demo mode — mock trades.
        mock_task = asyncio.create_task(generate_mock_trades(engine))
        try:
            await dashboard.run()
        finally:
            mock_task.cancel()
            try:
                await mock_task
            except asyncio.CancelledError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC CVD Dashboard")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Connect to Hyperliquid WebSocket for live data (default: demo mode)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        asyncio.run(_main(live=args.live))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
