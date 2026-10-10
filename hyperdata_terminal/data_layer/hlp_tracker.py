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
attaches to a fill that took the other side of a liquidation. Most are
``"market"`` fills by Strategy A/B when HLP was the book counterparty (often
both vaults fill the same liquidation: one transaction hash, one
absorption); ``"backstop"`` takeovers by a Liquidator vault are rarer.
Liquidations filled entirely by other traders never show up here.

Session PnL comes from Hyperliquid's own cumulative PnL series
(vaultDetails allTime.pnlHistory), never from the change in AUM, which moves
with every deposit and withdrawal.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

import aiohttp

from hyperdata_terminal.data_layer.hl_rate import HyperliquidRateLimited, hl_info

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
    # PnL since this session started, from Hyperliquid's own cumulative PnL
    # series (vaultDetails allTime.pnlHistory). NOT the change in AUM, which
    # moves by hundreds of thousands with every deposit and withdrawal.
    session_pnl: float = 0.0
    pnl_known: bool = False     # False until a fresh vaultDetails reading exists
    # Where account_value came from: "vaultDetails" (what Hyperliquid reports)
    # or "clearinghouseState" (summed fallback, misses Strategy X).
    aum_source: str = "clearinghouseState"


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
    # Hyperliquid's transaction hash. One liquidation is often filled by
    # several HLP vaults (Strategy A and B both): same hash, one absorption.
    fill_hash: str = ""
    is_history: bool = False       # happened before this session started

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
    # vaultDetails on the parent: the child list, the AUM Hyperliquid reports
    # (clearinghouseState misses Strategy X, ~$100M of it) and its cumulative
    # PnL series. Readings older than VAULT_DETAILS_STALE_AFTER are dropped
    # rather than shown as current.
    VAULT_DETAILS_INTERVAL = 300
    VAULT_DETAILS_STALE_AFTER = 900
    # userFillsByTime from a per vault watermark: weight 20 plus 1 per 20
    # fills returned. Strategy A/B fill ~150 times a minute each, so a 2
    # minute poll costs ~35 per busy vault and 20 per quiet one: under 10% of
    # Hyperliquid's 1200 per minute budget for all seven vaults.
    FILLS_INTERVAL = 120
    FILLS_PAGE_LIMIT = 2000       # userFillsByTime returns at most this many, oldest first
    # A full page means more fills are waiting (a volume spike): keep paging
    # forward, up to this many pages per vault per poll (6000 fills, ~20x the
    # normal rate; each full page costs ~120 weight, so the cap also bounds
    # the burst against the 1200 per minute limit).
    MAX_PAGES_PER_POLL = 3
    # Hyperliquid keeps roughly the last 10,000 fills per user. If a page
    # starts this far past the watermark, the fills in between are gone.
    GAP_WARN_SECONDS = 2 * FILLS_INTERVAL
    FIRST_POLL_LOOKBACK = 86_400  # seed 24h of absorptions from the quiet Liquidator vaults
    ZSCORE_WINDOW = 100         # Use last 100 snapshots for Z-score
    MAX_ABSORPTIONS = 2000

    def __init__(self) -> None:
        self.snapshots: deque[HLPSnapshot] = deque(maxlen=2000)  # ~16 hours at 30s
        self.trades: deque[HLPTrade] = deque(maxlen=5000)
        # Absorptions keyed by transaction hash: one liquidation filled by
        # several vaults is one absorption (sizes summed). Kept apart from
        # `trades`, which ~300 ordinary fills a minute would flush in minutes.
        self.absorptions: OrderedDict[str, HLPTrade] = OrderedDict()
        # Hashes created or grown since the last flush_absorptions(); new ones
        # are reported to absorption callbacks once, at the end of a pass.
        self._absorptions_new: dict[str, bool] = {}
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._tasks: list[asyncio.Task] = []
        # Per vault: newest fill time processed (ms) and the tids seen at that
        # exact time. Fills at or before the watermark are never processed
        # again, and an empty or short response changes nothing.
        self._fill_watermark: dict[str, int] = {}
        self._tids_at_watermark: dict[str, set[int]] = {}
        self._callbacks: list = []
        self._absorption_callbacks: list = []
        self.child_vaults: list[str] = list(self.FALLBACK_CHILD_VAULTS)
        self._vault_details_at: float = 0.0      # last successful vaultDetails
        self.reported_aum: float = 0.0           # vaultDetails AUM; 0 until fetched
        self.reported_cum_pnl: float | None = None  # vaultDetails allTime cumulative PnL
        self._pnl_baseline: float | None = None  # cumulative PnL at the first reading
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
        # A stop mid pass has already advanced watermarks past absorptions that
        # were never reported; report them now (the hub closes the store after).
        self.flush_absorptions()
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def on_hlp_trade(self, callback) -> None:
        """Register callback(trade) for every live HLP fill (not first-poll history)."""
        self._callbacks.append(callback)

    def on_hlp_absorption(self, callback) -> None:
        """Register callback(absorption, first_seen) for liquidations HLP absorbed.

        Called once per transaction hash with first_seen=True, and again with
        first_seen=False if a later poll adds another vault's fill of the same
        liquidation (size grows). History from the first poll is included
        (``absorption.is_history``) so persistence can upsert it by hash.
        """
        self._absorption_callbacks.append(callback)

    async def _post(self, payload: dict):
        """POST to the info endpoint; None on any failure (logged)."""
        if self._session is None or self._session.closed:
            return None
        try:
            return await hl_info(self._session, payload, component="hlp",
                                 timeout=aiohttp.ClientTimeout(total=10))
        except asyncio.CancelledError:
            raise
        except HyperliquidRateLimited:
            return None  # logged by the governor, which pauses every caller
        except aiohttp.ClientResponseError as exc:
            logger.warning("[hlp] %s HTTP %d", payload.get("type"), exc.status)
            return None
        except Exception:
            logger.exception("[hlp] %s request failed", payload.get("type"))
            return None

    async def refresh_vault_details(self) -> None:
        """Re-read the child list, reported AUM and cumulative PnL (keeps the last known on failure)."""
        data = await self._post({"type": "vaultDetails", "vaultAddress": self.PARENT_VAULT})
        if not isinstance(data, dict):
            return  # retried on the next snapshot pass, not in 5 minutes
        self.apply_vault_details(data)

    # Backwards compatible name.
    refresh_child_vaults = refresh_vault_details

    def apply_vault_details(self, data: dict, now: float | None = None) -> None:
        children = (data.get("relationship") or {}).get("data", {}).get("childAddresses")
        if isinstance(children, list) and children:
            self.child_vaults = [str(c).lower() for c in children if isinstance(c, str)]
        aum = self.parse_reported_aum(data)
        pnl = self.parse_cumulative_pnl(data)
        if aum > 0:
            self.reported_aum = aum
            self._vault_details_at = time.time() if now is None else now
        if pnl is not None:
            self.reported_cum_pnl = pnl
            if self._pnl_baseline is None:
                self._pnl_baseline = pnl

    @staticmethod
    def _portfolio_series(vault_details: dict, period: str, key: str) -> list:
        try:
            portfolio = dict(vault_details.get("portfolio") or [])
            return (portfolio.get(period) or {}).get(key) or []
        except (TypeError, ValueError):
            return []

    @classmethod
    def parse_reported_aum(cls, vault_details: dict) -> float:
        """Latest point of the parent's day accountValueHistory (what the Hyperliquid UI shows)."""
        history = cls._portfolio_series(vault_details, "day", "accountValueHistory")
        try:
            return float(history[-1][1]) if history else 0.0
        except (TypeError, ValueError, IndexError):
            return 0.0

    @classmethod
    def parse_cumulative_pnl(cls, vault_details: dict) -> float | None:
        """Latest point of allTime pnlHistory: cumulative PnL since HLP launched.

        Its last point has the same live timestamp as the AUM series, so the
        difference between two readings is the PnL over that span, unaffected
        by deposits and withdrawals. (day.pnlHistory restarts at 0 as its 24h
        window rolls, so it cannot be used as a baseline.)
        """
        history = cls._portfolio_series(vault_details, "allTime", "pnlHistory")
        try:
            return float(history[-1][1]) if history else None
        except (TypeError, ValueError, IndexError):
            return None

    def _details_fresh(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.reported_aum > 0 and now - self._vault_details_at <= self.VAULT_DETAILS_STALE_AFTER

    # ── Snapshot Loop ────────────────────────────────────────

    async def _snapshot_loop(self) -> None:
        """Periodically snapshot the whole HLP (parent plus every child vault)."""
        while self._running:
            try:
                if time.time() - self._vault_details_at > self.VAULT_DETAILS_INTERVAL:
                    await self.refresh_vault_details()
                snapshot = await self._take_aggregate_snapshot()
                if snapshot:
                    self.record_snapshot(snapshot)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[hlp] snapshot error")
            await asyncio.sleep(self.SNAPSHOT_INTERVAL)

    def record_snapshot(self, snapshot: HLPSnapshot) -> None:
        """Append a snapshot, filling in its z score and session PnL."""
        snapshot.delta_zscore = self._compute_delta_zscore(snapshot.net_delta_usd)
        # A stale reading would freeze PnL while AUM is already marked partial.
        if self.reported_cum_pnl is not None and self._pnl_baseline is not None and self._details_fresh():
            snapshot.session_pnl = self.reported_cum_pnl - self._pnl_baseline
            snapshot.pnl_known = True
        self.snapshots.append(snapshot)

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
        reported = self.reported_aum if self._details_fresh() else 0.0
        return self.build_snapshot(states, reported_aum=reported)

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
            aum_source="vaultDetails" if reported_aum > 0 else "clearinghouseState",
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
                # After every vault was polled, so A's and B's fills of one
                # liquidation are reported as one absorption.
                self.flush_absorptions()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[hlp] fills error")
            await asyncio.sleep(self.FILLS_INTERVAL)

    async def _fetch_page(self, address: str, start_ms: int) -> list | None:
        fills = await self._post({
            "type": "userFillsByTime", "user": address, "startTime": start_ms, "aggregateByTime": False,
        })
        return fills if isinstance(fills, list) else None

    async def _fetch_fills(self, address: str) -> None:
        """Fetch a vault's fills since its watermark (paging forward) and process the new ones."""
        now = time.time()
        if address not in self._fill_watermark:
            start_ms = int((now - self.FIRST_POLL_LOOKBACK) * 1000)
            fills = await self._fetch_page(address, start_ms)
            if fills is None:
                return
            if len(fills) < self.FILLS_PAGE_LIMIT:
                self.process_fills(address, fills)
                self._fill_watermark.setdefault(address, start_ms)  # a quiet vault: nothing returned yet
                return
            # A busy strategy vault: a full page from 24h ago does not reach
            # the present, and paging through a day of market making fills
            # would burn the rate budget. Keep the page's flagged absorptions
            # as history and resume one poll interval before session start:
            # that covers the last moments of a previous run (stored by hash,
            # so re-reading them never duplicates) without a large backfill.
            self.process_fills(address, [f for f in fills if isinstance(f, dict) and f.get("liquidation")])
            resume_ms = int(((self._started_at or now) - self.FILLS_INTERVAL) * 1000)
            if resume_ms > self._fill_watermark.get(address, 0):
                self._fill_watermark[address] = resume_ms
                self._tids_at_watermark[address] = set()

        for _ in range(self.MAX_PAGES_PER_POLL):
            requested_ms = self._fill_watermark[address]
            fills = await self._fetch_page(address, requested_ms)
            if fills is None:
                return
            if len(fills) >= self.FILLS_PAGE_LIMIT:
                first_ms = min((f.get("time", 0) for f in fills if isinstance(f, dict)), default=0)
                if first_ms - requested_ms > self.GAP_WARN_SECONDS * 1000:
                    logger.warning(
                        "[hlp] %s: fills from %.0fs before this page are no longer available from "
                        "Hyperliquid (the tracker fell behind, e.g. after a sleep); absorptions in "
                        "that gap are lost", address, (first_ms - requested_ms) / 1000,
                    )
            new = self.process_fills(address, fills)
            if len(fills) < self.FILLS_PAGE_LIMIT or not new:
                return  # caught up (or a page of nothing but already seen fills)
        logger.warning(
            "[hlp] %s still has a full page after %d pages: fills are arriving faster than "
            "they are read; the watermark is %.0fs behind",
            address, self.MAX_PAGES_PER_POLL, time.time() - self._fill_watermark[address] / 1000,
        )

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

            trade.is_history = bool(self._started_at) and trade.timestamp < self._started_at
            if not trade.fill_hash:
                trade.fill_hash = f"{address}:{tid}"
            self.trades.append(trade)
            if trade.is_liquidation:
                self._add_absorption(trade)
            new_trades.append(trade)

            if trade.is_history:
                continue  # first poll history: display only
            for cb in self._callbacks:
                try:
                    cb(trade)
                except Exception:
                    logger.exception("[hlp] trade callback error")

        if new_trades:
            self._fill_watermark[address] = watermark
            self._tids_at_watermark[address] = seen_at_mark
        return new_trades

    def _add_absorption(self, trade: HLPTrade) -> None:
        """Merge a liquidation fill into its absorption (one per transaction hash)."""
        key = trade.fill_hash
        group = self.absorptions.get(key)
        if group is None:
            self.absorptions[key] = dataclasses.replace(trade)
            self._absorptions_new[key] = True
            while len(self.absorptions) > self.MAX_ABSORPTIONS:
                self.absorptions.popitem(last=False)
            return
        group.size += trade.size
        group.size_usd += trade.size_usd
        group.price = group.size_usd / group.size if group.size else group.price
        group.vault = group.vault if trade.vault in group.vault.split(",") else f"{group.vault},{trade.vault}"
        self._absorptions_new.setdefault(key, False)

    def flush_absorptions(self) -> None:
        """Report absorptions created or grown since the last flush."""
        pending, self._absorptions_new = self._absorptions_new, {}
        for key, first_seen in pending.items():
            group = self.absorptions.get(key)
            if group is None:
                continue
            for cb in self._absorption_callbacks:
                try:
                    cb(group, first_seen)
                except Exception:
                    logger.exception("[hlp] absorption callback error")

    @staticmethod
    def _usable_hash(raw) -> str:
        """The fill's transaction hash, or "" when there is none.

        Some maker fills carry an all zero hash (62 of 2000 in a live check).
        Grouping by it would merge unrelated fills into one absorption, so it
        is treated as missing (the vault:tid fallback key is used instead).
        """
        h = str(raw or "")
        if not h or not h.removeprefix("0x").strip("0"):
            return ""
        return h

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
            fill_hash=HLPTracker._usable_hash(fill.get("hash")),
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
        return sorted((t for t in self.absorptions.values() if t.timestamp > cutoff), key=lambda t: t.timestamp)

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
            "pnl_known": snap.pnl_known if snap else False,
            "total_unrealized_pnl": snap.total_unrealized_pnl if snap else 0,
            "total_snapshots": len(self.snapshots),
            "total_trades": len(self.trades),
            "liquidation_absorptions": len(self.absorptions),
            "aum_source": snap.aum_source if snap else "",
        }
