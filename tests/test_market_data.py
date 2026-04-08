"""Tests for MarketData and AssetInfo."""
from __future__ import annotations

import pytest
from data_layer.market_data import AssetInfo, _timeframe_to_ms


def test_asset_info_premium_pct_positive():
    asset = AssetInfo(
        symbol="BTC",
        price=83500.0,
        funding_rate=0.0001,
        open_interest=1_000_000_000.0,
        volume_24h=5_000_000_000.0,
        price_change_24h_pct=0.02,
        mark_price=83500.0,
        index_price=83000.0,
        premium_pct=(83500.0 - 83000.0) / 83000.0 * 100,
    )
    assert asset.premium_pct == pytest.approx((83500.0 - 83000.0) / 83000.0 * 100)
    assert asset.premium_pct > 0


def test_asset_info_premium_pct_negative():
    asset = AssetInfo(
        symbol="ETH",
        price=3450.0,
        funding_rate=-0.0001,
        open_interest=500_000_000.0,
        volume_24h=2_000_000_000.0,
        price_change_24h_pct=-0.01,
        mark_price=3440.0,
        index_price=3450.0,
        premium_pct=(3440.0 - 3450.0) / 3450.0 * 100,
    )
    assert asset.premium_pct < 0


def test_asset_info_premium_pct_zero():
    asset = AssetInfo(
        symbol="SOL",
        price=178.0,
        funding_rate=0.0,
        open_interest=100_000_000.0,
        volume_24h=500_000_000.0,
        price_change_24h_pct=0.0,
        mark_price=178.0,
        index_price=178.0,
        premium_pct=0.0,
    )
    assert asset.premium_pct == pytest.approx(0.0)


def test_timeframe_to_ms():
    assert _timeframe_to_ms("1m") == 60_000
    assert _timeframe_to_ms("15m") == 900_000
    assert _timeframe_to_ms("1h") == 3_600_000
    assert _timeframe_to_ms("4h") == 14_400_000
    assert _timeframe_to_ms("1d") == 86_400_000
