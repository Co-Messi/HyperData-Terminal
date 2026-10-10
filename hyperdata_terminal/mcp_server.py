"""MCP server: HyperData's live market data as tools for AI agents.

    hyperdata mcp          # stdio transport (what Claude Code, Claude Desktop,
                           # Cursor and most MCP clients launch)

The server starts one HyperDataHub (the same live feeds the terminal uses)
and answers tool calls from its in-memory state, so calls are instant and
cost no exchange requests. Everything is read-only: there is no trading,
no keys and no account access.

Data needs a short warmup after launch (markets ~5s, whale positions ~30s,
order-flow windows fill over their own length). Every result carries a
``meta`` block with uptime and per-source readiness so an agent can tell
"nothing happened" from "not loaded yet".

Requires the optional extra: ``pip install "hyperdata-terminal[mcp]"``.
"""
from __future__ import annotations

import math
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

from hyperdata_terminal import __version__

INSTRUCTIONS = """\
Live crypto derivatives data from Hyperliquid, Binance, Bybit, OKX and Deribit,
read from public feeds by a local HyperData hub. Read-only.

Good first calls: get_market_overview (what is moving), get_liquidation_heatmap
(where leveraged positions get liquidated), get_positions_near_liquidation,
get_whale_positions, get_order_flow (who is aggressing), get_liquidations.

Every result has `meta`: check meta.warnings before concluding that something is
absent. Liquidations marked estimated are Hyperliquid large prints, not confirmed
liquidations. Whale and liquidation-distance data cover the Hyperliquid wallets
the hub has discovered, not every account.
"""


def _num(x: Any, digits: int = 2) -> float | None:
    """JSON-safe rounding: inf/nan become None."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, digits)


class HubTools:
    """Tool implementations over a running hub; return plain JSON-able dicts.

    Kept separate from the MCP wiring so they can be tested with a fake hub.
    """

    def __init__(self, hub) -> None:
        self.hub = hub

    # ── shared ───────────────────────────────────────────────────────

    def meta(self) -> dict[str, Any]:
        hub = self.hub
        started = getattr(hub.status, "started_at", 0.0) or 0.0
        uptime = time.time() - started if started else 0.0
        warnings: list[str] = []
        if not hub.market.assets:
            warnings.append("market data not loaded yet (first ~5s)")
        if not hub.positions.positions:
            warnings.append("whale/position scan not complete yet (first ~30s)")
        if hub.hlp.get_latest_snapshot() is None:
            warnings.append("HLP vault snapshot pending")
        return {
            "source": f"hyperdata-terminal {__version__}",
            "uptime_seconds": round(uptime, 1),
            "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "warnings": warnings,
        }

    def _with_meta(self, payload: dict[str, Any], warnings: list[str] | None = None) -> dict[str, Any]:
        meta = self.meta()
        if warnings:
            meta["warnings"] = [*warnings, *meta["warnings"]]
        return {**payload, "meta": meta}

    @staticmethod
    def _sym(symbol: str) -> str:
        return (symbol or "").strip().upper()

    @staticmethod
    def _position(p) -> dict[str, Any]:
        return {
            "address": p.address,
            "symbol": p.symbol,
            "side": p.side,
            "size_usd": _num(p.size_usd, 0),
            "entry_price": _num(p.entry_price, 6),
            "mark_price": _num(p.current_price, 6),
            # null when Hyperliquid reports no liquidation price for the
            # position (account equity covers it at any price).
            "liquidation_price": _num(p.liq_price, 6),
            # Signed: negative means the price has crossed the liquidation price.
            "distance_to_liquidation_pct": _num(p.distance_pct, 3),
            "margin_mode": getattr(p, "margin_mode", "") or None,
            "leverage": _num(p.leverage, 1),
            "unrealized_pnl_usd": _num(p.unrealized_pnl, 0),
        }

    # ── tools ────────────────────────────────────────────────────────

    def market_overview(self, limit: int = 20, sort_by: str = "open_interest") -> dict[str, Any]:
        keys = {
            "open_interest": lambda a: a.open_interest,
            "volume": lambda a: a.volume_24h,
            "change": lambda a: abs(a.price_change_24h_pct),
            "funding": lambda a: abs(a.funding_rate),
        }
        key = keys.get(sort_by, keys["open_interest"])
        assets = sorted(self.hub.market.assets.values(), key=key, reverse=True)[: max(1, min(limit, 100))]
        return self._with_meta({
            "venue": "hyperliquid",
            "sorted_by": sort_by if sort_by in keys else "open_interest",
            "total_assets": len(self.hub.market.assets),
            "assets": [
                {
                    "symbol": a.symbol,
                    "price": _num(a.price, 6),
                    "change_24h_pct": _num(a.price_change_24h_pct * 100, 2),
                    "funding_hourly_pct": _num(a.funding_rate * 100, 5),
                    "funding_annualized_pct": _num(a.funding_rate * 8760 * 100, 1),
                    "open_interest_usd": _num(a.open_interest, 0),
                    "volume_24h_usd": _num(a.volume_24h, 0),
                    "premium_pct": _num(getattr(a, "premium_pct", 0.0), 3),
                }
                for a in assets
            ],
        })

    def asset(self, symbol: str) -> dict[str, Any]:
        sym = self._sym(symbol)
        hub = self.hub
        a = hub.market.assets.get(sym)
        if a is None:
            return self._with_meta({"symbol": sym, "error": f"{sym} is not listed on Hyperliquid (or not loaded yet)"})
        funding = {"hyperliquid_annualized_pct": _num(a.funding_rate * 8760 * 100, 1)}
        for ex, rates in hub.funding.rates.items():
            snap = rates.get(sym)
            if snap is not None:
                funding[f"{ex}_annualized_pct"] = _num(snap.funding_rate_annualized * 100, 1)
        out: dict[str, Any] = {
            "symbol": sym,
            "price": _num(a.price, 6),
            "change_24h_pct": _num(a.price_change_24h_pct * 100, 2),
            "open_interest_usd": _num(a.open_interest, 0),
            "volume_24h_usd": _num(a.volume_24h, 0),
            "funding": funding,
        }
        lsr = hub.lsr.get_latest(sym)
        if lsr is not None:
            out["long_short_account_ratio"] = {
                "ratio": _num(lsr.long_short_ratio, 3),
                "long_pct": _num(lsr.long_ratio * 100, 1),
                "venue": getattr(lsr, "source", "binance"),
            }
        spot = hub.spot.get_latest(sym)
        if spot is not None:
            out["basis"] = {
                "basis_pct": _num(spot.basis_pct, 4),
                "spot_price": _num(spot.spot_price, 6),
                "spot_venue": getattr(spot, "source", "binance"),
            }
        if sym in hub.orderflow.buckets:
            out["order_flow"] = self._order_flow_frames(sym)
        near = [p for p in hub.positions.positions if p.symbol == sym and p.near_liquidation(5)]
        out["tracked_positions_within_5pct_of_liquidation"] = {
            "count": len(near),
            "long_usd": _num(sum(p.size_usd for p in near if p.side == "long"), 0),
            "short_usd": _num(sum(p.size_usd for p in near if p.side != "long"), 0),
        }
        return self._with_meta(out)

    def liquidations(
        self, minutes: int = 60, symbol: str | None = None, include_estimated: bool = False, limit: int = 25,
    ) -> dict[str, Any]:
        feed = self.hub.liquidations
        minutes = max(1, min(int(minutes), feed.MAX_WINDOW_MINUTES))
        sym = self._sym(symbol) if symbol else None
        # Totals, breakdowns and the event list all respect the symbol filter.
        stats = feed.get_stats(window_minutes=minutes, include_estimated=include_estimated, symbol=sym)
        events = feed.get_recent(minutes=minutes, symbol=sym, include_estimated=include_estimated)
        warnings = []
        if stats["window_coverage"] < 0.999:
            since = time.strftime("%H:%M UTC", time.gmtime(stats["covered_since"]))
            reason = "the event buffer filled up" if stats["truncated"] else "the hub started"
            warnings.append(
                f"liquidation totals cover {stats['window_coverage']:.0%} of the {minutes} minute window "
                f"(since {since}, when {reason}); they are not a full {minutes} minute total"
            )
        return self._with_meta({
            "window_minutes": minutes,
            "window_coverage": _num(stats["window_coverage"], 4),
            "covered_since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stats["covered_since"])),
            "truncated": stats["truncated"],
            "symbol": self._sym(symbol) if symbol else "ALL",
            "totals": {
                "count": stats["total_count"],
                "volume_usd": _num(stats["total_volume_usd"], 0),
                "long_liquidated_usd": _num(stats["long_volume_usd"], 0),
                "short_liquidated_usd": _num(stats["short_volume_usd"], 0),
                "includes_estimated": include_estimated,
            },
            "estimated_hyperliquid_large_prints": {
                "count": stats["heuristic_count"],
                "volume_usd": _num(stats["heuristic_volume_usd"], 0),
                "note": "trades >= $10K on Hyperliquid; mostly ordinary trades, not confirmed liquidations",
            },
            "by_exchange": {
                ex: {"count": v["count"], "volume_usd": _num(v["volume_usd"], 0), "coverage": v["method"]}
                for ex, v in stats["by_exchange"].items()
            },
            "coverage": {ex: c["note"] for ex, c in stats["coverage"].items()},
            "recent": [
                {
                    "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e.timestamp)),
                    "exchange": e.exchange,
                    "symbol": e.symbol,
                    "side_liquidated": e.side,
                    "size_usd": _num(e.size_usd, 0),
                    "price": _num(e.price, 6),
                    "confirmed": getattr(e, "confirmed", True),
                }
                for e in events[: max(1, min(limit, 200))]
            ],
        }, warnings)

    def liquidation_heatmap(self, symbol: str = "BTC", buckets: int = 24, range_pct: float = 10.0) -> dict[str, Any]:
        from hyperdata_terminal.dashboards.liquidation_heatmap import compute_heatmap_buckets

        sym = self._sym(symbol)
        asset = self.hub.market.assets.get(sym)
        price = asset.price if asset else self.hub.positions.market_prices.get(sym, 0.0)
        if not price:
            return self._with_meta({"symbol": sym, "error": f"no price for {sym} yet"})
        buckets = max(4, min(int(buckets), 100))
        range_pct = max(1.0, min(float(range_pct), 50.0))
        rows = compute_heatmap_buckets(
            self.hub.positions.positions, price, symbol=sym, n_buckets=buckets, range_pct=range_pct,
        )
        levels = [
            {
                "price_low": _num(b.price_low, 6),
                "price_high": _num(b.price_high, 6),
                "longs_liquidated_usd": _num(b.long_usd, 0),
                "shorts_liquidated_usd": _num(b.short_usd, 0),
                "positions": b.long_count + b.short_count,
            }
            for b in rows if b.total_usd > 0
        ]
        below = sum(b.long_usd for b in rows if b.mid < price)
        above = sum(b.short_usd for b in rows if b.mid >= price)
        biggest = sorted(rows, key=lambda b: b.total_usd, reverse=True)[:3]
        return self._with_meta({
            "symbol": sym,
            "price": _num(price, 6),
            "range_pct": range_pct,
            "long_liquidations_below_price_usd": _num(below, 0),
            "short_liquidations_above_price_usd": _num(above, 0),
            "largest_clusters": [
                {"price_mid": _num(b.mid, 6), "total_usd": _num(b.total_usd, 0),
                 "side": "longs" if b.long_usd >= b.short_usd else "shorts"}
                for b in biggest if b.total_usd > 0
            ],
            "levels_high_to_low": levels,
            "note": "built from the Hyperliquid wallets the hub tracks; a lower bound on real liquidation risk",
        })

    def whale_positions(
        self, min_size_usd: float = 1_000_000, symbol: str | None = None, limit: int = 20,
    ) -> dict[str, Any]:
        positions = self.hub.get_whale_positions(min_size_usd=max(0.0, float(min_size_usd)))
        if symbol:
            positions = [p for p in positions if p.symbol == self._sym(symbol)]
        positions = positions[: max(1, min(limit, 100))]
        return self._with_meta({
            "venue": "hyperliquid",
            "min_size_usd": min_size_usd,
            "count": len(positions),
            "long_usd": _num(sum(p.size_usd for p in positions if p.side == "long"), 0),
            "short_usd": _num(sum(p.size_usd for p in positions if p.side != "long"), 0),
            "positions": [self._position(p) for p in positions],
            "scan_age_seconds": _num(self.hub.positions.oldest_position_age_seconds(), 0),
        })

    def near_liquidation(
        self, max_distance_pct: float = 2.0, symbol: str | None = None, limit: int = 20,
    ) -> dict[str, Any]:
        cap = max(0.01, float(max_distance_pct))
        everything = self.hub.get_all_positions_sorted()
        if symbol:
            everything = [p for p in everything if p.symbol == self._sym(symbol)]
        # Only positions with a real liquidation price the price has not yet
        # crossed. A crossed one is being (or was) liquidated and its cached
        # state is out of date: counted, never listed as "near".
        positions = [p for p in everything if p.near_liquidation(cap)]
        crossed = [p for p in everything if p.crossed]
        return self._with_meta({
            "venue": "hyperliquid",
            "max_distance_pct": cap,
            "count": len(positions),
            "crossed_liquidation_price": {
                "count": len(crossed),
                "size_usd": _num(sum(p.size_usd for p in crossed), 0),
                "note": "price is past these positions' liquidation price; they are likely already "
                        "liquidated and drop out when their wallet is next scanned",
            },
            "long_usd": _num(sum(p.size_usd for p in positions if p.side == "long"), 0),
            "short_usd": _num(sum(p.size_usd for p in positions if p.side != "long"), 0),
            "positions": [self._position(p) for p in positions[: max(1, min(limit, 100))]],
        })

    def _order_flow_frames(self, sym: str) -> dict[str, Any]:
        return {
            tf: {
                "buy_usd": _num(s.buy_volume, 0),
                "sell_usd": _num(s.sell_volume, 0),
                "net_usd": _num(s.buy_volume - s.sell_volume, 0),
                "imbalance": _num(s.ofi, 3),
                "signal": s.signal,
                "window_coverage": _num(s.coverage, 3),
            }
            for tf, s in self.hub.orderflow.get_all_snapshots(sym).items()
        }

    def order_flow(self, symbol: str = "BTC") -> dict[str, Any]:
        sym = self._sym(symbol)
        engine = self.hub.orderflow
        if sym not in engine.buckets:
            return self._with_meta({
                "symbol": sym,
                "error": f"{sym} is not streamed; order flow covers: {', '.join(sorted(engine.buckets))}",
            })
        cvd = engine.get_cumulative_cvd(sym)
        return self._with_meta({
            "symbol": sym,
            "aggregate_signal": engine.display_signal(sym),
            "cumulative_cvd_usd": {k: _num(v, 0) for k, v in cvd.items()},
            "venues": engine.venue_coverage(),
            "timeframes": self._order_flow_frames(sym),
            "note": "window_coverage < 0.95 means the timeframe is still filling after start",
        })

    def hlp_vault(self, top: int = 10) -> dict[str, Any]:
        stats = self.hub.hlp.get_stats()
        top_pos = self.hub.hlp.get_top_positions(max(1, min(top, 50)))
        absorptions = self.hub.hlp.get_liquidation_absorptions(60)
        aum_source = stats.get("aum_source") or ""
        return self._with_meta({
            "aum_usd": _num(stats["account_value"], 0),
            # "vaultDetails" = what Hyperliquid reports. "clearinghouseState" = a
            # fallback sum that misses Strategy X (~$100M); treat it as a floor.
            "aum_source": aum_source,
            "aum_is_partial": aum_source != "vaultDetails",
            # From Hyperliquid's cumulative PnL series, not the AUM change
            # (deposits and withdrawals move AUM far more than PnL does).
            "session_pnl_usd": _num(stats["session_pnl"], 0) if stats.get("pnl_known") else None,
            "net_delta_usd": _num(stats["net_delta"], 0),
            "net_delta_zscore": _num(stats["delta_zscore"], 2),
            "gross_exposure_usd": _num(stats["total_exposure"], 0),
            "positions": stats["num_positions"],
            "unrealized_pnl_usd": _num(stats["total_unrealized_pnl"], 0),
            "top_positions": [
                {"symbol": p.symbol, "side": p.side, "size_usd": _num(p.size_usd, 0),
                 "unrealized_pnl_usd": _num(p.unrealized_pnl, 0)}
                for p in top_pos
            ],
            "liquidations_absorbed_last_hour": {
                "count": len(absorptions),
                "volume_usd": _num(sum(t.size_usd for t in absorptions), 0),
            },
        })

    def funding_extremes(self, min_annualized_pct: float = 50.0, limit: int = 15) -> dict[str, Any]:
        threshold = abs(float(min_annualized_pct)) / 100
        rows = []
        for a in self.hub.get_extreme_funding(threshold_annualized=threshold)[: max(1, min(limit, 100))]:
            row = {"symbol": a.symbol, "hyperliquid_annualized_pct": _num(a.funding_rate * 8760 * 100, 1)}
            for ex, rates in self.hub.funding.rates.items():
                snap = rates.get(a.symbol)
                if snap is not None:
                    row[f"{ex}_annualized_pct"] = _num(snap.funding_rate_annualized * 100, 1)
            rows.append(row)
        return self._with_meta({
            "min_annualized_pct": abs(float(min_annualized_pct)),
            "positive_means": "longs pay shorts",
            "assets": rows,
        })

    def data_health(self) -> dict[str, Any]:
        result = self.hub.health.latest()
        return self._with_meta({
            "overall": result["overall"] if result else "initializing",
            "checks": [
                {"name": c["name"], "status": c["status"], "detail": c["detail"]}
                for c in (result or {}).get("checks", [])
            ],
            "order_flow_venues": self.hub.orderflow.venue_coverage(),
            "spot_source": getattr(self.hub.spot, "active_source", None),
            "long_short_source": getattr(self.hub.lsr, "active_source", None),
        })


def build_server(hub_factory=None):
    """Create the MCP server; the hub starts in the server's lifespan."""
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    if hub_factory is None:
        from hyperdata_terminal.data_layer.hub import HyperDataHub

        hub_factory = HyperDataHub

    state: dict[str, HubTools] = {}

    @asynccontextmanager
    async def lifespan(_server):
        hub = hub_factory()
        await hub.start()
        state["tools"] = HubTools(hub)
        try:
            yield {}
        finally:
            await hub.stop()

    server = MCPServer(
        name="hyperdata",
        title="HyperData Terminal",
        instructions=INSTRUCTIONS,
        version=__version__,
        website_url="https://github.com/Co-Messi/HyperData-Terminal",
        lifespan=lifespan,
    )
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)

    def tools() -> HubTools:
        return state["tools"]

    # Every tool is `async def` on purpose. The MCP SDK runs a plain `def` tool
    # on a worker thread, where it would iterate deques and mutate CVD buckets
    # while the hub's event loop writes to them ("deque mutated during
    # iteration", lost updates in the running sums). An async tool with no
    # awaits runs to completion on the loop thread, atomically.

    @server.tool(annotations=read_only)
    async def get_market_overview(limit: int = 20, sort_by: str = "open_interest") -> dict[str, Any]:
        """Hyperliquid perps ranked by open_interest, volume, change (abs 24h move) or funding.

        Returns price, 24h change, hourly and annualized funding, OI, volume and mark premium.
        """
        return tools().market_overview(limit=limit, sort_by=sort_by)

    @server.tool(annotations=read_only)
    async def get_asset(symbol: str) -> dict[str, Any]:
        """Everything known about one asset: price, OI, funding on every venue, long/short account
        ratio, spot basis, order flow by timeframe and tracked positions near liquidation."""
        return tools().asset(symbol)

    @server.tool(annotations=read_only)
    async def get_liquidations(
        minutes: int = 60, symbol: str | None = None, include_estimated: bool = False, limit: int = 25,
    ) -> dict[str, Any]:
        """Liquidations across Binance, Bybit, OKX and Hyperliquid over the last N minutes (max 1440):
        totals, long vs short, per-exchange coverage notes and the most recent events.
        Hyperliquid large prints (estimated, usually ordinary trades) are excluded unless include_estimated."""
        return tools().liquidations(minutes=minutes, symbol=symbol, include_estimated=include_estimated, limit=limit)

    @server.tool(annotations=read_only)
    async def get_liquidation_heatmap(
        symbol: str = "BTC", buckets: int = 24, range_pct: float = 10.0,
    ) -> dict[str, Any]:
        """Where tracked Hyperliquid positions get liquidated: USD of longs (below price) and shorts
        (above price) per price level within +/- range_pct, plus the largest clusters."""
        return tools().liquidation_heatmap(symbol=symbol, buckets=buckets, range_pct=range_pct)

    @server.tool(annotations=read_only)
    async def get_whale_positions(
        min_size_usd: float = 1_000_000, symbol: str | None = None, limit: int = 20,
    ) -> dict[str, Any]:
        """Largest open Hyperliquid positions (address, side, size, entry, liquidation price, PnL, leverage)."""
        return tools().whale_positions(min_size_usd=min_size_usd, symbol=symbol, limit=limit)

    @server.tool(annotations=read_only)
    async def get_positions_near_liquidation(max_distance_pct: float = 2.0, symbol: str | None = None,
                                       limit: int = 20) -> dict[str, Any]:
        """Tracked Hyperliquid positions within max_distance_pct of their liquidation price, closest first."""
        return tools().near_liquidation(max_distance_pct=max_distance_pct, symbol=symbol, limit=limit)

    @server.tool(annotations=read_only)
    async def get_order_flow(symbol: str = "BTC") -> dict[str, Any]:
        """Cumulative volume delta and buy/sell imbalance for 1m to 24h windows, per venue attribution."""
        return tools().order_flow(symbol)

    @server.tool(annotations=read_only)
    async def get_hlp_vault(top: int = 10) -> dict[str, Any]:
        """Hyperliquid's HLP market maker vault: AUM, net delta and its z-score, top positions and the
        liquidations it absorbed in the last hour."""
        return tools().hlp_vault(top=top)

    @server.tool(annotations=read_only)
    async def get_funding_extremes(min_annualized_pct: float = 50.0, limit: int = 15) -> dict[str, Any]:
        """Assets whose Hyperliquid funding exceeds min_annualized_pct (either sign), with Binance and
        Bybit funding for comparison."""
        return tools().funding_extremes(min_annualized_pct=min_annualized_pct, limit=limit)

    @server.tool(annotations=read_only)
    async def get_data_health() -> dict[str, Any]:
        """Self-verification report: cross-checks against external sources, feed freshness and which
        venue each fallback source is using. Call this before trusting a surprising number."""
        return tools().data_health()

    return server


def serve() -> None:
    try:
        import mcp  # noqa: F401
    except ImportError:
        print(
            "hyperdata mcp needs the MCP SDK:\n"
            '    pipx install "hyperdata-terminal[mcp]"   or   pip install "hyperdata-terminal[mcp]"',
            file=sys.stderr,
        )
        sys.exit(1)
    build_server().run("stdio")
