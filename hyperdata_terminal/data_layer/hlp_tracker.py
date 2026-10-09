"""
HLP (Hyperliquidity Provider) Reverse Engineering Module.

Monitors Hyperliquid's native market-making protocol by tracking
HLP vault addresses, their positions, trades, and behavior patterns.

HLP is a *parent* vault that holds idle USDC and allocates it to child
vaults (Strategy A/B/X, several Liquidators). The parent itself carries no
positions, so tracking only the parent address shows "0 positions, net
delta 0" forever. The child list and the AUM Hyperliquid reports come from
``vaultDetails`` on the parent (every 5 minutes, with a static fallback
list), and every snapshot aggregates positions across all vaults. Strategy X
(~$100M at the time of writing) shows neither equity nor positions in
clearinghouseState, so AUM includes it but the visible positions do not.

Liquidation absorptions are read from the ``liquidation`` object Hyperliquid
attaches to a fill that took the other side of a liquidation: mostly
``"backstop"`` takeovers by a Liquidator vault (rare, often days apart),
occasionally ``"market"`` when a strategy vault was the book counterparty.
Most Hyperliquid liquidations are filled by other traders and never show up
here.
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
    # vaultDetails on the parent: the child list and the AUM Hyperliquid
    # reports (clearinghouseState misses Strategy X, ~$100M of it).
    VAULT_DETAILS_INTERVAL = 300
    # userFillsByTime from a per vault watermark: weight 20 plus 1 per 20
    # fills returned. Strategy A/B fill ~110 times a minute each, so a 2
    # minute poll costs ~31 per busy vault and 20 per quiet one: about 80 of
    # Hyperliquid's 1200 per minute budget for all seven vaults.
    FILLS_INTERVAL = 120
    FILLS_PAGE_LIMIT = 2000       # userFillsByTime returns at most this many, oldest first
    FIRST_POLL_LOOKBACK = 86_400  # seed 24h of absorptions from the (quiet) Liquidator vaults
    ZSCORE_WINDOW = 100         # Use last 100 snapshots for Z-score
    # Fills older than session start minus this seed the display history but
    # fire no callbacks: otherwise every start pushes old fills through
    # persistence (duplicating them on each restart) and the hub's liquidation
    # stream. Fills just before start still notify, to cover a quick restart.
    NOTIFY_BACKFILL_SECONDS = 120

    def __init__(self) -> None:
        self.snapshots: deque[HLPSnapshot] = deque(maxlen=2000)  # ~16 hours at 30s
        self.trades: deque[HLPTrade] = deque(maxlen=5000)
        # Absorptions get their own buffer: Strategy A/B add ~220 ordinary fills
        # a minute, which would push a day of absorptions out of `trades` in
        # about 20 minutes.
        self.absorptions: deque[HLPTrade] = deque(maxlen=2000)
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._tasks: list[asyncio.Task] = []
        # Per vault: newest fill time processed (ms) and the tids seen at that
        # exact time. Fills at or before the watermark are never processed
        # again, and an empty or short response changes nothing.
        self._fill_watermark: dict[str, int] = {}
        self._tids_at_watermark: dict[str, set[int]] = {}
        self._callbacks: list = []
        self._session_start_value: float = 0.0
        self.child_vaults: list[str] = list(self.FALLBACK_CHILD_VAULTS)
        self._vault_details_at: float = 0.0
        self.reported_aum: float = 0.0  # from vaultDetails; 0 until fetched
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

    async def refresh_vault_details(self) -> None:
        """Re-read the parent's child list and reported AUM (keeps the last known on failure)."""
        data = await self._post({"type": "vaultDetails", "vaultAddress": self.PARENT_VAULT})
        self._vault_details_at = time.time()
        if not isinstance(data, dict):
            return
        children = (data.get("relationship") or {}).get("data", {}).get("childAddresses")
        if isinstance(children, list) and children:
            self.child_vaults = [str(c).lower() for c in children if isinstance(c, str)]
        aum = self.parse_reported_aum(data)
        if aum > 0:
            self.reported_aum = aum

    # Backwards compatible name.
    refresh_child_vaults = refresh_vault_details

    @staticmethod
    def parse_reported_aum(vault_details: dict) -> float:
        """Latest point of the parent's day accountValueHistory (what the Hyperliquid UI shows)."""
        try:
            portfolio = dict(vault_details.get("portfolio") or [])
            history = (portfolio.get("day") or {}).get("accountValueHistory") or []
            return float(history[-1][1]) if history else 0.0
        except (TypeError, ValueError, IndexError):
            return 0.0

    # ── Snapshot Loop ────────────────────────────────────────

    async def _snapshot_loop(self) -> None:
        """Periodically snapshot the whole HLP (parent plus every child vault)."""
        while self._running:
            try:
                if time.time() - self._vault_details_at > self.VAULT_DETAILS_INTERVAL:
                    await self.refresh_vault_details()
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

        A partial sum would show a fake drop in delta and exposure whenever one
        child request fails, so a pass with any failure is discarded.
        """
        states = []
        for address in self.vault_addresses:
            state = await self._post({"type": "clearinghouseState", "user": address})
            if not isinstance(state, dict):
                return None
            states.append(state)
        return self.build_snapshot(states, reported_aum=self.reported_aum)

    async def _take_snapshot(self, address: str) -> HLPSnapshot | None:
        """Snapshot of a single vault (kept for ad hoc inspection)."""
        state = await self._post({"type": "clearinghouseState", "user": address})
        return self.build_snapshot([state]) if isinstance(state, dict) else None

    @staticmethod
    def build_snapshot(states: list[dict], reported_aum: float = 0.0) -> HLPSnapshot:
        """Aggregate clearinghouseState payloads into one HLP snapshot.

        Positions in the same coin held by different child vaults are netted
        into one position, because to the market HLP is one book: Strategy A
        and B often hold opposite sides of the same coin. ``total_exposure_usd``
        is the gross notional (each vault's positions summed before netting),
        so the netting never hides how much HLP actually has on.

        ``account_value`` is ``reported_aum`` (vaultDetails, includes Strategy
        X) when known; the sum of clearinghouse account values misses Strategy
        X, whose holdings are not visible through clearinghouseState at all.
        """
        account_value = 0.0
        total_margin_used = 0.0
        gross_exposure_usd = 0.0
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
                gross_exposure_usd += value
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
        total_unrealized_pnl = 0.0
        for symbol, m in merged.items():
            total_unrealized_pnl += m["pnl"]
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

        return HLPSnapshot(
            timestamp=time.time(),
            account_value=reported_aum if reported_aum > 0 else account_value,
            total_margin_used=total_margin_used,
            positions=positions,
            net_delta_usd=net_delta_usd,
            total_exposure_usd=gross_exposure_usd,
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
        """Fetch a vault's fills since its watermark and process the new ones."""
        first = address not in self._fill_watermark
        now = time.time()
        if first:
            start_ms = int((now - self.FIRST_POLL_LOOKBACK) * 1000)
        else:
            start_ms = self._fill_watermark[address]
        fills = await self._post({
            "type": "userFillsByTime", "user": address, "startTime": start_ms, "aggregateByTime": False,
        })
        if not isinstance(fills, list):
            return
        if first and len(fills) >= self.FILLS_PAGE_LIMIT:
            # A busy strategy vault: a full page from 24h ago does not reach
            # the present, and paging through a day of market making fills
            # would burn the rate budget. Keep the page's flagged absorptions
            # as history and resume just before session start.
            self.process_fills(address, [f for f in fills if isinstance(f, dict) and f.get("liquidation")])
            resume_ms = int(((self._started_at or now) - self.NOTIFY_BACKFILL_SECONDS) * 1000)
            if resume_ms > self._fill_watermark.get(address, 0):
                self._fill_watermark[address] = resume_ms
                self._tids_at_watermark[address] = set()
            return
        self.process_fills(address, fills)
        self._fill_watermark.setdefault(address, start_ms)  # a quiet vault: nothing returned yet

    def process_fills(self, address: str, fills: list[dict]) -> list[HLPTrade]:
        """Turn fills (any order) into HLPTrades; returns the ones not seen before."""
        watermark = self._fill_watermark.get(address, 0)
        seen_at_mark = self._tids_at_watermark.get(address, set())
        new_trades: list[HLPTrade] = []

        def _time(f):
            t = f.get("time", 0)
            return int(t) if isinstance(t, (int, float)) else 0

        for fill in sorted((f for f in fills if isinstance(f, dict)), key=_time):
            t_ms = _time(fill)
            tid = fill.get("tid", 0)
            if t_ms < watermark or (t_ms == watermark and tid in seen_at_mark):
                continue
            try:
                trade = self._parse_fill(fill, address)
            except (TypeError, ValueError):
                logger.debug("[hlp] unparseable fill: %.200s", fill)
                continue
            if t_ms > watermark:
                watermark, seen_at_mark = t_ms, {tid}
            else:
                seen_at_mark.add(tid)

            self.trades.append(trade)
            if trade.is_liquidation:
                self.absorptions.append(trade)
            new_trades.append(trade)

            if self._started_at and trade.timestamp < self._started_at - self.NOTIFY_BACKFILL_SECONDS:
                continue  # history from the first poll: display only
            for cb in self._callbacks:
                try:
                    cb(trade)
                except Exception:
                    logger.exception("[hlp] trade callback error")

        if new_trades:
            self._fill_watermark[address] = watermark
            self._tids_at_watermark[address] = seen_at_mark
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
        """Liquidations HLP absorbed in the last N minutes, oldest first."""
        cutoff = time.time() - minutes * 60
        return sorted((t for t in self.absorptions if t.timestamp > cutoff), key=lambda t: t.timestamp)

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
            "liquidation_absorptions": len(self.absorptions),
            "aum_source": "vaultDetails" if self.reported_aum > 0 else "clearinghouseState",
        }
