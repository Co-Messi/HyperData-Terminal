from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from src.data_layer import address_store

API_URL = "https://api.hyperliquid.xyz/info"
DATA_DIR = Path(__file__).resolve().parents[2] / "data"

RATE_LIMIT_PER_SEC = 10
META_CACHE_TTL = 300  # 5 minutes


@dataclass
class TrackedPosition:
    address: str
    symbol: str
    side: str
    size_usd: float
    entry_price: float
    current_price: float
    liq_price: float
    distance_pct: float
    leverage: float
    unrealized_pnl: float
    margin_used: float


@dataclass
class PositionScanner:
    positions: list[TrackedPosition] = field(default_factory=list)
    discovered_addresses: set[str] = field(default_factory=set)
    market_prices: dict[str, float] = field(default_factory=dict)
    market_meta: dict = field(default_factory=dict)

    _meta_updated_at: float = field(default=0.0, repr=False)
    _request_times: list[float] = field(default_factory=list, repr=False)
    _session: aiohttp.ClientSession | None = field(default=None, repr=False)

    def __post_init__(self):
        self._load_discovered_addresses()

    # ── Core scan ────────────────────────────────────────────────

    async def scan(self) -> list[TrackedPosition]:
        """Full scan: update prices, scan all known addresses, return sorted by distance."""
        async with aiohttp.ClientSession() as session:
            self._session = session
            try:
                await asyncio.gather(self.update_prices(), self.update_meta())

                # Discover new addresses: always on first run, then every 30 minutes
                import time as _time
                should_rediscover = (
                    not self.discovered_addresses
                    or (_time.time() - getattr(self, '_last_discovery', 0)) > 1800
                )
                if should_rediscover:
                    await self.discover_addresses()
                    self._last_discovery = _time.time()

                all_positions: list[TrackedPosition] = []
                addresses = list(self.discovered_addresses)

                for batch_start in range(0, len(addresses), RATE_LIMIT_PER_SEC):
                    batch = addresses[batch_start : batch_start + RATE_LIMIT_PER_SEC]
                    results = await asyncio.gather(
                        *[self.get_positions_for_address(addr) for addr in batch],
                        return_exceptions=True,
                    )
                    for result in results:
                        if isinstance(result, list):
                            all_positions.extend(result)

                    if batch_start + RATE_LIMIT_PER_SEC < len(addresses):
                        await asyncio.sleep(1.0)

                self.positions = sorted(all_positions, key=lambda p: p.distance_pct)
                return self.positions
            finally:
                self._session = None

    # ── Address discovery ────────────────────────────────────────

    async def discover_addresses(self, limit: int = 100) -> set[str]:
        """Discover active trader addresses from recent trades on popular markets."""
        symbols = ["BTC", "ETH", "SOL", "DOGE", "ARB", "SUI", "WIF", "PEPE"]
        new_addresses: set[str] = set()

        for symbol in symbols:
            if len(new_addresses) >= limit:
                break
            try:
                data = await self._post({
                    "type": "recentTrades",
                    "coin": symbol,
                })
                if isinstance(data, list):
                    for trade in data:
                        for side_key in ("buyer", "seller", "users"):
                            if side_key in trade and isinstance(trade[side_key], str):
                                new_addresses.add(trade[side_key])
                        if "users" in trade and isinstance(trade["users"], list):
                            for addr in trade["users"]:
                                new_addresses.add(addr)
                        if len(new_addresses) >= limit:
                            break
            except Exception:
                continue

        self.discovered_addresses.update(new_addresses)
        self._save_discovered_addresses()
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

            liq_price_raw = pos.get("liquidationPx")
            if liq_price_raw is not None and liq_price_raw != "":
                liq_price = float(liq_price_raw)
            else:
                liq_price = self._calculate_liq_price(
                    side, entry_price, leverage_value, coin
                )

            current_price = self.market_prices.get(coin, entry_price)

            if current_price > 0:
                distance_pct = abs(current_price - liq_price) / current_price * 100
            else:
                distance_pct = float("inf")

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
            ))

        return positions

    # ── Filtering / query helpers ────────────────────────────────

    def get_danger_zone(self, threshold_pct: float = 2.0) -> list[TrackedPosition]:
        """Get all positions within threshold% of liquidation."""
        return [p for p in self.positions if p.distance_pct <= threshold_pct]

    def get_closest_longs(self, n: int = 3) -> list[TrackedPosition]:
        """Get N long positions closest to liquidation."""
        longs = [p for p in self.positions if p.side == "long"]
        return sorted(longs, key=lambda p: p.distance_pct)[:n]

    def get_closest_shorts(self, n: int = 3) -> list[TrackedPosition]:
        """Get N short positions closest to liquidation."""
        shorts = [p for p in self.positions if p.side == "short"]
        return sorted(shorts, key=lambda p: p.distance_pct)[:n]

    def get_zone_summary(self) -> dict:
        """Return summary of positions grouped by distance-to-liquidation zones."""
        zones = {
            "within_1pct": {"count": 0, "total_value": 0.0},
            "within_2pct": {"count": 0, "total_value": 0.0},
            "within_5pct": {"count": 0, "total_value": 0.0},
        }
        for p in self.positions:
            if p.distance_pct <= 1.0:
                zones["within_1pct"]["count"] += 1
                zones["within_1pct"]["total_value"] += p.size_usd
            if p.distance_pct <= 2.0:
                zones["within_2pct"]["count"] += 1
                zones["within_2pct"]["total_value"] += p.size_usd
            if p.distance_pct <= 5.0:
                zones["within_5pct"]["count"] += 1
                zones["within_5pct"]["total_value"] += p.size_usd
        return zones

    # ── Price & meta updates ─────────────────────────────────────

    async def update_prices(self):
        """Fetch latest mid prices for all assets."""
        data = await self._post({"type": "allMids"})
        if isinstance(data, dict):
            self.market_prices = {k: float(v) for k, v in data.items()}

    async def update_meta(self):
        """Fetch market metadata (maintenance margins, etc). Cached for 5 min."""
        now = time.monotonic()
        if self.market_meta and (now - self._meta_updated_at) < META_CACHE_TTL:
            return

        data = await self._post({"type": "meta"})
        if isinstance(data, dict):
            self.market_meta = data
            self._meta_updated_at = now

    # ── Liquidation price fallback ───────────────────────────────

    def _calculate_liq_price(
        self, side: str, entry_price: float, leverage: float, coin: str
    ) -> float:
        mm_rate = self._get_maintenance_margin(coin)
        if leverage == 0:
            return 0.0
        if side == "long":
            return entry_price * (1 - 1 / leverage + mm_rate / leverage)
        return entry_price * (1 + 1 / leverage - mm_rate / leverage)

    def _get_maintenance_margin(self, coin: str) -> float:
        """Look up maintenance margin rate from cached metadata."""
        universe = self.market_meta.get("universe", [])
        for asset in universe:
            if asset.get("name") == coin:
                return float(asset.get("maintenanceMarginRatio", 0.03))
        return 0.03  # default 3%

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
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    # ── Address persistence (SQLite-backed) ──────────────────────

    def _load_discovered_addresses(self):
        """Load addresses from SQLite store."""
        self.discovered_addresses = address_store.get_all_addresses()

    def _save_discovered_addresses(self):
        """Persist current in-memory set to SQLite."""
        address_store.add_addresses(self.discovered_addresses, source="position_scanner")

    def add_addresses(self, addresses: list[str]):
        """Manually add addresses to track."""
        self.discovered_addresses.update(addresses)
        address_store.add_addresses(addresses, source="position_scanner_manual")
