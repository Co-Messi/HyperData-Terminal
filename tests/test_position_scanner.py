"""PositionScanner unit tests.

All network calls are mocked; the SQLite-backed address store is redirected to
a per-test temp directory so tests never touch the repo's data/ files.
"""
from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_layer.position_scanner import PositionScanner, TrackedPosition
from src.data_layer import address_store

# ── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_address_store(tmp_path, monkeypatch):
    """Point the address store at a temp SQLite DB for every test."""
    monkeypatch.setattr(address_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(address_store, "DB_PATH", tmp_path / "hyperdata.db")
    monkeypatch.setattr(address_store, "LEGACY_JSON", tmp_path / "discovered_addresses.json")
    monkeypatch.setattr(address_store, "_initialized", False)
    yield tmp_path


def _addr(seed: str) -> str:
    """Build a valid 0x + 40-hex wallet address from a short seed."""
    return "0x" + (seed * 40)[:40]


def _make_position(**overrides) -> TrackedPosition:
    defaults = dict(
        address=_addr("abc"),
        symbol="BTC",
        side="long",
        size_usd=50_000.0,
        entry_price=70_000.0,
        current_price=71_000.0,
        liq_price=63_000.0,
        distance_pct=11.27,
        leverage=10.0,
        unrealized_pnl=500.0,
        margin_used=5_000.0,
    )
    defaults.update(overrides)
    return TrackedPosition(**defaults)


def _mock_session(json_payload) -> MagicMock:
    session = MagicMock()
    response = AsyncMock()
    response.json = AsyncMock(return_value=json_payload)
    response.raise_for_status = MagicMock()
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(return_value=response)
    return session


MOCK_ALL_MIDS = {"BTC": "71000.0", "ETH": "3500.0", "SOL": "150.0"}

MOCK_META = {
    "universe": [
        {"name": "BTC", "maintenanceMarginRatio": "0.03"},
        {"name": "ETH", "maintenanceMarginRatio": "0.03"},
        {"name": "SOL", "maintenanceMarginRatio": "0.05"},
    ]
}

MOCK_CLEARINGHOUSE_STATE = {
    "assetPositions": [
        {
            "position": {
                "coin": "BTC",
                "szi": "0.5",
                "entryPx": "70000",
                "positionValue": "35000",
                "unrealizedPnl": "500",
                "leverage": {"type": "cross", "value": 10},
                "liquidationPx": "63500",
            }
        },
        {
            "position": {
                "coin": "ETH",
                "szi": "-5.0",
                "entryPx": "3400",
                "positionValue": "17000",
                "unrealizedPnl": "-200",
                "leverage": {"type": "cross", "value": 5},
                "liquidationPx": "3740",
            }
        },
    ],
    "marginSummary": {"totalMarginUsed": "10000"},
}


# ── Unit tests ───────────────────────────────────────────────────

class TestTrackedPosition:
    def test_creation(self):
        p = _make_position()
        assert p.address == _addr("abc")
        assert p.side == "long"
        assert p.leverage == 10.0

    def test_fields_override(self):
        p = _make_position(side="short", symbol="ETH", distance_pct=1.5)
        assert p.side == "short"
        assert p.symbol == "ETH"
        assert p.distance_pct == 1.5


class TestLiquidationPriceCalculation:
    def test_long_liq_price(self):
        scanner = PositionScanner()
        scanner.market_meta = MOCK_META
        liq = scanner._calculate_liq_price("long", 70_000.0, 10.0, "BTC")
        # liq = 70000 * (1 - 1/10 + 0.03/10) = 70000 * 0.903 = 63210
        assert abs(liq - 63_210.0) < 0.01

    def test_short_liq_price(self):
        scanner = PositionScanner()
        scanner.market_meta = MOCK_META
        liq = scanner._calculate_liq_price("short", 70_000.0, 10.0, "BTC")
        # liq = 70000 * (1 + 1/10 - 0.03/10) = 70000 * 1.097 = 76790
        assert abs(liq - 76_790.0) < 0.01

    def test_zero_leverage_returns_zero(self):
        scanner = PositionScanner()
        scanner.market_meta = MOCK_META
        assert scanner._calculate_liq_price("long", 70_000.0, 0.0, "BTC") == 0.0

    def test_unknown_coin_uses_default_mm(self):
        scanner = PositionScanner()
        scanner.market_meta = MOCK_META
        liq = scanner._calculate_liq_price("long", 100.0, 5.0, "UNKNOWN")
        # default mm = 0.03, liq = 100 * (1 - 1/5 + 0.03/5) = 100 * 0.806 = 80.6
        assert abs(liq - 80.6) < 0.01

    def test_high_mm_sol(self):
        scanner = PositionScanner()
        scanner.market_meta = MOCK_META
        liq = scanner._calculate_liq_price("long", 150.0, 10.0, "SOL")
        # mm=0.05, liq = 150 * (1 - 0.1 + 0.005) = 150 * 0.905 = 135.75
        assert abs(liq - 135.75) < 0.01


class TestMaintenanceMarginLookup:
    def test_known_coin(self):
        scanner = PositionScanner()
        scanner.market_meta = MOCK_META
        assert scanner._get_maintenance_margin("BTC") == 0.03
        assert scanner._get_maintenance_margin("SOL") == 0.05

    def test_unknown_coin_defaults(self):
        scanner = PositionScanner()
        scanner.market_meta = MOCK_META
        assert scanner._get_maintenance_margin("MEME") == 0.03


class TestFilterMethods:
    def setup_positions(self):
        scanner = PositionScanner()
        scanner.positions = [
            _make_position(side="long", distance_pct=0.5, size_usd=100_000),
            _make_position(side="long", distance_pct=1.5, size_usd=50_000),
            _make_position(side="short", distance_pct=0.8, size_usd=200_000),
            _make_position(side="short", distance_pct=3.0, size_usd=75_000),
            _make_position(side="long", distance_pct=4.5, size_usd=30_000),
            _make_position(side="short", distance_pct=10.0, size_usd=10_000),
        ]
        return scanner

    def test_get_danger_zone_default(self):
        danger = self.setup_positions().get_danger_zone()
        assert len(danger) == 3
        assert all(p.distance_pct <= 2.0 for p in danger)

    def test_get_danger_zone_custom_threshold(self):
        danger = self.setup_positions().get_danger_zone(threshold_pct=1.0)
        assert len(danger) == 2

    def test_get_closest_longs(self):
        longs = self.setup_positions().get_closest_longs(n=2)
        assert len(longs) == 2
        assert all(p.side == "long" for p in longs)
        assert longs[0].distance_pct < longs[1].distance_pct

    def test_get_closest_shorts(self):
        shorts = self.setup_positions().get_closest_shorts(n=2)
        assert len(shorts) == 2
        assert all(p.side == "short" for p in shorts)
        assert shorts[0].distance_pct < shorts[1].distance_pct

    def test_get_closest_longs_more_than_available(self):
        longs = self.setup_positions().get_closest_longs(n=100)
        assert len(longs) == 3

    def test_get_zone_summary(self):
        summary = self.setup_positions().get_zone_summary()
        assert summary["within_1pct"]["count"] == 2
        assert summary["within_1pct"]["total_value"] == 300_000
        assert summary["within_2pct"]["count"] == 3
        assert summary["within_5pct"]["count"] == 5

    def test_get_zone_summary_empty(self):
        scanner = self.setup_positions()
        scanner.positions = []
        summary = scanner.get_zone_summary()
        assert summary["within_1pct"]["count"] == 0
        assert summary["within_5pct"]["total_value"] == 0.0


class TestAddressPersistence:
    """Address persistence is SQLite-backed (data_layer.address_store)."""

    def test_add_addresses_persists_to_sqlite(self, isolated_address_store):
        scanner = PositionScanner()
        a1, a2 = _addr("aaa"), _addr("bbb")
        scanner.add_addresses([a1, a2])

        assert a1 in scanner.discovered_addresses
        assert a2 in scanner.discovered_addresses

        # Rows actually landed in the discovered_addresses table.
        conn = sqlite3.connect(str(isolated_address_store / "hyperdata.db"))
        rows = {
            r[0] for r in
            conn.execute("SELECT address FROM discovered_addresses").fetchall()
        }
        conn.close()
        assert {a1, a2} <= rows

    def test_load_existing_addresses(self):
        a1, a2 = _addr("111"), _addr("222")
        address_store.add_addresses([a1, a2], source="test")

        scanner = PositionScanner()
        assert a1 in scanner.discovered_addresses
        assert a2 in scanner.discovered_addresses

    def test_invalid_addresses_rejected(self, isolated_address_store):
        scanner = PositionScanner()
        scanner.add_addresses([
            "not-an-address",
            "0xTOOSHORT",
            "0x" + "g" * 40,        # non-hex
            _addr("c0ffee"),         # the only valid one
        ])
        assert scanner.discovered_addresses == {_addr("c0ffee")}

        conn = sqlite3.connect(str(isolated_address_store / "hyperdata.db"))
        count = conn.execute("SELECT COUNT(*) FROM discovered_addresses").fetchone()[0]
        conn.close()
        assert count == 1

    def test_addresses_normalized_to_lowercase(self):
        mixed = "0x" + "AbCdEf0123456789aBcDeF0123456789ABCDEF01"
        scanner = PositionScanner()
        scanner.add_addresses([mixed])
        assert mixed.lower() in scanner.discovered_addresses
        assert mixed not in scanner.discovered_addresses

    def test_retention_cap_expires_oldest(self, monkeypatch):
        monkeypatch.setattr(address_store, "MAX_TRACKED_ADDRESSES", 3)
        for i in range(5):
            address_store.add_addresses([_addr(f"{i}{i}{i}")], source="test")
        remaining = address_store.get_all_addresses()
        assert len(remaining) == 3

    def test_store_validator(self):
        assert address_store.is_valid_address(_addr("abc"))
        assert not address_store.is_valid_address("0xabc")
        assert not address_store.is_valid_address(None)
        assert not address_store.is_valid_address(42)
        assert not address_store.is_valid_address("0x" + "z" * 40)


class TestGetPositionsForAddress:
    @pytest.mark.asyncio
    async def test_parses_clearinghouse_state(self):
        scanner = PositionScanner()
        scanner.market_prices = {"BTC": 71_000.0, "ETH": 3_500.0}
        scanner.market_meta = MOCK_META
        scanner._session = _mock_session(MOCK_CLEARINGHOUSE_STATE)

        positions = await scanner.get_positions_for_address(_addr("fed"))

        assert len(positions) == 2

        btc_pos = next(p for p in positions if p.symbol == "BTC")
        assert btc_pos.side == "long"
        assert btc_pos.entry_price == 70_000.0
        assert btc_pos.liq_price == 63_500.0
        assert btc_pos.leverage == 10.0
        assert btc_pos.size_usd == 35_000.0

        eth_pos = next(p for p in positions if p.symbol == "ETH")
        assert eth_pos.side == "short"
        assert eth_pos.liq_price == 3_740.0

    @pytest.mark.asyncio
    async def test_empty_positions(self):
        scanner = PositionScanner()
        scanner.market_prices = {}
        scanner.market_meta = MOCK_META
        scanner._session = _mock_session({"assetPositions": [], "marginSummary": {}})

        positions = await scanner.get_positions_for_address(_addr("e"))
        assert positions == []

    @pytest.mark.asyncio
    async def test_fallback_liq_price_when_missing(self):
        state = {
            "assetPositions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "1.0",
                        "entryPx": "70000",
                        "positionValue": "70000",
                        "unrealizedPnl": "0",
                        "leverage": {"type": "cross", "value": 10},
                        "liquidationPx": None,
                    }
                }
            ],
            "marginSummary": {"totalMarginUsed": "7000"},
        }

        scanner = PositionScanner()
        scanner.market_prices = {"BTC": 70_000.0}
        scanner.market_meta = MOCK_META
        scanner._session = _mock_session(state)

        positions = await scanner.get_positions_for_address(_addr("f"))
        assert len(positions) == 1
        assert abs(positions[0].liq_price - 63_210.0) < 0.01


class TestDiscoverAddresses:
    @pytest.mark.asyncio
    async def test_discovery_validates_payload_addresses(self):
        """Junk strings in exchange trade payloads must never be persisted."""
        good = _addr("dead")
        trades = [
            {"buyer": good, "seller": "junk-string"},
            {"users": [good, "0xshort", 12345, None]},
            {"users": "not-a-list"},
        ]
        scanner = PositionScanner()
        scanner._session = _mock_session(trades)

        discovered = await scanner.discover_addresses(limit=10)
        assert good in discovered
        assert "junk-string" not in discovered
        assert "0xshort" not in discovered
        assert address_store.get_all_addresses() == {good}


class TestUpdatePrices:
    @pytest.mark.asyncio
    async def test_update_prices(self):
        scanner = PositionScanner()
        scanner._session = _mock_session(MOCK_ALL_MIDS)

        await scanner.update_prices()
        assert scanner.market_prices["BTC"] == 71_000.0
        assert scanner.market_prices["ETH"] == 3_500.0
        assert scanner.market_prices["SOL"] == 150.0


class TestUpdateMeta:
    @pytest.mark.asyncio
    async def test_update_meta(self):
        scanner = PositionScanner()
        scanner._session = _mock_session(MOCK_META)

        await scanner.update_meta()
        assert scanner.market_meta == MOCK_META
        assert scanner._meta_updated_at > 0

    @pytest.mark.asyncio
    async def test_meta_caching(self):
        scanner = PositionScanner()
        scanner.market_meta = MOCK_META
        scanner._meta_updated_at = float("inf")

        mock_session = MagicMock()
        mock_session.post = MagicMock()
        scanner._session = mock_session

        await scanner.update_meta()
        mock_session.post.assert_not_called()


class TestDistanceCalculation:
    def test_distance_for_long(self):
        # liq at 63500, current at 70000
        # distance = |70000 - 63500| / 70000 * 100 = 9.2857%
        current = 70_000.0
        liq = 63_500.0
        expected = abs(current - liq) / current * 100
        assert abs(expected - 9.2857) < 0.01

    def test_distance_for_short(self):
        current = 3_500.0
        liq = 3_740.0
        expected = abs(current - liq) / current * 100
        assert abs(expected - 6.857) < 0.01


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_rate_limit_allows_under_limit(self):
        scanner = PositionScanner()
        scanner._request_times = []
        await scanner._rate_limit()
        assert len(scanner._request_times) == 1

    @pytest.mark.asyncio
    async def test_rate_limit_tracks_requests(self):
        scanner = PositionScanner()
        for _ in range(5):
            await scanner._rate_limit()
        assert len(scanner._request_times) == 5


class TestPostTimeout:
    @pytest.mark.asyncio
    async def test_post_sends_explicit_timeout(self):
        """Every outbound request must carry an explicit deadline (High 4)."""
        scanner = PositionScanner()
        session = _mock_session({})
        scanner._session = session
        await scanner._post({"type": "allMids"})
        _, kwargs = session.post.call_args
        assert kwargs.get("timeout") is not None
        assert kwargs["timeout"].total == 10
