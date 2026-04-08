"""Hub-connected dashboard panels for the combined view.

Each class wraps a data source from HyperDataHub and renders a compact
rich Panel suitable for the multi-panel CombinedDashboard layout.
"""
from __future__ import annotations

from datetime import datetime, timezone

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.data_layer.hub import HyperDataHub
from src.utils.helpers import format_usd as fmt_usd, format_price as fmt_price, format_pct as fmt_pct


class HubLiqWatch:
    """Liquidation Watch — shows ALL positions near liquidation."""

    def __init__(self, hub: HyperDataHub):
        self.hub = hub
        self.cycle = 0

    def build_compact(self) -> Panel:
        from src.dashboards.liquidation_watch import compute_zone_breakdown

        all_pos = self.hub.get_all_positions_sorted()
        btc_price = self.hub.get_btc_price()
        mode = self.hub.status.mode.upper()

        if not all_pos:
            return Panel(
                Text("  Scanning positions...", style="dim bright_yellow"),
                title=f"[bold bright_cyan]\U0001f525 LIQUIDATION WATCH ({mode})[/]",
                border_style="bright_cyan", box=box.ROUNDED, padding=(0, 1),
            )

        zones = compute_zone_breakdown(all_pos)
        zone_lines = Text()
        for z in zones:
            zone_lines.append(f" {z['label']:<12}", style=z["style"])
            zone_lines.append(f" L:{z['long_count']:>3} ", style="bright_green")
            zone_lines.append(f"{fmt_usd(z['long_value']):>8}", style="green")
            zone_lines.append(f"  S:{z['short_count']:>3} ", style="bright_red")
            zone_lines.append(f"{fmt_usd(z['short_value']):>8}", style="red")
            zone_lines.append(f"  T:{fmt_usd(z['total_value']):>8}\n", style="bold bright_yellow")

        danger = [p for p in all_pos if p.distance_pct < 5.0][:15]
        table = Table(
            box=box.SIMPLE_HEAVY, border_style="bright_cyan",
            header_style="bold bright_white", expand=True, padding=(0, 0),
        )
        table.add_column("SIDE", justify="center", width=2)
        table.add_column("SYM", style="bright_white", justify="center", width=5)
        table.add_column("SIZE", justify="right", min_width=7)
        table.add_column("DIST", justify="right", min_width=5)
        table.add_column("PnL", justify="right", min_width=7)
        table.add_column("LV", justify="right", width=3)

        for pos in danger:
            s = "bold bright_green" if pos.side == "long" else "bold bright_red"
            ps = "bright_green" if pos.unrealized_pnl >= 0 else "bright_red"
            ds = "bold bright_red" if pos.distance_pct < 1 else ("yellow" if pos.distance_pct < 2 else "white")
            sign = "+" if pos.unrealized_pnl >= 0 else ""
            table.add_row(
                Text(pos.side[0].upper(), style=s), pos.symbol,
                fmt_usd(pos.size_usd), Text(fmt_pct(pos.distance_pct), style=ds),
                Text(f"{sign}{fmt_usd(pos.unrealized_pnl)}", style=ps),
                f"{pos.leverage:.0f}x",
            )

        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        return Panel(
            Group(zone_lines, table),
            title=f"[bold bright_cyan]\U0001f525 LIQ WATCH  BTC:{fmt_price(btc_price)}  {self.hub.status.tracked_positions} tracked[/]",
            subtitle=f"[dim]{now_str}  #{self.cycle}[/]",
            border_style="bright_cyan", box=box.ROUNDED, padding=(0, 1),
        )


class HubLiqStream:
    """Liquidation Stream — packed live feed with per-exchange stats."""

    def __init__(self, hub: HyperDataHub):
        self.hub = hub
        self.cycle = 0

    def build_compact(self) -> Panel:
        from src.dashboards.liquidation_stream import EXCHANGE_COLORS, SIDE_LONG, SIDE_SHORT

        feed = self.hub.liquidations
        stats_1h = feed.get_stats(window_minutes=60)

        by_ex = stats_1h.get("by_exchange", {})
        ex_line = Text()
        for ex_name, color in [("binance", "yellow"), ("bybit", "green"), ("okx", "blue"), ("hyperliquid", "magenta")]:
            ex_data = by_ex.get(ex_name, {"count": 0, "volume_usd": 0.0})
            ex_line.append(f" {ex_name[:3].upper()}", style=f"bold {color}")
            ex_line.append(f":{ex_data['count']}", style="bright_white")
            # Show ✓ for confirmed feeds, ~ for heuristic
            if ex_name == "bybit" and ex_data["count"] > 0:
                ex_line.append("\u2713", style="bold bright_green")
            elif ex_name == "hyperliquid" and ex_data["count"] > 0:
                ex_line.append("~", style="dim yellow")
            ex_line.append(f"/{fmt_usd(ex_data['volume_usd'])} ", style="dim")

        summary = Text()
        for label, mins in [("10m", 10), ("1h", 60), ("4h", 240), ("24h", 1440)]:
            s = feed.get_stats(window_minutes=mins)
            summary.append(f" {label} ", style="bold white")
            summary.append(f"L:{s['long_count']}", style="green")
            summary.append(f"/{fmt_usd(s['long_volume_usd'])}", style="green")
            summary.append(f" S:{s['short_count']}", style="red")
            summary.append(f"/{fmt_usd(s['short_volume_usd'])}\n", style="red")

        recent = feed.get_recent(minutes=60)[:20]
        lines = Text()
        for ev in recent:
            ts = datetime.fromtimestamp(ev.timestamp, tz=timezone.utc).strftime("%H:%M:%S")
            icon = SIDE_LONG if ev.side == "long" else SIDE_SHORT
            side_style = "green" if ev.side == "long" else "red"
            ex_color = EXCHANGE_COLORS.get(ev.exchange, "white")
            confirmed_mark = "" if getattr(ev, 'confirmed', True) else "~"
            lines.append(f"{ts}", style="dim")
            lines.append(f" {icon}", style=side_style)
            lines.append(f" {ev.exchange[:3].upper()}", style=ex_color)
            lines.append(f" {ev.symbol:<5}", style="bright_white")
            lines.append(f" {confirmed_mark}{fmt_usd(ev.size_usd):>8}\n", style="bold white" if not confirmed_mark else "dim white")

        if not recent:
            lines.append("  Waiting for liquidations...\n", style="dim")

        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        total = self.hub.status.total_liquidations
        return Panel(
            Group(ex_line, Text(""), summary, Text(""), lines),
            title=f"[bold bright_cyan]\U0001f4a5 LIQUIDATIONS  {total} total  {fmt_usd(stats_1h['total_volume_usd'])}/1h[/]",
            subtitle=f"[dim]{now_str}  #{self.cycle}[/]",
            border_style="bright_cyan", box=box.ROUNDED, padding=(0, 1),
        )


class HubCVD:
    """CVD — multi-symbol order flow with live trade tape."""

    def __init__(self, hub: HyperDataHub):
        self.hub = hub
        self.cycle = 0

    def build_compact(self) -> Panel:
        from src.dashboards.cvd_dashboard import SIGNAL_STYLES

        engine = self.hub.orderflow
        tfs = ["1m", "5m", "15m", "1h", "4h"]
        symbols = ["BTC", "ETH", "SOL"]

        info = Text()
        for sym in symbols:
            cvd = engine.cumulative_cvd.get(sym, 0.0)
            tps = engine.get_trades_per_second(sym)
            agg = engine.get_multi_timeframe_signal(sym)
            agg_style = SIGNAL_STYLES.get(agg, "white")
            price_val = 0.0
            asset = self.hub.market.assets.get(sym)
            if asset:
                price_val = asset.price
            info.append(f" {sym}", style="bold bright_white")
            if price_val > 0:
                info.append(f" ${price_val:,.2f}" if price_val >= 1 else f" ${price_val:.6f}", style="bright_white")
            info.append(f" CVD:{cvd:+,.0f}", style="green" if cvd >= 0 else "red")
            info.append(f" {tps:.1f}t/s", style="dim")
            info.append(f" {agg}\n", style=agg_style)

        table = Table(
            box=box.SIMPLE_HEAVY, border_style="bright_green",
            header_style="bold bright_white", expand=True, padding=(0, 0),
        )
        table.add_column("TF", style="bold cyan", justify="center", width=3)
        table.add_column("BUY VOL", style="green", justify="right", min_width=8)
        table.add_column("SELL VOL", style="red", justify="right", min_width=8)
        table.add_column("FLOW", justify="center", min_width=12)
        table.add_column("SIGNAL", justify="center", min_width=9)

        snapshots = engine.get_all_snapshots("BTC")
        for tf in tfs:
            snap = snapshots.get(tf)
            if snap is None or (snap.buy_volume + snap.sell_volume) == 0:
                table.add_row(tf, "---", "---", "---", "---")
                continue
            total = snap.buy_volume + snap.sell_volume
            ratio = snap.buy_volume / total if total > 0 else 0.5
            filled = int(ratio * 12)
            bar = Text()
            bar.append("\u2588" * filled, style="green")
            bar.append("\u2591" * (12 - filled), style="red")
            sig_style = SIGNAL_STYLES.get(snap.signal, "white")
            table.add_row(tf, fmt_usd(snap.buy_volume), fmt_usd(snap.sell_volume),
                          bar, Text(snap.signal[:8], style=sig_style))

        tape = Text()
        recent = list(engine.recent_trades.get("BTC", []))[-10:]
        recent.reverse()
        for t in recent:
            s = "bold green" if t.side == "buy" else "bold red"
            ts = datetime.fromtimestamp(t.timestamp).strftime("%H:%M:%S")
            tape.append(f" {'BUY ' if t.side == 'buy' else 'SELL'}", style=s)
            tape.append(f" {ts} ${t.size_usd:,.0f}\n", style="dim")

        if not recent:
            tape.append("  Waiting for trades...\n", style="dim")

        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        return Panel(
            Group(info, table, Text(""), tape),
            title=f"[bold bright_green]\U0001f4c8 ORDER FLOW  {self.hub.status.total_trades_processed:,} trades[/]",
            subtitle=f"[dim]{now_str}  #{self.cycle}[/]",
            border_style="bright_green", box=box.ROUNDED, padding=(0, 1),
        )


class HubMarket:
    """Market overview — show 20+ assets with OI and volume."""

    def __init__(self, hub: HyperDataHub):
        self.hub = hub
        self.cycle = 0

    def build_compact(self) -> Panel:
        from src.dashboards.market_overview import fmt_funding

        assets = self.hub.get_all_assets()[:20]
        if not assets:
            return Panel(
                Text("  Loading...", style="dim"),
                title="[bold bright_magenta]\U0001f4ca MARKET[/]",
                border_style="bright_magenta", box=box.ROUNDED, padding=(0, 1),
            )

        all_assets = self.hub.get_all_assets()
        total_oi = sum(a.open_interest for a in all_assets)
        total_vol = sum(a.volume_24h for a in all_assets)
        green = sum(1 for a in all_assets if a.price_change_24h_pct >= 0)
        red = len(self.hub.market.assets) - green
        summary = Text()
        summary.append(f" OI:{fmt_usd(total_oi)}", style="bold bright_yellow")
        summary.append(f"  Vol:{fmt_usd(total_vol)}", style="bright_white")
        summary.append(f"  {green}", style="bright_green")
        summary.append("/", style="dim")
        summary.append(f"{red}\n", style="bright_red")

        table = Table(
            box=box.SIMPLE_HEAVY, border_style="bright_magenta",
            header_style="bold bright_white", expand=True, padding=(0, 0),
        )
        table.add_column("#", style="dim", width=2)
        table.add_column("SYM", style="bold bright_white", justify="center", width=5)
        table.add_column("PRICE", justify="right", min_width=9)
        table.add_column("CHG", justify="right", min_width=6)
        table.add_column("FUND", justify="right", min_width=7)
        table.add_column("PREM", justify="right", min_width=6)
        table.add_column("OI", justify="right", min_width=6)

        for i, a in enumerate(assets, 1):
            chg_style = "bright_green" if a.price_change_24h_pct >= 0 else "bright_red"
            fund_style = "bright_green" if a.funding_rate >= 0 else "bright_red"
            prem = getattr(a, "premium_pct", 0.0)
            if abs(prem) > 0.5:
                prem_style = "bold bright_red"
            elif abs(prem) > 0.2:
                prem_style = "bright_yellow"
            else:
                prem_style = "dim"
            prem_str = f"{prem:+.2f}%" if prem != 0.0 else "--"
            table.add_row(
                str(i), a.symbol, fmt_price(a.price),
                Text(fmt_pct(a.price_change_24h_pct), style=chg_style),
                Text(fmt_funding(a.funding_rate), style=fund_style),
                Text(prem_str, style=prem_style),
                fmt_usd(a.open_interest),
            )

        extreme = self.hub.get_extreme_funding(threshold_annualized=0.50)[:5]
        alerts = Text()
        if extreme:
            alerts.append("\n \U0001f525 EXTREME FUNDING:\n", style="bold bright_yellow")
            for a in extreme:
                ann = a.funding_rate * 8760 * 100
                style = "bold bright_green" if a.funding_rate > 0 else "bold bright_red"
                alerts.append(f" {a.symbol:<6}", style=style)
                alerts.append(f" {ann:+.0f}%/yr\n", style=style)

        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        content = Group(summary, table, alerts) if extreme else Group(summary, table)
        return Panel(
            content,
            title=f"[bold bright_magenta]\U0001f4ca MARKET ({len(self.hub.market.assets)} assets)[/]",
            subtitle=f"[dim]{now_str}  #{self.cycle}[/]",
            border_style="bright_magenta", box=box.ROUNDED, padding=(0, 1),
        )


class HubWhales:
    """Whale tracker — show 15+ largest positions with full details."""

    def __init__(self, hub: HyperDataHub):
        self.hub = hub
        self.cycle = 0

    def build_compact(self) -> Panel:
        whales = self.hub.get_whale_positions(min_size_usd=50_000)[:15]
        if not whales:
            return Panel(
                Text("  Scanning...", style="dim"),
                title="[bold bright_cyan]\U0001f40b WHALES[/]",
                border_style="bright_cyan", box=box.ROUNDED, padding=(0, 1),
            )

        total_val = sum(p.size_usd for p in whales)
        total_pnl = sum(p.unrealized_pnl for p in whales)
        longs = sum(1 for p in whales if p.side == "long")
        shorts = len(whales) - longs
        summary = Text()
        summary.append(f" Total:{fmt_usd(total_val)}", style="bold bright_yellow")
        summary.append(f"  L:{longs}", style="bright_green")
        summary.append(f" S:{shorts}", style="bright_red")
        pnl_style = "bright_green" if total_pnl >= 0 else "bright_red"
        summary.append(f"  PnL:{'+'if total_pnl>=0 else ''}{fmt_usd(total_pnl)}\n", style=pnl_style)

        table = Table(
            box=box.SIMPLE_HEAVY, border_style="bright_cyan",
            header_style="bold bright_white", expand=True, padding=(0, 0),
        )
        table.add_column("SIDE", justify="center", width=2)
        table.add_column("SYM", style="bright_white", justify="center", width=5)
        table.add_column("SIZE", justify="right", min_width=7)
        table.add_column("PnL", justify="right", min_width=7)
        table.add_column("DIST", justify="right", min_width=5)
        table.add_column("LV", justify="right", width=3)

        for p in whales:
            ss = "bold bright_green" if p.side == "long" else "bold bright_red"
            ps = "bright_green" if p.unrealized_pnl >= 0 else "bright_red"
            ds = "bright_red" if p.distance_pct < 5 else "white"
            sign = "+" if p.unrealized_pnl >= 0 else ""
            sz_style = "bold bright_yellow" if p.size_usd >= 5e6 else ("bold bright_white" if p.size_usd >= 1e6 else "bright_white")
            table.add_row(
                Text(p.side[0].upper(), style=ss), p.symbol,
                Text(fmt_usd(p.size_usd), style=sz_style),
                Text(f"{sign}{fmt_usd(p.unrealized_pnl)}", style=ps),
                Text(fmt_pct(p.distance_pct), style=ds),
                f"{p.leverage:.0f}x",
            )

        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        return Panel(
            Group(summary, table),
            title=f"[bold bright_cyan]\U0001f40b WHALES  {len(whales)} positions  {fmt_usd(total_val)}[/]",
            subtitle=f"[dim]{now_str}  #{self.cycle}[/]",
            border_style="bright_cyan", box=box.ROUNDED, padding=(0, 1),
        )


class HubStatusPanel:
    """System status panel showing hub health."""

    def __init__(self, hub: HyperDataHub):
        self.hub = hub

    def build(self) -> Panel:
        s = self.hub.status
        uptime = int(s.uptime_seconds)
        h, m, sec = uptime // 3600, (uptime % 3600) // 60, uptime % 60

        lines = Text()
        lines.append("  MODE: ", style="dim")
        mode_style = "bold bright_green" if s.mode == "demo" else "bold bright_red"
        lines.append(f"{s.mode.upper()}\n", style=mode_style)
        lines.append(f"  UPTIME: {h:02d}:{m:02d}:{sec:02d}\n", style="bright_white")
        lines.append(f"  CYCLE: #{s.scan_cycle}\n", style="bright_white")

        lines.append("\n  COMPONENTS:\n", style="bold bright_cyan")
        components = [
            ("Liquidations", s.liquidation_feed, s.total_liquidations),
            ("Order Flow", s.orderflow_engine, s.total_trades_processed),
            ("Positions", s.position_scanner, s.tracked_positions),
            ("Market Data", s.market_data, s.tracked_assets),
            ("HLP Tracker", s.hlp_status, s.hlp_positions),
        ]
        # New data layer components
        fr_total = s.funding_rate_symbols_binance + s.funding_rate_symbols_bybit
        new_components = [
            ("Funding Rates", "ready" if fr_total > 0 else "offline", fr_total),
            ("L/S Ratios", "ready" if s.lsr_btc_ratio > 0 else "offline", 0),
            ("Orderbook", "ready" if s.orderbook_symbols > 0 else "offline", s.orderbook_symbols),
            ("Deribit IV", "ready" if s.deribit_btc_iv > 0 else "offline", 0),
            ("Spot/Basis", "ready" if s.spot_btc_basis_pct != 0 else "offline", 0),
        ]
        components.extend(new_components)
        for name, status, count in components:
            if status in ("connected", "demo", "ready"):
                icon, style = "\U0001f7e2", "bright_green"
            elif status == "reconnecting":
                icon, style = "\U0001f7e1", "yellow"
            else:
                icon, style = "\U0001f534", "red"
            lines.append(f"  {icon} {name:<16}", style=style)
            lines.append(f" {count:>8,}\n", style="bright_white")

        lines.append("\n  ALERTS:\n", style="bold bright_cyan")
        lines.append(f"  Sent: {self.hub.alerts.alerts_sent}\n", style="bright_white")
        enabled = self.hub.alerts.enabled
        lines.append(
            f"  Enabled: {'YES' if enabled else 'NO'}\n",
            style="bright_green" if enabled else "dim",
        )

        return Panel(
            lines,
            title="[bold bright_yellow]\u2699 SYSTEM[/]",
            border_style="bright_yellow", box=box.ROUNDED, padding=(0, 1),
        )


class HubSmartMoney:
    """Smart money panel showing top/bottom traders and recent signals."""

    def __init__(self, hub: HyperDataHub):
        self.hub = hub
        self.cycle = 0

    def build_compact(self) -> Panel:
        from datetime import datetime, timezone

        sm_stats = self.hub.smart_money.get_stats()
        smart = self.hub.get_smart_money(5)
        dumb = self.hub.get_dumb_money(3)
        signals = self.hub.get_smart_money_signals(8)

        # ── Stats line ──
        stats_line = Text()
        stats_line.append(f" Tracked:{sm_stats['total_wallets']:,}", style="bright_white")
        stats_line.append(f"  Ranked:{sm_stats['ranked_wallets']:,}", style="bright_yellow")
        stats_line.append(f"  Signals:{sm_stats['total_signals']:,}\n", style="bright_cyan")

        # ── Smart money table ──
        smart_table = Table(
            box=box.SIMPLE_HEAVY, border_style="bright_green",
            header_style="bold bright_white", expand=True, padding=(0, 0),
        )
        smart_table.add_column("#", style="dim", width=3)
        smart_table.add_column("WIN%", justify="right", min_width=5)
        smart_table.add_column("PnL", justify="right", min_width=8)
        smart_table.add_column("ACCT", justify="right", min_width=7)
        smart_table.add_column("COINS", min_width=8)

        if smart:
            for w in smart:
                wr_style = "bold bright_green" if w.win_rate >= 0.6 else "bright_green"
                pnl_style = "bright_green" if w.total_realized_pnl >= 0 else "bright_red"
                coins = ",".join(w.active_symbols[:3]) if w.active_symbols else "-"
                smart_table.add_row(
                    f"#{w.rank}",
                    Text(f"{w.win_rate * 100:.0f}%", style=wr_style),
                    Text(fmt_usd(w.total_realized_pnl), style=pnl_style),
                    fmt_usd(w.account_value),
                    Text(coins, style="bright_cyan"),
                )
        else:
            smart_table.add_row("---", "---", "---", "---", "---")

        # ── Dumb money table ──
        dumb_table = Table(
            box=box.SIMPLE_HEAVY, border_style="bright_red",
            header_style="bold bright_white", expand=True, padding=(0, 0),
        )
        dumb_table.add_column("#", style="dim", width=3)
        dumb_table.add_column("WIN%", justify="right", min_width=5)
        dumb_table.add_column("PnL", justify="right", min_width=8)
        dumb_table.add_column("ACCT", justify="right", min_width=7)

        if dumb:
            for w in dumb:
                wr_style = "bold bright_red" if w.win_rate < 0.4 else "bright_red"
                pnl_style = "bright_red" if w.total_realized_pnl < 0 else "bright_green"
                dumb_table.add_row(
                    f"#{w.rank}",
                    Text(f"{w.win_rate * 100:.0f}%", style=wr_style),
                    Text(fmt_usd(w.total_realized_pnl), style=pnl_style),
                    fmt_usd(w.account_value),
                )
        else:
            dumb_table.add_row("---", "---", "---", "---")

        # ── Signals feed ──
        signal_lines = Text()
        if signals:
            for sig in signals[-8:]:
                ts = datetime.fromtimestamp(sig.timestamp, tz=timezone.utc).strftime("%H:%M:%S")
                if sig.signal_type == "follow":
                    icon = ">>>"
                    sig_style = "bold bright_green"
                    label = f"FOLLOW Smart #{sig.wallet_rank}"
                else:
                    icon = "<<<"
                    sig_style = "bold bright_red"
                    label = f"FADE Dumb #{sig.wallet_rank}"

                action_style = "bright_green" if "LONG" in sig.action else "bright_red"
                signal_lines.append(f" {ts} ", style="dim")
                signal_lines.append(f"{icon} ", style=sig_style)
                signal_lines.append(f"{label} ", style=sig_style)
                signal_lines.append(f"{sig.action} ", style=action_style)
                signal_lines.append(f"{sig.symbol} ", style="bright_white")
                signal_lines.append(f"{fmt_usd(sig.size_usd)}\n", style="bold white")
        else:
            signal_lines.append("  Waiting for signals...\n", style="dim")

        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")

        smart_label = Text()
        smart_label.append(" TOP SMART MONEY\n", style="bold bright_green")
        dumb_label = Text()
        dumb_label.append(" BOTTOM DUMB MONEY\n", style="bold bright_red")
        signal_label = Text()
        signal_label.append(" SIGNALS\n", style="bold bright_cyan")

        return Panel(
            Group(stats_line, smart_label, smart_table, Text(""), dumb_label, dumb_table, Text(""), signal_label, signal_lines),
            title=f"[bold bright_yellow]\U0001f9e0 SMART MONEY  {sm_stats['smart_wallets']} smart / {sm_stats['dumb_wallets']} dumb[/]",
            subtitle=f"[dim]{now_str}  #{self.cycle}[/]",
            border_style="bright_yellow", box=box.ROUNDED, padding=(0, 1),
        )


class HubHLP:
    """HLP (Hyperliquidity Provider) panel -- reverse-engineering Hyperliquid's market maker."""

    def __init__(self, hub: HyperDataHub):
        self.hub = hub
        self.cycle = 0

    def build_compact(self) -> Panel:
        stats = self.hub.hlp.get_stats()
        snap = self.hub.hlp.get_latest_snapshot()

        if not snap:
            return Panel(
                Text("  Waiting for HLP data...", style="dim bright_yellow"),
                title="[bold bright_magenta]HLP VAULT[/]",
                border_style="bright_magenta", box=box.ROUNDED, padding=(0, 1),
            )

        # ── Header stats ──
        header = Text()
        header.append(" AUM: ", style="dim")
        header.append(f"{fmt_usd(stats['account_value'])}", style="bold bright_white")
        header.append("  PnL: ", style="dim")
        pnl = stats["session_pnl"]
        pnl_style = "bold bright_green" if pnl >= 0 else "bold bright_red"
        header.append(f"{'+'if pnl>=0 else ''}{fmt_usd(pnl)}", style=pnl_style)
        header.append(f"  Pos: {stats['num_positions']}\n", style="bright_white")

        # ── Net delta with Z-score ──
        delta_line = Text()
        delta = stats["net_delta"]
        zscore = stats["delta_zscore"]
        delta_style = "bright_green" if delta >= 0 else "bright_red"
        delta_line.append(" Net Delta: ", style="dim")
        delta_line.append(f"{'+'if delta>=0 else ''}{fmt_usd(delta)}", style=delta_style)
        delta_line.append("  Z: ", style="dim")
        if abs(zscore) > 2:
            z_style = "bold bright_red"
        elif abs(zscore) > 1:
            z_style = "bold bright_yellow"
        else:
            z_style = "bright_white"
        delta_line.append(f"{zscore:+.2f}", style=z_style)
        if abs(zscore) > 2:
            delta_line.append(" EXTREME", style="bold bright_red")
        delta_line.append(f"  Exposure: {fmt_usd(stats['total_exposure'])}\n", style="dim")

        # ── Delta sparkline (last 10 snapshots) ──
        spark_line = Text()
        history = self.hub.hlp.get_delta_history(10)
        if len(history) >= 2:
            deltas = [d for _, d in history]
            d_min = min(deltas)
            d_max = max(deltas)
            d_range = d_max - d_min if d_max != d_min else 1.0
            blocks = " _.\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
            spark_line.append(" Delta Trend: ", style="dim")
            for d in deltas:
                idx = int((d - d_min) / d_range * (len(blocks) - 1))
                idx = max(0, min(idx, len(blocks) - 1))
                style = "bright_green" if d >= 0 else "bright_red"
                spark_line.append(blocks[idx], style=style)
            spark_line.append("\n")

        # ── Top 5 positions ──
        top_pos = self.hub.hlp.get_top_positions(5)
        pos_table = Table(
            box=box.SIMPLE_HEAVY, border_style="bright_magenta",
            header_style="bold bright_white", expand=True, padding=(0, 0),
        )
        pos_table.add_column("SIDE", justify="center", width=2)
        pos_table.add_column("SYM", style="bright_white", justify="center", width=5)
        pos_table.add_column("SIZE", justify="right", min_width=8)
        pos_table.add_column("PnL", justify="right", min_width=7)
        pos_table.add_column("LV", justify="right", width=3)

        for p in top_pos:
            ss = "bold bright_green" if p.side == "long" else "bold bright_red"
            ps = "bright_green" if p.unrealized_pnl >= 0 else "bright_red"
            sign = "+" if p.unrealized_pnl >= 0 else ""
            sz_style = "bold bright_yellow" if p.size_usd >= 5_000_000 else "bright_white"
            pos_table.add_row(
                Text(p.side[0].upper(), style=ss), p.symbol,
                Text(fmt_usd(p.size_usd), style=sz_style),
                Text(f"{sign}{fmt_usd(p.unrealized_pnl)}", style=ps),
                f"{p.leverage:.0f}x",
            )

        # ── Recent liquidation absorptions ──
        liqs = self.hub.hlp.get_liquidation_absorptions(60)[-5:]
        liq_lines = Text()
        if liqs:
            liq_lines.append(" LIQ ABSORPTIONS:\n", style="bold bright_yellow")
            for t in reversed(liqs):
                ts = datetime.fromtimestamp(t.timestamp, tz=timezone.utc).strftime("%H:%M:%S")
                side_style = "bright_green" if t.side == "buy" else "bright_red"
                liq_lines.append(f" {ts}", style="dim")
                liq_lines.append(f" {t.side.upper():<4}", style=side_style)
                liq_lines.append(f" {t.symbol:<5}", style="bright_white")
                liq_lines.append(f" {fmt_usd(t.size_usd)}", style="bold white")
                liq_lines.append(f" {t.direction}\n", style="dim")
        else:
            liq_lines.append(" No liquidation absorptions yet\n", style="dim")

        # ── Footer stats ──
        footer = Text()
        footer.append(f" Snaps:{stats['total_snapshots']}", style="dim")
        footer.append(f"  Trades:{stats['total_trades']}", style="dim")
        footer.append(f"  LiqAbsorb:{stats['liquidation_absorptions']}", style="bright_yellow")

        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")

        elements = [header, delta_line]
        if len(history) >= 2:
            elements.append(spark_line)
        elements.extend([pos_table, Text(""), liq_lines, footer])

        return Panel(
            Group(*elements),
            title=f"[bold bright_magenta]HLP VAULT  {fmt_usd(stats['account_value'])} AUM  {stats['num_positions']} pos[/]",
            subtitle=f"[dim]{now_str}  #{self.cycle}[/]",
            border_style="bright_magenta", box=box.ROUNDED, padding=(0, 1),
        )


class HubMarketIntel:
    """Market Intelligence — Deribit IV, spot/perp basis, L/S ratios, cross-exchange funding."""

    def __init__(self, hub: HyperDataHub):
        self.hub = hub
        self.cycle = 0

    def build_compact(self) -> Panel:
        lines = Text()

        # ── Deribit Implied Volatility ──
        lines.append(" DERIBIT DVOL (30d IV)\n", style="bold bright_cyan")
        for sym in ["BTC", "ETH"]:
            snap = self.hub.deribit.get_latest(sym)
            if snap and snap.mark_iv > 0:
                iv_style = "bold bright_yellow" if snap.mark_iv > 70 else "bright_white"
                lines.append(f"  {sym}: ", style="dim")
                lines.append(f"{snap.mark_iv:.1f}%", style=iv_style)
                if snap.index_price > 0:
                    lines.append(f"  idx:{fmt_price(snap.index_price)}", style="dim")
                lines.append("\n")
            else:
                lines.append(f"  {sym} IV: ", style="dim")
                lines.append("--\n", style="dim")

        # ── Spot/Perp Basis ──
        lines.append("\n BASIS (perp-spot)\n", style="bold bright_green")
        for sym in ["BTC", "ETH", "SOL"]:
            snap = self.hub.spot.get_latest(sym)
            if snap:
                b = snap.basis_pct
                b_style = "bold bright_red" if abs(b) > 0.3 else ("bright_yellow" if abs(b) > 0.1 else "bright_white")
                lines.append(f"  {sym}: ", style="dim")
                lines.append(f"{b:+.3f}%", style=b_style)
                lines.append(f"  spot:{fmt_price(snap.spot_price)}", style="dim")
                lines.append(f"  perp:{fmt_price(snap.perp_price)}\n", style="dim")
            else:
                lines.append(f"  {sym}: --\n", style="dim")

        # ── Long/Short Ratios ──
        lines.append("\n L/S RATIO\n", style="bold bright_magenta")
        for sym in ["BTC", "ETH", "SOL"]:
            snap = self.hub.lsr.get_latest(sym)
            if snap:
                long_pct = snap.long_ratio * 100
                r_style = "bright_green" if snap.long_short_ratio > 1.0 else "bright_red"
                bar_filled = int(long_pct / 100 * 10)
                bar = Text()
                bar.append("\u2588" * bar_filled, style="green")
                bar.append("\u2591" * (10 - bar_filled), style="red")
                lines.append(f"  {sym} L/S: ", style="dim")
                lines.append(f"{snap.long_short_ratio:.2f}", style=r_style)
                lines.append(f"  ({long_pct:.0f}% long) ", style=r_style)
                lines.append_text(bar)
                lines.append("\n")
            else:
                lines.append(f"  {sym} L/S: --\n", style="dim")

        # ── Cross-Exchange Funding Extremes ──
        lines.append("\n FUNDING EXTREMES (annualized)\n", style="bold bright_yellow")
        # Collect all funding rates across exchanges, sort by absolute value
        all_funding: dict[str, dict[str, float]] = {}
        for ex_name, ex_rates in self.hub.funding.rates.items():
            for sym, snap in ex_rates.items():
                if sym not in all_funding:
                    all_funding[sym] = {}
                all_funding[sym][ex_name] = snap.funding_rate_annualized * 100

        # Also include HL funding from market data
        for sym, asset in list(self.hub.market.assets.items())[:30]:
            ann = asset.funding_rate * 8760 * 100
            if abs(ann) > 1.0:
                if sym not in all_funding:
                    all_funding[sym] = {}
                all_funding[sym]["hl"] = ann

        # Sort by max absolute funding across any exchange
        top_funding = sorted(
            all_funding.items(),
            key=lambda x: max(abs(v) for v in x[1].values()) if x[1] else 0,
            reverse=True,
        )[:5]

        if top_funding:
            for sym, ex_rates in top_funding:
                lines.append(f"  {sym:<8}", style="bold bright_white")
                for ex in ["hl", "binance", "bybit"]:
                    ex_label = ex[:3].upper()
                    rate = ex_rates.get(ex)
                    if rate is not None:
                        r_style = "bright_green" if rate > 0 else "bright_red"
                        lines.append(f" {ex_label}:{rate:+.0f}%", style=r_style)
                    else:
                        lines.append(f" {ex_label}:n/a", style="dim")
                lines.append("\n")
        else:
            lines.append("  Waiting for data...\n", style="dim")

        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        return Panel(
            lines,
            title="[bold bright_yellow]\U0001f4ca MARKET INTELLIGENCE[/]",
            subtitle=f"[dim]{now_str}  #{self.cycle}[/]",
            border_style="bright_yellow", box=box.ROUNDED, padding=(0, 1),
        )
