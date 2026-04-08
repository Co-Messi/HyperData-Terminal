"""
HLP (Hyperliquidity Provider) Reverse Engineering Module.

Monitors Hyperliquid's native market-making protocol by tracking
HLP vault addresses, their positions, trades, and behavior patterns.

What Moon Dev calls "data nowhere else" -- we compute it ourselves.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

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


# ── Tracker ───────────────────────────────────────────────────────────────


class HLPTracker:
    """Tracks and analyzes HLP vault behavior."""

    API_URL = "https://api.hyperliquid.xyz/info"

    # Known HLP vault addresses
    HLP_VAULTS = {
        "main": "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303",
        # Add more vault addresses if discovered
    }

    SNAPSHOT_INTERVAL = 30      # Take snapshot every 30 seconds
    FILLS_INTERVAL = 60         # Fetch fills every 60 seconds
    ZSCORE_WINDOW = 100         # Use last 100 snapshots for Z-score

    def __init__(self) -> None:
        self.snapshots: deque[HLPSnapshot] = deque(maxlen=2000)  # ~16 hours at 30s
        self.trades: deque[HLPTrade] = deque(maxlen=5000)
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._seen_fill_tids: set[int] = set()
        self._callbacks: list = []
        self._session_start_value: float = 0.0

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
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

    # ── Snapshot Loop ────────────────────────────────────────

    async def _snapshot_loop(self) -> None:
        """Periodically snapshot all HLP vault positions."""
        while self._running:
            try:
                for vault_name, address in self.HLP_VAULTS.items():
                    snapshot = await self._take_snapshot(address)
                    if snapshot:
                        # Compute Z-score from history
                        snapshot.delta_zscore = self._compute_delta_zscore(snapshot.net_delta_usd)
                        self.snapshots.append(snapshot)

                        # Track session PnL
                        if self._session_start_value == 0:
                            self._session_start_value = snapshot.account_value
                        snapshot.session_pnl = snapshot.account_value - self._session_start_value
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[hlp] snapshot error")
            await asyncio.sleep(self.SNAPSHOT_INTERVAL)

    async def _take_snapshot(self, address: str) -> HLPSnapshot | None:
        """Fetch clearinghouseState and build a snapshot."""
        if self._session is None or self._session.closed:
            return None

        payload = {"type": "clearinghouseState", "user": address}
        try:
            async with self._session.post(self.API_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning("[hlp] clearinghouseState HTTP %d", resp.status)
                    return None
                data = await resp.json()
        except Exception:
            logger.exception("[hlp] clearinghouseState request failed")
            return None

        # Parse margin summary
        margin_summary = data.get("marginSummary", {})
        account_value = float(margin_summary.get("accountValue", 0))
        total_margin_used = float(margin_summary.get("totalMarginUsed", 0))

        # Parse positions
        positions: list[HLPPosition] = []
        net_delta_usd = 0.0
        total_exposure_usd = 0.0
        total_unrealized_pnl = 0.0

        for pos_data in data.get("assetPositions", []):
            pos_info = pos_data.get("position", pos_data)
            symbol = pos_info.get("coin", "")
            size_raw = float(pos_info.get("szi", 0))
            if size_raw == 0:
                continue

            entry_price = float(pos_info.get("entryPx", 0))
            # Current price from position data
            current_price = float(pos_info.get("positionValue", 0))
            if abs(size_raw) > 0:
                current_price = abs(float(pos_info.get("positionValue", 0)) / size_raw) if size_raw != 0 else entry_price

            unrealized_pnl = float(pos_info.get("unrealizedPnl", 0))
            leverage_info = pos_info.get("leverage", {})
            if isinstance(leverage_info, dict):
                leverage = float(leverage_info.get("value", 1))
            else:
                leverage = float(leverage_info) if leverage_info else 1.0

            side = "long" if size_raw > 0 else "short"
            size_usd = abs(float(pos_info.get("positionValue", 0)))

            positions.append(HLPPosition(
                symbol=symbol,
                side=side,
                size=size_raw,
                size_usd=size_usd,
                entry_price=entry_price,
                current_price=current_price,
                unrealized_pnl=unrealized_pnl,
                leverage=leverage,
            ))

            # Signed value for delta: positive for long, negative for short
            signed_value = size_usd if side == "long" else -size_usd
            net_delta_usd += signed_value
            total_exposure_usd += size_usd
            total_unrealized_pnl += unrealized_pnl

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
        """Periodically fetch HLP fills to detect liquidation absorptions."""
        while self._running:
            try:
                for vault_name, address in self.HLP_VAULTS.items():
                    await self._fetch_fills(address)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[hlp] fills error")
            await asyncio.sleep(self.FILLS_INTERVAL)

    async def _fetch_fills(self, address: str) -> None:
        """Fetch recent fills and detect liquidation events."""
        if self._session is None or self._session.closed:
            return

        payload = {"type": "userFills", "user": address}
        try:
            async with self._session.post(self.API_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning("[hlp] userFills HTTP %d", resp.status)
                    return
                fills = await resp.json()
        except Exception:
            logger.exception("[hlp] userFills request failed")
            return

        if not isinstance(fills, list):
            return

        for fill in fills:
            tid = fill.get("tid", 0)
            if tid in self._seen_fill_tids:
                continue
            self._seen_fill_tids.add(tid)

            # Keep seen set bounded
            if len(self._seen_fill_tids) > 10000:
                # Remove oldest half
                to_remove = sorted(self._seen_fill_tids)[:5000]
                self._seen_fill_tids -= set(to_remove)

            symbol = fill.get("coin", "")
            side = fill.get("side", "").lower()  # 'A' (ask/sell) or 'B' (bid/buy)
            if side in ("a", "sell"):
                side = "sell"
            elif side in ("b", "buy"):
                side = "buy"

            price = float(fill.get("px", 0))
            size = float(fill.get("sz", 0))
            size_usd = price * size
            direction = fill.get("dir", "")
            closed_pnl = float(fill.get("closedPnl", 0))
            fill_time = fill.get("time", 0)
            if isinstance(fill_time, int) and fill_time > 1e12:
                fill_time = fill_time / 1000.0  # ms to seconds
            elif isinstance(fill_time, (int, float)):
                fill_time = float(fill_time)
            else:
                fill_time = time.time()

            # Detect liquidation absorption:
            # HLP fills that open a new position (dir starts with "Open")
            # with closedPnl of 0 are likely absorbing a liquidation.
            # Also check the crossed field if present.
            is_liquidation = False
            crossed = fill.get("crossed", False)
            if crossed:
                is_liquidation = True
            elif direction.startswith("Open") and abs(closed_pnl) < 0.01:
                is_liquidation = True

            trade = HLPTrade(
                timestamp=fill_time,
                symbol=symbol,
                side=side,
                price=price,
                size=size,
                size_usd=size_usd,
                direction=direction,
                closed_pnl=closed_pnl,
                is_liquidation=is_liquidation,
            )
            self.trades.append(trade)

            # Notify callbacks
            for cb in self._callbacks:
                try:
                    cb(trade)
                except Exception:
                    logger.exception("[hlp] trade callback error")

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
