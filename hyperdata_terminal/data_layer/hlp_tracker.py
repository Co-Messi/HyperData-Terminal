"""
HLP (Hyperliquidity Provider) Reverse Engineering Module.

Monitors Hyperliquid's native market-making protocol by tracking
HLP vault addresses, their positions, trades, and behavior patterns.

HLP is a *parent* vault that holds idle USDC and allocates it to child
vaults (Strategy A/B/X, several Liquidators). The parent itself carries no
positions, so tracking only the parent address shows "0 positions, net
delta 0" forever. The child list comes from ``vaultDetails`` on the parent
(refreshed hourly, with a static fallback), and every snapshot aggregates
the parent plus all children.

Liquidation absorptions are read from the ``liquidation`` object Hyperliquid
attaches to a fill that took the other side of a liquidation
(``method: "market"`` when a strategy vault was the book counterparty,
``"backstop"`` when a Liquidator vault took the position over).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────


@dataclass
class HLPPosition:
    """A single HLP position."""
    symbol: str
    side: str               # 'long' or 'short'
    size: float             # Asset quantity (signed: + long, - short)
    size_usd: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    leverage: float


@dataclass
class HLPSnapshot:
    """Point-in-time snapshot of the entire HLP state."""
    timestamp: float
    account_value: float
    total_margin_used: float
    positions: list[HLPPosition]

    # Computed metrics
    net_delta_usd: float        # Sum of all position values (+ = net long, - = net short)
    total_exposure_usd: float   # Sum of absolute position values
    num_positions: int

    # Delta Z-score (how extreme is the current delta vs history)
    delta_zscore: float = 0.0

    # PnL tracking
    total_unrealized_pnl: float = 0.0
    session_pnl: float = 0.0    # PnL since tracking started


@dataclass
class HLPTrade:
    """An HLP trade (from fills)."""
    timestamp: float
    symbol: str
    side: str               # 'buy' or 'sell'
    price: float
    size: float
    size_usd: float
    direction: str          # 'Open Long', 'Close Short', etc.
    closed_pnl: float       # Realized PnL from this trade
    is_liquidation: bool    # Was this absorbing a liquidation?
    liquidation_method: str = ""   # "market" | "backstop" when is_liquidation
    vault: str = ""                # child vault address the fill belongs to

    @property
    def liquidated_side(self) -> str:
        """Side of the position that was liquidated (the counterparty's).

        HLP is the other side of the liquidated user's forced order: HLP
        selling means the liquidated user was forced to buy, i.e. a short
        was liquidated; HLP buying means a long was liquidated.
        """
        return "short" if self.side == "sell" else "long"


# ── Tracker ───────────────────────────────────────────────────────────────


class HLPTracker:
    """Tracks and analyzes HLP vault behavior."""

    API_URL = "https://api.hyperliquid.xyz/info"

    # The HLP parent vault. Its children are discovered via vaultDetails.
    PARENT_VAULT = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"
    # Child vaults as of 2026-10, used only if vaultDetails is unreachable.
    FALLBACK_CHILD_VAULTS = (
        "0x010461c14e146ac35fe42271bdc1134ee31c703a",  # HLP Strategy A
        "0x2e3d94f0562703b25c83308a05046ddaf9a8dd14",  # HLP Liquidator
        "0x2ed5c4484ea3ff8b57d5f2fb152a40d9f2b68308",  # HLP Liquidator 4
        "0x31ca8395cf837de08b24da3f660e77761dfb974b",  # HLP Strategy B
        "0x469f690213c467c39a23efacfd2816896009d7d8",  # HLP Strategy X
        "0x5e177e5e39c0f4e421f5865a6d8beed8d921cb70",  # HLP Liquidator 3
        "0xb0a55f13d22f66e6d495ac98113841b2326e9540",  # HLP Liquidator 2
    )
    # Kept for callers that read the old mapping; the parent alone has no positions.
    HLP_VAULTS = {"main": PARENT_VAULT}

    SNAPSHOT_INTERVAL = 30      # Take snapshot every 30 seconds
    # userFills costs 20 weight per vault on Hyperliquid's 1200/min budget,
    # shared with the position scanner; two minutes keeps HLP well under 10%.
    FILLS_INTERVAL = 120
    CHILD_REFRESH_INTERVAL = 3600
    ZSCORE_WINDOW = 100         # Use last 100 snapshots for Z-score
    # userFills returns each vault's last 2000 fills: days of history on the
    # first poll. Those seed the display history but fire no callbacks, or
    # every start would push ~12k old rows through persistence (duplicating
    # them on each restart) and the hub's liquidation stream. Fills up to
    # this long before start still notify, to cover a quick restart.
    NOTIFY_BACKFILL_SECONDS = 120

    def __init__(self) -> None:
        self.snapshots: deque[HLPSnapshot] = deque(maxlen=2000)  # ~16 hours at 30s
        self.trades: deque[HLPTrade] = deque(maxlen=5000)
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._tasks: list[asyncio.Task] = []
        # Per vault: the tids of the latest userFills response. The endpoint
        # returns the most recent fills, so a tid that has dropped out of the
        # response can never come back; keeping exactly the latest set is
        # both bounded and duplicate free (a global set pruned by size
        # re-admitted still-returned fills as "new" once 6+ vaults were polled).
        self._seen_fill_tids: dict[str, set[int]] = {}
        self._callbacks: list = []
        self._session_start_value: float = 0.0
        self.child_vaults: list[str] = list(self.FALLBACK_CHILD_VAULTS)
        self._children_refreshed_at: float = 0.0
        self._started_at: float = 0.0  # 0 = not started: every fill notifies (tests, ad hoc use)

    @property
    def vault_addresses(self) -> list[str]:
        return [self.PARENT_VAULT, *self.child_vaults]

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._started_at = time.time()
        self._session = aiohttp.ClientSession()
        self._tasks = [
            asyncio.create_task(self._snapshot_loop(), name="hlp-snapshot"),
            asyncio.create_task(self._fills_loop(), name="hlp-fills"),
        ]

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def on_hlp_trade(self, callback) -> None:
        """Register callback for HLP trades (especially liquidation absorptions)."""
        self._callbacks.append(callback)

    async def _post(self, payload: dict):
        """POST to the info endpoint; None on any failure (logged)."""
        if self._session is None or self._session.closed:
            return None
        try:
            async with self._session.post(
                self.API_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning("[hlp] %s HTTP %d", payload.get("type"), resp.status)
                    return None
                return await resp.json()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[hlp] %s request failed", payload.get("type"))
            return None

    async def refresh_child_vaults(self) -> None:
        """Re-read the parent's child vault list (falls back to the last known list)."""
        data = await self._post({"type": "vaultDetails", "vaultAddress": self.PARENT_VAULT})
        children = (
            (data or {}).get("relationship", {}).get("data", {}).get("childAddresses")
            if isinstance(data, dict) else None
        )
        if isinstance(children, list) and children:
            self.child_vaults = [str(c).lower() for c in children if isinstance(c, str)]
        self._children_refreshed_at = time.time()

    # ── Snapshot Loop ────────────────────────────────────────

    async def _snapshot_loop(self) -> None:
        """Periodically snapshot the whole HLP (parent plus every child vault)."""
        while self._running:
            try:
                if time.time() - self._children_refreshed_at > self.CHILD_REFRESH_INTERVAL:
                    await self.refresh_child_vaults()
                snapshot = await self._take_aggregate_snapshot()
                if snapshot:
                    snapshot.delta_zscore = self._compute_delta_zscore(snapshot.net_delta_usd)
                    self.snapshots.append(snapshot)

                    if self._session_start_value == 0:
                        self._session_start_value = snapshot.account_value
                    snapshot.session_pnl = snapshot.account_value - self._session_start_value
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[hlp] snapshot error")
            await asyncio.sleep(self.SNAPSHOT_INTERVAL)

    async def _take_aggregate_snapshot(self) -> HLPSnapshot | None:
        """One snapshot summing every HLP vault; None unless every vault answered.

        A partial sum would show a fake drop in AUM and delta whenever one
        child request fails, so a pass with any failure is discarded.
        """
        states = []
        for address in self.vault_addresses:
            state = await self._post({"type": "clearinghouseState", "user": address})
            if not isinstance(state, dict):
                return None
            states.append(state)
        return self.build_snapshot(states)

    async def _take_snapshot(self, address: str) -> HLPSnapshot | None:
        """Snapshot of a single vault (kept for ad hoc inspection)."""
        state = await self._post({"type": "clearinghouseState", "user": address})
        return self.build_snapshot([state]) if isinstance(state, dict) else None

    @staticmethod
    def build_snapshot(states: list[dict]) -> HLPSnapshot:
        """Aggregate clearinghouseState payloads into one HLP snapshot.

        Positions in the same coin held by different child vaults are merged
        (signed size and value summed, entry price size weighted), because
        to the market HLP is one book.
        """
        account_value = 0.0
        total_margin_used = 0.0
        merged: dict[str, dict] = {}

        for data in states:
            margin_summary = data.get("marginSummary", {}) or {}
            account_value += float(margin_summary.get("accountValue", 0) or 0)
            total_margin_used += float(margin_summary.get("totalMarginUsed", 0) or 0)

            for pos_data in data.get("assetPositions", []) or []:
                pos_info = pos_data.get("position", pos_data)
                symbol = pos_info.get("coin", "")
                size_raw = float(pos_info.get("szi", 0) or 0)
                if size_raw == 0 or not symbol:
                    continue
                value = abs(float(pos_info.get("positionValue", 0) or 0))
                signed_value = value if size_raw > 0 else -value
                entry_price = float(pos_info.get("entryPx", 0) or 0)
                leverage_info = pos_info.get("leverage", {})
                if isinstance(leverage_info, dict):
                    leverage = float(leverage_info.get("value", 1) or 1)
                else:
                    leverage = float(leverage_info) if leverage_info else 1.0

                m = merged.setdefault(symbol, {
                    "size": 0.0, "signed_value": 0.0, "entry_notional": 0.0,
                    "abs_size": 0.0, "pnl": 0.0, "leverage": 0.0,
                })
                m["size"] += size_raw
                m["signed_value"] += signed_value
                m["entry_notional"] += entry_price * abs(size_raw)
                m["abs_size"] += abs(size_raw)
                m["pnl"] += float(pos_info.get("unrealizedPnl", 0) or 0)
                m["leverage"] = max(m["leverage"], leverage)

        positions: list[HLPPosition] = []
        net_delta_usd = 0.0
        total_exposure_usd = 0.0
        total_unrealized_pnl = 0.0
        for symbol, m in merged.items():
            if m["size"] == 0:
                continue  # child vaults netted each other out
            size_usd = abs(m["signed_value"])
            side = "long" if m["size"] > 0 else "short"
            positions.append(HLPPosition(
                symbol=symbol,
                side=side,
                size=m["size"],
                size_usd=size_usd,
                entry_price=m["entry_notional"] / m["abs_size"] if m["abs_size"] else 0.0,
                current_price=size_usd / abs(m["size"]),
                unrealized_pnl=m["pnl"],
                leverage=m["leverage"] or 1.0,
            ))
            net_delta_usd += m["signed_value"]
            total_exposure_usd += size_usd
            total_unrealized_pnl += m["pnl"]

        return HLPSnapshot(
            timestamp=time.time(),
            account_value=account_value,
            total_margin_used=total_margin_used,
            positions=positions,
            net_delta_usd=net_delta_usd,
            total_exposure_usd=total_exposure_usd,
            num_positions=len(positions),
            total_unrealized_pnl=total_unrealized_pnl,
        )

    # ── Fills Loop ───────────────────────────────────────────

    async def _fills_loop(self) -> None:
        """Periodically fetch fills of every child vault to catch liquidation absorptions."""
        while self._running:
            try:
                for address in self.child_vaults:
                    await self._fetch_fills(address)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[hlp] fills error")
            await asyncio.sleep(self.FILLS_INTERVAL)

    async def _fetch_fills(self, address: str) -> None:
        """Fetch a vault's recent fills and process the ones not seen before."""
        fills = await self._post({"type": "userFills", "user": address})
        if not isinstance(fills, list):
            return
        self.process_fills(address, fills)

    def process_fills(self, address: str, fills: list[dict]) -> list[HLPTrade]:
        """Turn a userFills response into HLPTrades; returns the new ones."""
        seen = self._seen_fill_tids.get(address, set())
        current: set[int] = set()
        new_trades: list[HLPTrade] = []

        # The API lists newest first; process oldest first so callbacks and
        # the trades deque stay in time order.
        for fill in reversed(fills):
            if not isinstance(fill, dict):
                continue
            tid = fill.get("tid", 0)
            current.add(tid)
            if tid in seen:
                continue

            try:
                trade = self._parse_fill(fill, address)
            except (TypeError, ValueError):
                logger.debug("[hlp] unparseable fill: %.200s", fill)
                continue
            self.trades.append(trade)
            new_trades.append(trade)

            if self._started_at and trade.timestamp < self._started_at - self.NOTIFY_BACKFILL_SECONDS:
                continue  # history from the first poll: display only
            for cb in self._callbacks:
                try:
                    cb(trade)
                except Exception:
                    logger.exception("[hlp] trade callback error")

        self._seen_fill_tids[address] = current
        return new_trades

    @staticmethod
    def _parse_fill(fill: dict, address: str) -> HLPTrade:
        side = str(fill.get("side", "")).lower()  # 'A' (ask/sell) or 'B' (bid/buy)
        if side in ("a", "sell"):
            side = "sell"
        elif side in ("b", "buy"):
            side = "buy"

        price = float(fill.get("px", 0))
        size = float(fill.get("sz", 0))
        fill_time = fill.get("time", 0)
        if isinstance(fill_time, (int, float)) and fill_time > 1e12:
            fill_time = fill_time / 1000.0  # ms to seconds
        elif isinstance(fill_time, (int, float)) and fill_time > 0:
            fill_time = float(fill_time)
        else:
            fill_time = time.time()

        # Hyperliquid marks the fill itself: a `liquidation` object with the
        # liquidated user, mark price and method. No guessing from `crossed`
        # or from zero closed PnL (both are true of ordinary fills).
        liquidation = fill.get("liquidation")
        is_liquidation = isinstance(liquidation, dict)
        method = str(liquidation.get("method", "")) if is_liquidation else ""

        return HLPTrade(
            timestamp=fill_time,
            symbol=str(fill.get("coin", "")),
            side=side,
            price=price,
            size=size,
            size_usd=price * size,
            direction=str(fill.get("dir", "")),
            closed_pnl=float(fill.get("closedPnl", 0) or 0),
            is_liquidation=is_liquidation,
            liquidation_method=method,
            vault=address,
        )

    # ── Z-Score ──────────────────────────────────────────────

    def _compute_delta_zscore(self, current_delta: float) -> float:
        """Compute Z-score of current net delta vs historical values."""
        if len(self.snapshots) < 10:
            return 0.0
        import numpy as np
        deltas = [s.net_delta_usd for s in self.snapshots][-self.ZSCORE_WINDOW:]
        mean = np.mean(deltas)
        std = np.std(deltas)
        if std == 0:
            return 0.0
        return float((current_delta - mean) / std)

    # ── Queries ──────────────────────────────────────────────

    def get_latest_snapshot(self) -> HLPSnapshot | None:
        return self.snapshots[-1] if self.snapshots else None

    def get_positions(self) -> list[HLPPosition]:
        snap = self.get_latest_snapshot()
        return snap.positions if snap else []

    def get_top_positions(self, n: int = 10) -> list[HLPPosition]:
        positions = self.get_positions()
        return sorted(positions, key=lambda p: abs(p.size_usd), reverse=True)[:n]

    def get_recent_trades(self, n: int = 50) -> list[HLPTrade]:
        return list(self.trades)[-n:]

    def get_liquidation_absorptions(self, minutes: int = 60) -> list[HLPTrade]:
        """Get trades where HLP absorbed a liquidation."""
        cutoff = time.time() - minutes * 60
        return [t for t in self.trades if t.is_liquidation and t.timestamp > cutoff]

    def get_delta_history(self, n: int = 100) -> list[tuple[float, float]]:
        """Return (timestamp, net_delta_usd) pairs for charting."""
        return [(s.timestamp, s.net_delta_usd) for s in list(self.snapshots)[-n:]]

    def get_stats(self) -> dict:
        snap = self.get_latest_snapshot()
        return {
            "account_value": snap.account_value if snap else 0,
            "net_delta": snap.net_delta_usd if snap else 0,
            "delta_zscore": snap.delta_zscore if snap else 0,
            "total_exposure": snap.total_exposure_usd if snap else 0,
            "num_positions": snap.num_positions if snap else 0,
            "session_pnl": snap.session_pnl if snap else 0,
            "total_unrealized_pnl": snap.total_unrealized_pnl if snap else 0,
            "total_snapshots": len(self.snapshots),
            "total_trades": len(self.trades),
            "liquidation_absorptions": sum(1 for t in self.trades if t.is_liquidation),
        }
