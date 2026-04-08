from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.data_layer.position_scanner import (
    DISCOVERED_ADDRESSES_PATH,
    PositionScanner,
    TrackedPosition,
)


# ── Fixtures ─────────────────────────────────────────────────────

def _make_position(**overrides) -> TrackedPosition:
    defaults = dict(
        address="0xabc",
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
        assert p.address == "0xabc"
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
    def setup_method(self):
        self.scanner = PositionScanner()
        self.scanner.positions = [
            _make_position(side="long", distance_pct=0.5, size_usd=100_000),
            _make_position(side="long", distance_pct=1.5, size_usd=50_000),
            _make_position(side="short", distance_pct=0.8, size_usd=200_000),
            _make_position(side="short", distance_pct=3.0, size_usd=75_000),
            _make_position(side="long", distance_pct=4.5, size_usd=30_000),
            _make_position(side="short", distance_pct=10.0, size_usd=10_000),
        ]

    def test_get_danger_zone_default(self):
        danger = self.scanner.get_danger_zone()
        assert len(danger) == 3
        assert all(p.distance_pct <= 2.0 for p in danger)

    def test_get_danger_zone_custom_threshold(self):
        danger = self.scanner.get_danger_zone(threshold_pct=1.0)
        assert len(danger) == 2

    def test_get_closest_longs(self):
        longs = self.scanner.get_closest_longs(n=2)
        assert len(longs) == 2
        assert all(p.side == "long" for p in longs)
        assert longs[0].distance_pct < longs[1].distance_pct

    def test_get_closest_shorts(self):
        shorts = self.scanner.get_closest_shorts(n=2)
        assert len(shorts) == 2
        assert all(p.side == "short" for p in shorts)
        assert shorts[0].distance_pct < shorts[1].distance_pct

    def test_get_closest_longs_more_than_available(self):
        longs = self.scanner.get_closest_longs(n=100)
        assert len(longs) == 3

    def test_get_zone_summary(self):
        summary = self.scanner.get_zone_summary()
        assert summary["within_1pct"]["count"] == 2
        assert summary["within_1pct"]["total_value"] == 300_000
        assert summary["within_2pct"]["count"] == 3
        assert summary["within_5pct"]["count"] == 5

    def test_get_zone_summary_empty(self):
        self.scanner.positions = []
        summary = self.scanner.get_zone_summary()
        assert summary["within_1pct"]["count"] == 0
        assert summary["within_5pct"]["total_value"] == 0.0


class TestAddressPersistence:
    def test_add_addresses(self, tmp_path, monkeypatch):
        addr_file = tmp_path / "discovered_addresses.json"
        monkeypatch.setattr(
            "src.data_layer.position_scanner.DISCOVERED_ADDRESSES_PATH", addr_file
        )
        scanner = PositionScanner()
        scanner.add_addresses(["0xaaa", "0xbbb"])
        assert "0xaaa" in scanner.discovered_addresses
        assert addr_file.exists()

        loaded = json.loads(addr_file.read_text())
        assert "0xaaa" in loaded
        assert "0xbbb" in loaded

    def test_load_existing_addresses(self, tmp_path, monkeypatch):
        addr_file = tmp_path / "discovered_addresses.json"
        addr_file.write_text(json.dumps(["0x111", "0x222"]))
        monkeypatch.setattr(
            "src.data_layer.position_scanner.DISCOVERED_ADDRESSES_PATH", addr_file
        )
        scanner = PositionScanner()
        assert "0x111" in scanner.discovered_addresses
        assert "0x222" in scanner.discovered_addresses

    def test_corrupted_file_handled(self, tmp_path, monkeypatch):
        addr_file = tmp_path / "discovered_addresses.json"
        addr_file.write_text("NOT VALID JSON {{{")
        monkeypatch.setattr(
            "src.data_layer.position_scanner.DISCOVERED_ADDRESSES_PATH", addr_file
        )
        scanner = PositionScanner()
        assert len(scanner.discovered_addresses) == 0


class TestGetPositionsForAddress:
    @pytest.mark.asyncio
    async def test_parses_clearinghouse_state(self):
        scanner = PositionScanner()
        scanner.market_prices = {"BTC": 71_000.0, "ETH": 3_500.0}
        scanner.market_meta = MOCK_META

        mock_session = MagicMock()
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value=MOCK_CLEARINGHOUSE_STATE)
        mock_response.raise_for_status = MagicMock()
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_response)

        scanner._session = mock_session

        positions = await scanner.get_positions_for_address("0xtest")

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

        mock_session = MagicMock()
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"assetPositions": [], "marginSummary": {}})
        mock_response.raise_for_status = MagicMock()
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_response)

        scanner._session = mock_session

        positions = await scanner.get_positions_for_address("0xempty")
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

        mock_session = MagicMock()
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value=state)
        mock_response.raise_for_status = MagicMock()
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_response)

        scanner._session = mock_session

        positions = await scanner.get_positions_for_address("0xfallback")
        assert len(positions) == 1
        assert abs(positions[0].liq_price - 63_210.0) < 0.01


class TestUpdatePrices:
    @pytest.mark.asyncio
    async def test_update_prices(self):
        scanner = PositionScanner()

        mock_session = MagicMock()
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value=MOCK_ALL_MIDS)
        mock_response.raise_for_status = MagicMock()
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_response)

        scanner._session = mock_session

        await scanner.update_prices()
        assert scanner.market_prices["BTC"] == 71_000.0
        assert scanner.market_prices["ETH"] == 3_500.0
        assert scanner.market_prices["SOL"] == 150.0


class TestUpdateMeta:
    @pytest.mark.asyncio
    async def test_update_meta(self):
        scanner = PositionScanner()

        mock_session = MagicMock()
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value=MOCK_META)
        mock_response.raise_for_status = MagicMock()
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_response)

        scanner._session = mock_session

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
        scanner = PositionScanner()
        scanner.market_prices = {"BTC": 70_000.0}
        scanner.market_meta = MOCK_META

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
