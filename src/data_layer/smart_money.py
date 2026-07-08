"""
Smart Money Scoring Engine — discovers, tracks, and ranks Hyperliquid wallets.

Continuously watches the trade WebSocket for active addresses, fetches their
fill history, computes PnL / win-rate / Sharpe, and generates actionable
signals when "smart money" or "dumb money" opens positions.

Usage:
    engine = SmartMoneyEngine()
    await engine.start()          # begins discovery + analysis loops
    engine.on_signal(my_callback) # get notified on signals
    ...
    await engine.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import aiohttp
import numpy as np

from src.data_layer import address_store

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
ANCHOR_PATH = DATA_DIR / "anchor_wallets.json"

LEADERBOARD_URLS: list[str] = []  # No external leaderboard in OSS build


# ── Data models ────────────────────────────────────────────────────────────

@dataclass
class WalletProfile:
    address: str
    discovered_at: float            # When we first saw this wallet
    last_seen: float                # Last trade activity
    last_analyzed: float            # Last time we fetched their fills

    # Performance metrics
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_realized_pnl: float = 0.0
    total_volume_usd: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    avg_hold_time_seconds: float = 0.0

    # Computed scores
    win_rate: float = 0.0           # winning / total
    pnl_score: float = 0.0         # log-scaled PnL
    sharpe_ratio: float = 0.0      # risk-adjusted returns
    composite_score: float = 0.0    # final weighted score
    confidence: float = 0.0         # 0-1 sample-size confidence (see _compute_confidence)

    # Classification
    rank: int = 0                   # 1 = best performer
    tier: str = "unknown"           # "smart", "average", "dumb"

    # Current state
    account_value: float = 0.0
    open_positions: int = 0
    active_symbols: list = field(default_factory=list)

    def __post_init__(self):
        if self.active_symbols is None:
            self.active_symbols = []


@dataclass
class SmartMoneySignal:
    timestamp: float
    address: str
    tier: str               # "smart" or "dumb"
    action: str             # "OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT"
    symbol: str
    size_usd: float
    wallet_rank: int
    wallet_win_rate: float
    wallet_pnl: float
    signal_type: str        # "follow" (smart money) or "fade" (dumb money)


# ── Engine ─────────────────────────────────────────────────────────────────

class SmartMoneyEngine:
    API_URL = "https://api.hyperliquid.xyz/info"

    # Scoring weights
    ALPHA = 0.35    # Win rate weight
    BETA = 0.40     # PnL weight (log-scaled)
    GAMMA = 0.25    # Sharpe weight

    # Thresholds. Three closed trades says nothing about skill — a coin flip
    # "wins" three in a row 12.5% of the time — so ranking requires a
    # minimally meaningful sample plus real volume, and every profile carries
    # a sample-size confidence that consumers must surface alongside tiers.
    MIN_TRADES_FOR_RANKING = 10
    MIN_VOLUME_FOR_RANKING = 50_000     # Total traded volume (USD)
    FULL_CONFIDENCE_TRADES = 50         # Trades at which confidence saturates
    SMART_MONEY_TOP_N = 100             # Top 100 = smart money
    DUMB_MONEY_BOTTOM_N = 100           # Bottom 100 = dumb money
    ANALYSIS_INTERVAL = 300             # Analyze wallets every 5 minutes
    DISCOVERY_INTERVAL = 10             # Discover new addresses every 10 seconds
    MIN_ACCOUNT_VALUE = 1000            # Ignore wallets < $1K
    ANALYSIS_BATCH_SIZE = 20            # Wallets per analysis cycle

    def __init__(self) -> None:
        self.wallets: dict[str, WalletProfile] = {}
        self.signals: deque[SmartMoneySignal] = deque(maxlen=1000)
        self._callbacks: list[Callable] = []
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._tasks: list[asyncio.Task] = []
        # Rate limiting
        self._request_times: deque = deque(maxlen=8)

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start discovery and analysis loops."""
        self._running = True
        self._session = aiohttp.ClientSession()
        self._tasks = [
            asyncio.create_task(self._discovery_loop(), name="sm-discovery"),
            asyncio.create_task(self._analysis_loop(), name="sm-analysis"),
            asyncio.create_task(self.seed_from_leaderboard(), name="sm-seed"),
        ]
        logger.info("[smart_money] Engine started")

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("[smart_money] Engine stopped — %d wallets tracked", len(self.wallets))

    def on_signal(self, callback: Callable[[SmartMoneySignal], Any]) -> None:
        """Register callback for smart money signals."""
        self._callbacks.append(callback)

    # ── Address persistence ────────────────────────────────────────────

    def _save_addresses(self) -> None:
        """Persist tracked wallets to SQLite (atomic, race-free)."""
        try:
            address_store.add_addresses(self.wallets.keys(), source="smart_money")
        except Exception:
            logger.debug("[smart_money] Failed to save discovered addresses")

    # ── Leaderboard seeding ────────────────────────────────────────────

    async def fetch_leaderboard_wallets(self, top_n: int = 50) -> list[str]:
        """Fetch top wallets from on-chain activity. Returns list of 0x addresses.

        In the open-source build, wallets are discovered organically from
        Hyperliquid position activity. No external leaderboard seeding.
        """
        addresses: list[str] = []

        # Fallback: HTTP API endpoints
        session = self._session
        if session is None or session.closed:
            session = aiohttp.ClientSession()
            own = True
        else:
            own = False

        try:
            for url in LEADERBOARD_URLS:
                try:
                    async with session.get(
                        url,
                        params={"limit": str(top_n), "sort": "profit"},
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        rows = data if isinstance(data, list) else data.get("data", [])
                        if not isinstance(rows, list):
                            continue
                        for row in rows:
                            addr = (
                                row.get("proxy_wallet")
                                or row.get("address")
                                or row.get("wallet")
                                or row.get("maker")
                                or row.get("user")
                                or ""
                            )
                            if address_store.is_valid_address(addr):
                                addresses.append(address_store.normalize_address(addr))
                        if addresses:
                            logger.info(
                                "[smart_money] Fetched %d leaderboard wallets from %s",
                                len(addresses), url,
                            )
                            break
                except Exception:
                    logger.debug("[smart_money] Leaderboard fetch failed: %s", url)
                    continue
        finally:
            if own and not session.closed:
                await session.close()

        return addresses

    async def seed_from_leaderboard(self) -> int:
        """Seed wallets from anchor file + on-chain leaderboard."""
        now = time.time()
        seed_addrs: list[str] = []  # ordered, deduped later

        # 1. Load anchor_wallets.json
        anchor_count = 0
        try:
            if ANCHOR_PATH.exists():
                raw = json.loads(ANCHOR_PATH.read_text())
                for entry in raw.get("anchors", []):
                    addr = entry.get("address", "")
                    if address_store.is_valid_address(addr):
                        seed_addrs.append(address_store.normalize_address(addr))
                        anchor_count += 1
        except Exception:
            logger.debug("[smart_money] Failed to load anchor_wallets.json")

        # 2. Fetch leaderboard
        leaderboard = await self.fetch_leaderboard_wallets(top_n=50)
        lb_count = len(leaderboard)
        seed_addrs.extend(leaderboard)

        # 3. Deduplicate preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for addr in seed_addrs:
            if addr not in seen:
                seen.add(addr)
                unique.append(addr)

        # 4. Add new wallets
        added = 0
        for addr in unique:
            if addr not in self.wallets:
                self.wallets[addr] = WalletProfile(
                    address=addr,
                    discovered_at=now,
                    last_seen=now,
                    last_analyzed=0,
                )
                added += 1

        # 5. Persist
        self._save_addresses()

        logger.info(
            "[smart_money] Seeded %d new wallets (%d anchors + %d leaderboard)",
            added, anchor_count, lb_count,
        )
        logger.info(
            "[smart_money] Total tracked wallets after seed: %d",
            len(self.wallets),
        )
        return added

    # ── Rate limiting ─────────────────────────────────────────────────

    async def _rate_limit(self) -> None:
        """Max 8 requests per second to Hyperliquid."""
        now = time.time()
        self._request_times.append(now)
        if len(self._request_times) >= 8:
            oldest = self._request_times[0]
            elapsed = now - oldest
            if elapsed < 1.0:
                await asyncio.sleep(1.0 - elapsed)

    # ── API helpers ───────────────────────────────────────────────────

    async def _post(self, payload: dict) -> Any:
        """POST to Hyperliquid info endpoint with rate limiting."""
        if self._session is None or self._session.closed:
            return None
        await self._rate_limit()
        try:
            async with self._session.post(self.API_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning("[smart_money] API %d for %s", resp.status, payload.get("type"))
                return None
        except Exception:
            logger.debug("[smart_money] API request failed for %s", payload.get("type"))
            return None

    async def _fetch_fills(self, address: str) -> list[dict]:
        """Fetch recent trade fills for a wallet."""
        data = await self._post({"type": "userFills", "user": address})
        if isinstance(data, list):
            return data
        return []

    async def _fetch_clearinghouse(self, address: str) -> dict | None:
        """Fetch clearinghouse state (positions + account value)."""
        return await self._post({"type": "clearinghouseState", "user": address})

    # ── Discovery ─────────────────────────────────────────────────────

    async def _discovery_loop(self) -> None:
        """Watch trade WebSocket and collect addresses."""
        coins = ["BTC", "ETH", "SOL", "DOGE", "XRP", "AVAX", "LINK", "ARB"]
        while self._running:
            try:
                async with self._session.ws_connect(
                    "wss://api.hyperliquid.xyz/ws", heartbeat=20
                ) as ws:
                    for coin in coins:
                        await ws.send_json({
                            "method": "subscribe",
                            "subscription": {"type": "trades", "coin": coin},
                        })

                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("channel") == "trades":
                                for trade in data.get("data", []):
                                    for addr in trade.get("users", []):
                                        # Untrusted payload: only well-formed
                                        # wallet addresses become profiles.
                                        if not address_store.is_valid_address(addr):
                                            continue
                                        addr = address_store.normalize_address(addr)
                                        if addr not in self.wallets:
                                            self.wallets[addr] = WalletProfile(
                                                address=addr,
                                                discovered_at=time.time(),
                                                last_seen=time.time(),
                                                last_analyzed=0,
                                            )
                                        else:
                                            self.wallets[addr].last_seen = time.time()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[smart_money] Discovery WS error")
                await asyncio.sleep(5)

    async def discover_from_trades(self, symbols: list[str] | None = None, duration: float = 10) -> int:
        """One-shot discovery: watch trades for N seconds, collect addresses."""
        symbols = symbols or ["BTC", "ETH", "SOL"]
        before = len(self.wallets)
        session = self._session or aiohttp.ClientSession()
        own_session = self._session is None

        try:
            async with session.ws_connect("wss://api.hyperliquid.xyz/ws", heartbeat=20) as ws:
                for coin in symbols:
                    await ws.send_json({
                        "method": "subscribe",
                        "subscription": {"type": "trades", "coin": coin},
                    })

                end_time = time.time() + duration
                async for msg in ws:
                    if time.time() >= end_time:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if data.get("channel") == "trades":
                            for trade in data.get("data", []):
                                for addr in trade.get("users", []):
                                    if not address_store.is_valid_address(addr):
                                        continue
                                    addr = address_store.normalize_address(addr)
                                    if addr not in self.wallets:
                                        self.wallets[addr] = WalletProfile(
                                            address=addr,
                                            discovered_at=time.time(),
                                            last_seen=time.time(),
                                            last_analyzed=0,
                                        )
        except Exception:
            logger.exception("[smart_money] One-shot discovery error")
        finally:
            if own_session and not session.closed:
                await session.close()

        discovered = len(self.wallets) - before
        logger.info("[smart_money] Discovered %d new addresses in %.0fs", discovered, duration)
        return discovered

    # ── Analysis ──────────────────────────────────────────────────────

    async def _analysis_loop(self) -> None:
        """Periodically analyze wallet performance."""
        while self._running:
            try:
                now = time.time()
                # Pick wallets that haven't been analyzed recently
                candidates = [
                    w for w in self.wallets.values()
                    if (now - w.last_analyzed) > self.ANALYSIS_INTERVAL
                ]
                # Prioritize: recently active first
                candidates.sort(key=lambda w: w.last_seen, reverse=True)
                batch = candidates[: self.ANALYSIS_BATCH_SIZE]

                logger.info(
                    "[smart_money] Analysis cycle: %d wallets to analyze (of %d tracked)",
                    len(batch), len(self.wallets),
                )

                for wallet in batch:
                    if not self._running:
                        break
                    try:
                        await self.analyze_wallet(wallet.address)
                    except Exception:
                        logger.debug("[smart_money] Failed to analyze %s", wallet.address[:10])

                # Re-rank after each batch
                self.rank_all()

                ranked_count = sum(1 for w in self.wallets.values() if self._qualifies_for_ranking(w))
                logger.info(
                    "[smart_money] Analyzed %d wallets, %d total tracked, %d ranked",
                    len(batch), len(self.wallets), ranked_count,
                )

            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("[smart_money] Analysis loop error")

            await asyncio.sleep(self.ANALYSIS_INTERVAL)

    async def analyze_wallet(self, address: str) -> WalletProfile:
        """Fetch fills for a wallet and compute all performance metrics."""
        wallet = self.wallets.get(address)
        if wallet is None:
            wallet = WalletProfile(
                address=address,
                discovered_at=time.time(),
                last_seen=time.time(),
                last_analyzed=0,
            )
            self.wallets[address] = wallet

        # 1. Fetch recent fills
        fills = await self._fetch_fills(address)
        if not fills:
            wallet.last_analyzed = time.time()
            return wallet

        # 2. Parse closedPnl and compute metrics
        close_pnls: list[float] = []
        total_volume = 0.0
        winning = 0
        losing = 0
        total_pnl = 0.0
        largest_win = 0.0
        largest_loss = 0.0
        open_times: dict[str, float] = {}  # coin -> open timestamp
        hold_times: list[float] = []

        for fill in fills:
            px = float(fill.get("px", 0))
            sz = float(fill.get("sz", 0))
            fill_time = fill.get("time", 0)
            if isinstance(fill_time, str):
                fill_time = float(fill_time)
            fill_time_s = fill_time / 1000.0 if fill_time > 1e12 else fill_time

            size_usd = px * sz
            total_volume += size_usd

            direction = fill.get("dir", "")
            coin = fill.get("coin", "")
            closed_pnl = float(fill.get("closedPnl", "0"))

            # Track open/close for hold time estimation
            if direction.startswith("Open"):
                open_times[coin] = fill_time_s
            elif direction.startswith("Close"):
                if coin in open_times:
                    ht = fill_time_s - open_times[coin]
                    if ht > 0:
                        hold_times.append(ht)
                    del open_times[coin]

                # Every close fill counts as a trade (including breakeven)
                close_pnls.append(closed_pnl)
                total_pnl += closed_pnl
                if closed_pnl > 0:
                    winning += 1
                    largest_win = max(largest_win, closed_pnl)
                elif closed_pnl < 0:
                    losing += 1
                    largest_loss = min(largest_loss, closed_pnl)

        total_close_trades = len(close_pnls)

        # 3. Update wallet profile
        wallet.total_trades = total_close_trades
        wallet.winning_trades = winning
        wallet.losing_trades = losing
        wallet.total_realized_pnl = total_pnl
        wallet.total_volume_usd = total_volume
        wallet.largest_win = largest_win
        wallet.largest_loss = largest_loss
        wallet.avg_hold_time_seconds = float(np.mean(hold_times)) if hold_times else 0.0

        # 4. Compute derived scores
        wallet.win_rate = winning / total_close_trades if total_close_trades > 0 else 0.0
        wallet.sharpe_ratio = self._compute_sharpe(close_pnls)
        wallet.pnl_score = self._compute_pnl_score(total_pnl)
        wallet.composite_score = self._compute_composite(wallet)
        wallet.confidence = self._compute_confidence(wallet)

        # 5. Fetch clearinghouse state for account value + open positions
        ch = await self._fetch_clearinghouse(address)
        if ch and isinstance(ch, dict):
            margin = ch.get("marginSummary", {})
            wallet.account_value = float(margin.get("accountValue", 0))
            positions = ch.get("assetPositions", [])
            active = []
            for pos_wrapper in positions:
                pos = pos_wrapper.get("position", pos_wrapper)
                sz = float(pos.get("szi", 0))
                if sz != 0:
                    active.append(pos.get("coin", "?"))
            wallet.open_positions = len(active)
            wallet.active_symbols = active

        wallet.last_analyzed = time.time()

        # 6. Check for signals from recent fills
        await self.check_signals(address, fills)

        return wallet

    # ── Scoring ───────────────────────────────────────────────────────

    def _compute_pnl_score(self, total_pnl: float) -> float:
        """Log-scaled PnL normalized to approx -1..1 range."""
        if total_pnl > 0:
            score = math.log10(1 + total_pnl) / 10
        elif total_pnl < 0:
            score = -math.log10(1 + abs(total_pnl)) / 10
        else:
            score = 0.0
        return max(-1.0, min(1.0, score))

    def _compute_sharpe(self, pnl_values: list[float]) -> float:
        """Sharpe ratio from distribution of closed PnL values."""
        if len(pnl_values) < 2:
            return 0.0
        returns = np.array(pnl_values)
        mean = float(np.mean(returns))
        std = float(np.std(returns))
        if std == 0:
            return 0.0
        return mean / std

    def _compute_composite(self, w: WalletProfile) -> float:
        """Weighted composite score."""
        # Win rate: already 0-1
        wr = w.win_rate

        # Log-scaled PnL
        pnl = self._compute_pnl_score(w.total_realized_pnl)

        # Sharpe — normalize to approx -1..1
        sharpe = max(-3.0, min(3.0, w.sharpe_ratio)) / 3.0

        return self.ALPHA * wr + self.BETA * pnl + self.GAMMA * sharpe

    def _compute_confidence(self, w: WalletProfile) -> float:
        """Sample-size confidence in [0, 1].

        Linear in closed-trade count up to FULL_CONFIDENCE_TRADES. This is a
        coverage heuristic (recent fills only, no confidence interval on win
        rate) — consumers must show it next to any smart/dumb label rather
        than presenting tiers as certainty.
        """
        return min(1.0, w.total_trades / float(self.FULL_CONFIDENCE_TRADES))

    def _qualifies_for_ranking(self, w: WalletProfile) -> bool:
        return (w.total_trades >= self.MIN_TRADES_FOR_RANKING
                and w.total_volume_usd >= self.MIN_VOLUME_FOR_RANKING)

    # ── Ranking ───────────────────────────────────────────────────────

    def rank_all(self) -> None:
        """Re-rank all wallets by composite_score."""
        qualified = [w for w in self.wallets.values() if self._qualifies_for_ranking(w)]
        qualified.sort(key=lambda w: w.composite_score, reverse=True)

        qualified_set = {w.address for w in qualified}
        # Wallets that no longer qualify must lose their old rank/tier, or a
        # stale "smart" label survives after the sample stops qualifying.
        for w in self.wallets.values():
            if w.address not in qualified_set:
                w.rank = 0
                if w.tier in ("smart", "average", "dumb"):
                    w.tier = "unknown"

        for i, w in enumerate(qualified, 1):
            w.rank = i
            if i <= self.SMART_MONEY_TOP_N:
                w.tier = "smart"
            elif i > len(qualified) - self.DUMB_MONEY_BOTTOM_N:
                w.tier = "dumb"
            else:
                w.tier = "average"

    # ── Signal generation ─────────────────────────────────────────────

    async def check_signals(self, address: str, fills: list[dict]) -> None:
        """Check if a ranked wallet just opened/closed a position."""
        wallet = self.wallets.get(address)
        if wallet is None or wallet.tier == "unknown" or wallet.tier == "average":
            return
        if wallet.rank == 0:
            return

        # Only look at very recent fills (last 10 minutes)
        cutoff = time.time() - 600
        for fill in fills:
            fill_time = fill.get("time", 0)
            if isinstance(fill_time, str):
                fill_time = float(fill_time)
            fill_time_s = fill_time / 1000.0 if fill_time > 1e12 else fill_time
            if fill_time_s < cutoff:
                continue

            direction = fill.get("dir", "")
            coin = fill.get("coin", "")
            px = float(fill.get("px", 0))
            sz = float(fill.get("sz", 0))
            size_usd = px * sz

            action_map = {
                "Open Long": "OPEN_LONG",
                "Open Short": "OPEN_SHORT",
                "Close Long": "CLOSE_LONG",
                "Close Short": "CLOSE_SHORT",
            }
            action = action_map.get(direction)
            if action is None:
                continue

            # Determine signal type
            if wallet.tier == "smart":
                signal_type = "follow"
            elif wallet.tier == "dumb":
                signal_type = "fade"
            else:
                continue

            signal = SmartMoneySignal(
                timestamp=fill_time_s,
                address=address,
                tier=wallet.tier,
                action=action,
                symbol=coin,
                size_usd=size_usd,
                wallet_rank=wallet.rank,
                wallet_win_rate=wallet.win_rate,
                wallet_pnl=wallet.total_realized_pnl,
                signal_type=signal_type,
            )

            # Deduplicate: skip if we already have this exact signal
            if self.signals and any(
                s.address == signal.address
                and s.timestamp == signal.timestamp
                and s.symbol == signal.symbol
                and s.action == signal.action
                for s in self.signals
            ):
                continue

            self.signals.append(signal)
            self._emit_signal(signal)

    def _emit_signal(self, signal: SmartMoneySignal) -> None:
        """Notify all registered callbacks."""
        for cb in self._callbacks:
            try:
                cb(signal)
            except Exception:
                logger.exception("[smart_money] Signal callback error")

    # ── Queries ───────────────────────────────────────────────────────

    def get_smart_money(self, n: int = 20) -> list[WalletProfile]:
        """Get top N smart money wallets."""
        ranked = [w for w in self.wallets.values() if w.tier == "smart" and w.rank > 0]
        ranked.sort(key=lambda w: w.rank)
        return ranked[:n]

    def get_dumb_money(self, n: int = 20) -> list[WalletProfile]:
        """Get bottom N wallets."""
        ranked = [w for w in self.wallets.values() if w.tier == "dumb" and w.rank > 0]
        ranked.sort(key=lambda w: w.rank, reverse=True)
        return ranked[:n]

    def get_recent_signals(self, n: int = 50, max_age_s: int = 300) -> list[SmartMoneySignal]:
        """Return last N signals within max_age_s seconds.

        Time-filtered to prevent the full deque being replayed on
        cluster restart.
        """
        cutoff = time.time() - max_age_s
        return [
            s for s in list(self.signals)[-n:]
            if getattr(s, "timestamp", 0) >= cutoff
        ]

    def get_wallet(self, address: str) -> WalletProfile | None:
        """Look up a specific wallet."""
        return self.wallets.get(address)

    def get_stats(self) -> dict:
        """Summary stats: total wallets, ranked wallets, signals generated."""
        ranked = sum(1 for w in self.wallets.values() if self._qualifies_for_ranking(w))
        smart = sum(1 for w in self.wallets.values() if w.tier == "smart")
        dumb = sum(1 for w in self.wallets.values() if w.tier == "dumb")
        return {
            "total_wallets": len(self.wallets),
            "ranked_wallets": ranked,
            "smart_wallets": smart,
            "dumb_wallets": dumb,
            "total_signals": len(self.signals),
            # Coverage caveats: rankings come from recent fills only and small
            # samples — consumers should show these limits, not just tiers.
            "ranking_criteria": {
                "min_trades": self.MIN_TRADES_FOR_RANKING,
                "min_volume_usd": self.MIN_VOLUME_FOR_RANKING,
                "note": (
                    "Performance is computed from recent fills only; tiers are "
                    "heuristic. Check each wallet's `confidence` (0-1 sample-"
                    "size score) before acting on smart/dumb labels."
                ),
            },
        }
