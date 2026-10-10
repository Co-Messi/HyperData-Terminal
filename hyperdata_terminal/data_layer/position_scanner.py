from __future__ import annotations

import asyncio
import logging
import math
import time
import time as _time  # wall-clock stamps (time.monotonic is used for rate limiting)
from dataclasses import dataclass, field

import aiohttp

from hyperdata_terminal.data_layer import address_store

logger = logging.getLogger(__name__)

API_URL = "https://api.hyperliquid.xyz/info"

RATE_LIMIT_PER_SEC = 10

# Per-cycle address budget (H4). scan() used to walk EVERY tracked address
# at 10 req/s, so cycle time grew linearly with the store — 50k addresses
# was an 83-minute cycle behind a 15s scan_interval, and nothing reported
# it. Now each cycle scans at most this many addresses (15 batches of
# RATE_LIMIT_PER_SEC), round-robin across cycles, and serves the rest from a
# per-address cache whose distance-to-liquidation is recomputed from fresh
# prices every cycle. Every position carries the time it was actually
# scanned.
SCAN_ADDRESS_BUDGET = 150

# Seconds the hub sleeps between scan() cycles (HyperDataHub's default
# scan_interval). Lives here because the staleness threshold below is
# derived from it; a hub configured with a LONGER interval is told at
# construction that the threshold no longer holds.
SCAN_INTERVAL_SECONDS = 15.0

# Sleep between batches of RATE_LIMIT_PER_SEC concurrent requests.
BATCH_SLEEP_SECONDS = 1.0

# Wall-clock allowance per batch for the exchange round-trip itself.
# clearinghouseState answers ~0.3s per batch of 10 concurrent requests when
# healthy; 1s absorbs a slow region without a healthy scanner ever reading
# as stale. (A request that actually hangs is bounded by HTTP_TIMEOUT and
# leaves the address's cached entry — and its older scanned_at — in place,
# so a broken endpoint still surfaces as staleness, not as a slow cycle.)
BATCH_LATENCY_ALLOWANCE_SECONDS = 1.0

# Allowance per cycle for the allMids refresh that precedes the batches.
CYCLE_OVERHEAD_ALLOWANCE_SECONDS = 1.0

# Address discovery: every DISCOVERY_INTERVAL_SECONDS scan() pulls recent
# trades on DISCOVERY_SYMBOLS and adds up to DISCOVERY_LIMIT new addresses.
DISCOVERY_INTERVAL_SECONDS = 1800.0
DISCOVERY_LIMIT = 100
DISCOVERY_SYMBOLS = ("BTC", "ETH", "SOL", "DOGE", "ARB", "SUI", "WIF", "PEPE")

# The in-memory tracked set is re-synced from the store right after the hub
# prunes the table (address_store.MAX_TRACKED_ADDRESSES, hourly). Between
# prunes only discovery can grow it, so this is the most addresses a full
# round-robin pass can ever have to cover. Before the re-sync existed the
# set was loaded once and only ever grew — prune() trimmed the TABLE, the
# scanner never noticed, and full-pass time was unbounded.
ADDRESS_PRUNE_INTERVAL_SECONDS = 3600.0
MAX_TRACKED_ADDRESSES_IN_MEMORY = address_store.MAX_TRACKED_ADDRESSES + (
    int(ADDRESS_PRUNE_INTERVAL_SECONDS // DISCOVERY_INTERVAL_SECONDS) + 1
) * DISCOVERY_LIMIT

# Safety factor between the worst-case healthy full pass and "stale".
STALE_MARGIN_FACTOR = 1.5


def scan_cycle_seconds_worst_case(
    budget: int = SCAN_ADDRESS_BUDGET, scan_interval: float = SCAN_INTERVAL_SECONDS,
) -> float:
    """Wall-clock seconds one hub scan cycle takes when every request is
    answered within its latency allowance: the batches, the sleeps between
    them, the price refresh and the hub's idle interval."""
    batches = -(-budget // RATE_LIMIT_PER_SEC)
    return (
        batches * BATCH_LATENCY_ALLOWANCE_SECONDS
        + max(0, batches - 1) * BATCH_SLEEP_SECONDS
        + CYCLE_OVERHEAD_ALLOWANCE_SECONDS
        + scan_interval
    )


def full_pass_seconds_worst_case(
    tracked: int = MAX_TRACKED_ADDRESSES_IN_MEMORY,
    budget: int = SCAN_ADDRESS_BUDGET,
    scan_interval: float = SCAN_INTERVAL_SECONDS,
) -> float:
    """Longest a healthy scanner can take to re-fetch EVERY tracked address
    once: the cycles a round-robin pass over `tracked` needs, plus one
    discovery run (a pass of this length always contains at most one)."""
    cycles = -(-tracked // budget)
    discovery = len(DISCOVERY_SYMBOLS) * BATCH_LATENCY_ALLOWANCE_SECONDS
    return cycles * scan_cycle_seconds_worst_case(budget, scan_interval) + discovery


# A position whose last real scan is older than this — or a scanner whose
# last cycle finished longer ago than this — is stale: its size/entry/liq
# may have changed and its distance is being extrapolated from cached
# state. DERIVED from the worst-case healthy full pass with a safety margin,
# never hand-tuned: the previous hardcoded 600s was 20s above the
# zero-latency pass time, so any real install read "stale" forever.
# tests/test_review_fixes.py::TestB1StalenessBudget pins the relationship.
POSITION_STALE_AFTER_SECONDS = float(math.ceil(full_pass_seconds_worst_case() * STALE_MARGIN_FACTOR))

# Explicit deadline on every request so a hung endpoint fails the scan cycle
# instead of blocking the hub's position-scan loop indefinitely. Split
# connect/read so a slow handshake can't consume the entire budget.
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=3, sock_connect=3, sock_read=5)


def liquidation_distance_pct(side: str, price: float, liq_price: float | None) -> float:
    """Signed distance from `price` to the liquidation price, in percent.

    Positive while the position is on the safe side of its liquidation
    price (a long above it, a short below it), negative once the price has
    crossed it. ``inf`` when there is no liquidation price, or no price.
    """
    if liq_price is None or not price > 0:
        return math.inf
    if side == "long":
        return (price - liq_price) / price * 100
    return (liq_price - price) / price * 100


@dataclass
class TrackedPosition:
    address: str
    symbol: str
    side: str
    size_usd: float
    entry_price: float
    current_price: float
    # Hyperliquid's own liquidation price. None when Hyperliquid reports
    # none (``liquidationPx: null``: the account's equity covers the
    # position at any price); nothing is ever estimated in its place.
    liq_price: float | None
    # Signed (see liquidation_distance_pct): negative means the price has
    # already crossed the liquidation price; inf means there is none.
    distance_pct: float
    leverage: float
    unrealized_pnl: float
    margin_used: float
    # Wall-clock time this position was last fetched from the exchange.
    # current_price/distance_pct may be newer (recomputed from fresh mids);
    # size, entry, liq price and PnL are as of this moment.
    scanned_at: float = 0.0
    # "cross" or "isolated", from Hyperliquid's leverage object.
    margin_mode: str = ""

    @property
    def has_liq_price(self) -> bool:
        return self.liq_price is not None

    @property
    def crossed(self) -> bool:
        """The price is past the liquidation price: the position is being
        (or already was) liquidated and the cached state is out of date."""
        return self.distance_pct < 0

    def near_liquidation(self, threshold_pct: float) -> bool:
        """Within threshold_pct of a real liquidation price, not past it."""
        return 0 <= self.distance_pct <= threshold_pct


@dataclass
class PositionScanner:
    positions: list[TrackedPosition] = field(default_factory=list)
    discovered_addresses: set[str] = field(default_factory=set)
    market_prices: dict[str, float] = field(default_factory=dict)
    scan_budget: int = SCAN_ADDRESS_BUDGET
    # End of the last completed scan() cycle (0 = never).
    last_scan_at: float = 0.0
    # When the round-robin cursor last wrapped (0 = never). Every address
    # that was tracked for the WHOLE rotation has been visited since the
    # previous wrap; one added or removed mid-rotation shifts the sorted
    # order under the cursor, so it may have been skipped or visited twice.
    # Per-position freshness is scanned_at, not this stamp.
    last_full_pass_at: float = 0.0

    _request_times: list[float] = field(default_factory=list, repr=False)
    _session: aiohttp.ClientSession | None = field(default=None, repr=False)
    # address -> positions from its last successful scan (possibly []).
    _position_cache: dict[str, list[TrackedPosition]] = field(default_factory=dict, repr=False)
    _scan_cursor: int = field(default=0, repr=False)

    def __post_init__(self):
        self._load_discovered_addresses()

    # ── Core scan ────────────────────────────────────────────────

    async def scan(self) -> list[TrackedPosition]:
        """One bounded scan cycle.

        Refreshes prices/meta, runs discovery when due, fetches positions for
        the next SCAN_ADDRESS_BUDGET addresses in round-robin order, then
        rebuilds self.positions from the whole cache with distance_pct
        recomputed against the fresh mids. Cycle time is therefore bounded
        by the budget, not by the size of the address store.
        """
        async with aiohttp.ClientSession() as session:
            self._session = session
            try:
                try:
                    await self.update_prices()
                except Exception as exc:
                    logger.warning("[scanner] update_prices failed: %r", exc)

                # Discover new addresses: always on first run, then every
                # DISCOVERY_INTERVAL_SECONDS.
                should_rediscover = (
                    not self.discovered_addresses
                    or (_time.time() - getattr(self, '_last_discovery', 0)) > DISCOVERY_INTERVAL_SECONDS
                )
                if should_rediscover:
                    await self.discover_addresses()
                    self._last_discovery = _time.time()

                # Forget cached positions of addresses that were pruned.
                for addr in [a for a in self._position_cache if a not in self.discovered_addresses]:
                    del self._position_cache[addr]

                # Round-robin slice of the tracked set for this cycle.
                addresses = sorted(self.discovered_addresses)
                n = len(addresses)
                if n:
                    start = self._scan_cursor % n
                    take = min(self.scan_budget, n)
                    slice_ = [addresses[(start + i) % n] for i in range(take)]
                    if start + take >= n:
                        self.last_full_pass_at = _time.time()
                    self._scan_cursor = (start + take) % n

                    for batch_start in range(0, len(slice_), RATE_LIMIT_PER_SEC):
                        batch = slice_[batch_start: batch_start + RATE_LIMIT_PER_SEC]
                        results = await asyncio.gather(
                            *[self.get_positions_for_address(addr) for addr in batch],
                            return_exceptions=True,
                        )
                        fetched_at = _time.time()
                        for addr, result in zip(batch, results):
                            if isinstance(result, list):
                                for p in result:
                                    p.scanned_at = fetched_at
                                self._position_cache[addr] = result
                            # A failed request keeps the previous cached entry
                            # (still carrying its older scanned_at) rather than
                            # silently reading as "no positions".

                        if batch_start + RATE_LIMIT_PER_SEC < len(slice_):
                            await asyncio.sleep(BATCH_SLEEP_SECONDS)

                self.positions = self._assemble_positions()
                self.last_scan_at = _time.time()
                return self.positions
            finally:
                self._session = None

    def _assemble_positions(self) -> list[TrackedPosition]:
        """Every cached position, distance re-derived from the freshest mids.

        Sorted by signed distance: crossed positions (negative) first, then
        the closest to liquidation, then positions with no liquidation price.
        """
        out: list[TrackedPosition] = []
        for plist in self._position_cache.values():
            for p in plist:
                price = self.market_prices.get(p.symbol, 0.0)
                if price > 0:
                    p.current_price = price
                    p.distance_pct = liquidation_distance_pct(p.side, price, p.liq_price)
                out.append(p)
        return sorted(out, key=lambda p: p.distance_pct)

    # ── Freshness ────────────────────────────────────────────────

    def scan_age_seconds(self, now: float | None = None) -> float:
        """Seconds since the last completed cycle (inf if none yet)."""
        if self.last_scan_at <= 0:
            return float("inf")
        return (now if now is not None else time.time()) - self.last_scan_at

    def oldest_position_age_seconds(self, now: float | None = None) -> float:
        """Age of the least recently scanned position on display (0 if none)."""
        if not self.positions:
            return 0.0
        now = now if now is not None else time.time()
        return max(0.0, now - min(p.scanned_at for p in self.positions))

    def is_stale(self, now: float | None = None) -> bool:
        """True when a cycle has run but the data on display is too old to
        trust: the scanner has not completed a cycle within
        POSITION_STALE_AFTER_SECONDS, or some displayed position has not been
        re-fetched within it. Never stale before the first cycle (that is
        'starting', not 'stale')."""
        if self.last_scan_at <= 0:
            return False
        return (self.scan_age_seconds(now) > POSITION_STALE_AFTER_SECONDS
                or self.oldest_position_age_seconds(now) > POSITION_STALE_AFTER_SECONDS)

    @staticmethod
    def as_of(positions: list[TrackedPosition]) -> float | None:
        """Oldest scanned_at among the given positions — the honest 'as of'
        for a response built from them. None when empty or unknown."""
        stamps = [p.scanned_at for p in positions if p.scanned_at > 0]
        return min(stamps) if stamps else None

    def freshness(self, now: float | None = None) -> dict:
        now = now if now is not None else time.time()
        age = self.scan_age_seconds(now)
        return {
            "last_scan_at": self.last_scan_at or None,
            "scan_age_seconds": None if age == float("inf") else round(age, 1),
            "oldest_position_age_seconds": round(self.oldest_position_age_seconds(now), 1),
            "last_full_pass_at": self.last_full_pass_at or None,
            "tracked_addresses": len(self.discovered_addresses),
            "scan_budget_per_cycle": self.scan_budget,
            "stale_after_seconds": POSITION_STALE_AFTER_SECONDS,
            "full_pass_worst_case_seconds": full_pass_seconds_worst_case(),
            "stale": self.is_stale(now),
        }

    # ── Address discovery ────────────────────────────────────────

    async def discover_addresses(self, limit: int = DISCOVERY_LIMIT) -> set[str]:
        """Discover active trader addresses from recent trades on popular markets."""
        new_addresses: set[str] = set()

        for symbol in DISCOVERY_SYMBOLS:
            if len(new_addresses) >= limit:
                break
            try:
                data = await self._post({
                    "type": "recentTrades",
                    "coin": symbol,
                })
                if isinstance(data, list):
                    for trade in data:
                        # Validate at the boundary: exchange payloads are
                        # untrusted, and a junk identifier persisted here gets
                        # re-scanned (one API call per cycle) forever.
                        candidates: list[object] = []
                        for side_key in ("buyer", "seller", "users"):
                            if side_key in trade and isinstance(trade[side_key], str):
                                candidates.append(trade[side_key])
                        if "users" in trade and isinstance(trade["users"], list):
                            candidates.extend(trade["users"])
                        for addr in candidates:
                            if address_store.is_valid_address(addr):
                                new_addresses.add(address_store.normalize_address(addr))
                        if len(new_addresses) >= limit:
                            break
            except Exception:
                continue

        self.discovered_addresses.update(new_addresses)
        # Persist only what this cycle found — the store is an upsert per
        # row, so re-writing the whole known set every 30 minutes was O(N)
        # event-loop work for nothing.
        if new_addresses:
            address_store.add_addresses(new_addresses, source="position_scanner")
        return self.discovered_addresses

    # ── Per-address positions ────────────────────────────────────

    async def get_positions_for_address(self, address: str) -> list[TrackedPosition]:
        """Get all open positions for a single address with liquidation data."""
        await self._rate_limit()

        data = await self._post({
            "type": "clearinghouseState",
            "user": address,
        })

        if not data or "assetPositions" not in data:
            return []

        positions: list[TrackedPosition] = []
        margin_summary = data.get("marginSummary", {})
        total_margin_used = float(margin_summary.get("totalMarginUsed", 0))

        for asset_pos in data["assetPositions"]:
            pos = asset_pos.get("position", {})
            coin = pos.get("coin", "")
            szi = float(pos.get("szi", 0))

            if szi == 0:
                continue

            side = "long" if szi > 0 else "short"
            entry_price = float(pos.get("entryPx", 0))
            position_value = float(pos.get("positionValue", 0))
            unrealized_pnl = float(pos.get("unrealizedPnl", 0))

            leverage_info = pos.get("leverage", {})
            leverage_value = float(leverage_info.get("value", 1)) if isinstance(leverage_info, dict) else 1.0
            margin_mode = str(leverage_info.get("type", "")) if isinstance(leverage_info, dict) else ""

            # Hyperliquid's liquidation price, or None when it reports none
            # (null: the account's equity covers the position at any price).
            # Never estimated: an isolated-margin formula on the leverage
            # setting invented levels for cross positions that cannot be
            # liquidated at all.
            liq_price_raw = pos.get("liquidationPx")
            liq_price = float(liq_price_raw) if liq_price_raw not in (None, "") else None
            if liq_price is not None and not (math.isfinite(liq_price) and liq_price > 0):
                liq_price = None

            current_price = self.market_prices.get(coin, entry_price)
            distance_pct = liquidation_distance_pct(side, current_price, liq_price)

            num_positions = max(len(data["assetPositions"]), 1)
            margin_used = total_margin_used / num_positions

            positions.append(TrackedPosition(
                address=address,
                symbol=coin,
                side=side,
                size_usd=position_value,
                entry_price=entry_price,
                current_price=current_price,
                liq_price=liq_price,
                distance_pct=distance_pct,
                leverage=leverage_value,
                unrealized_pnl=unrealized_pnl,
                margin_used=margin_used,
                margin_mode=margin_mode,
            ))

        return positions

    # ── Filtering / query helpers ────────────────────────────────
    # Distance views only ever contain positions with a real liquidation
    # price that the price has not crossed. A crossed position is being (or
    # already was) liquidated and its cached state is out of date until its
    # address is scanned again; get_crossed() lists those separately.

    def get_danger_zone(self, threshold_pct: float = 2.0) -> list[TrackedPosition]:
        """Positions within threshold% of their liquidation price (not past it)."""
        return [p for p in self.positions if p.near_liquidation(threshold_pct)]

    def get_crossed(self) -> list[TrackedPosition]:
        """Positions whose price is already past their liquidation price."""
        return [p for p in self.positions if p.crossed]

    def get_closest_longs(self, n: int = 3) -> list[TrackedPosition]:
        """Get N long positions closest to liquidation."""
        longs = [p for p in self.positions if p.side == "long" and p.distance_pct >= 0 and p.has_liq_price]
        return sorted(longs, key=lambda p: p.distance_pct)[:n]

    def get_closest_shorts(self, n: int = 3) -> list[TrackedPosition]:
        """Get N short positions closest to liquidation."""
        shorts = [p for p in self.positions if p.side == "short" and p.distance_pct >= 0 and p.has_liq_price]
        return sorted(shorts, key=lambda p: p.distance_pct)[:n]

    def get_zone_summary(self) -> dict:
        """Return summary of positions grouped by distance-to-liquidation zones."""
        zones = {
            "within_1pct": {"count": 0, "total_value": 0.0},
            "within_2pct": {"count": 0, "total_value": 0.0},
            "within_5pct": {"count": 0, "total_value": 0.0},
        }
        for p in self.positions:
            for limit, key in ((1.0, "within_1pct"), (2.0, "within_2pct"), (5.0, "within_5pct")):
                if p.near_liquidation(limit):
                    zones[key]["count"] += 1
                    zones[key]["total_value"] += p.size_usd
        return zones

    # ── Price & meta updates ─────────────────────────────────────

    async def update_prices(self):
        """Fetch latest mid prices for all assets."""
        data = await self._post({"type": "allMids"})
        if isinstance(data, dict):
            self.market_prices = {k: float(v) for k, v in data.items()}

    # ── Rate limiter ─────────────────────────────────────────────

    async def _rate_limit(self):
        now = time.monotonic()
        self._request_times = [t for t in self._request_times if now - t < 1.0]
        if len(self._request_times) >= RATE_LIMIT_PER_SEC:
            sleep_time = 1.0 - (now - self._request_times[0])
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
        self._request_times.append(time.monotonic())

    # ── HTTP helper ──────────────────────────────────────────────

    async def _post(self, payload: dict) -> dict | list | None:
        if self._session is None:
            raise RuntimeError("No active aiohttp session — use scan() or create one manually")
        async with self._session.post(
            API_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=HTTP_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    # ── Address persistence (SQLite-backed) ──────────────────────

    def _load_discovered_addresses(self):
        """Load addresses from the SQLite store.

        Propagates a read failure: an unreadable store must fail loudly at
        construction, not be mistaken for "no addresses yet".
        """
        self.discovered_addresses = address_store.get_all_addresses()

    async def resync_addresses(self) -> int:
        """Make the in-memory tracked set match the store again.

        The set is loaded once at construction and grows with discovery;
        address_store.prune() trims the TABLE, so without this the scanner
        kept scanning pruned addresses forever and full-pass time was
        unbounded (B1). The hub calls this right after each prune. The read
        runs off the event loop; anything discovered while it ran is kept
        (it is in the store too). Returns how many addresses were dropped.
        """
        before = set(self.discovered_addresses)
        in_store = await asyncio.to_thread(address_store.get_all_addresses)
        discovered_meanwhile = self.discovered_addresses - before
        self.discovered_addresses = in_store | discovered_meanwhile
        dropped = len(before - self.discovered_addresses)
        # scan() also does this each cycle; do it now so the very next
        # freshness() reflects the pruned set.
        for addr in [a for a in self._position_cache if a not in self.discovered_addresses]:
            del self._position_cache[addr]
        return dropped

    def add_addresses(self, addresses: list[str]):
        """Manually add addresses to track (validated + normalized)."""
        valid = [
            address_store.normalize_address(a)
            for a in addresses if address_store.is_valid_address(a)
        ]
        self.discovered_addresses.update(valid)
        address_store.add_addresses(valid, source="position_scanner_manual")
