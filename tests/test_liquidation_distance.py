"""Distance-based position views (H2): only real liquidation prices the
price has not crossed count as "near liquidation". A crossed position (the
price is already past its liquidation price) and a position Hyperliquid
reports no liquidation price for must never read as a cluster."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from hyperdata_terminal.data_layer.position_scanner import PositionScanner, TrackedPosition


def _pos(side: str, price: float, liq: float | None, size: float = 1_000_000.0, symbol: str = "BTC",
         address: str = "0x" + "a" * 40) -> TrackedPosition:
    p = TrackedPosition(
        address=address, symbol=symbol, side=side, size_usd=size, entry_price=price,
        current_price=price, liq_price=liq, distance_pct=0.0, leverage=10.0,
        unrealized_pnl=0.0, margin_used=size / 10, scanned_at=time.time(),
    )
    scanner = PositionScanner.__new__(PositionScanner)
    scanner.market_prices = {symbol: price}
    scanner._position_cache = {address: [p]}
    (p,) = scanner._assemble_positions()
    return p


@pytest.fixture
def positions():
    return {
        # Long whose mark (80,000) is already below its liquidation price.
        "crossed_long": _pos("long", 80_000.0, 80_300.0, address="0x" + "1" * 40),
        # Short whose mark is already above its liquidation price.
        "crossed_short": _pos("short", 80_000.0, 79_800.0, address="0x" + "2" * 40),
        # Genuinely 0.5% from liquidation.
        "near_long": _pos("long", 80_000.0, 79_600.0, address="0x" + "3" * 40),
        # Hyperliquid reports no liquidation price.
        "no_liq": _pos("long", 80_000.0, None, address="0x" + "4" * 40),
    }


def test_distance_is_signed(positions):
    assert positions["crossed_long"].distance_pct == pytest.approx(-0.375)
    assert positions["crossed_short"].distance_pct == pytest.approx(-0.25)
    assert positions["near_long"].distance_pct == pytest.approx(0.5)
    assert positions["no_liq"].distance_pct == float("inf")
    assert positions["crossed_long"].crossed and not positions["near_long"].crossed


def test_scanner_views_exclude_crossed_and_missing(positions):
    scanner = PositionScanner.__new__(PositionScanner)
    scanner.positions = list(positions.values())
    assert scanner.get_danger_zone(threshold_pct=2.0) == [positions["near_long"]]
    assert scanner.get_zone_summary()["within_1pct"]["count"] == 1
    assert scanner.get_closest_longs(5) == [positions["near_long"]]
    assert scanner.get_closest_shorts(5) == []
    assert set(map(id, scanner.get_crossed())) == {id(positions["crossed_long"]), id(positions["crossed_short"])}


def test_zone_breakdown_counts_only_near(positions):
    from hyperdata_terminal.dashboards.liquidation_watch import compute_zone_breakdown

    zones = compute_zone_breakdown(list(positions.values()))
    assert [z["long_count"] + z["short_count"] for z in zones] == [1, 1, 1]


def test_heatmap_skips_crossed_and_missing_liquidation_prices(positions):
    from hyperdata_terminal.dashboards.liquidation_heatmap import compute_heatmap_buckets

    rows = compute_heatmap_buckets(list(positions.values()), 80_000.0, symbol="BTC", n_buckets=20, range_pct=5.0)
    assert sum(b.long_usd for b in rows) == 1_000_000.0  # near_long only
    assert sum(b.short_usd for b in rows) == 0.0


def test_mcp_near_liquidation_lists_near_and_counts_crossed(positions):
    from hyperdata_terminal.mcp_server import HubTools

    everything = sorted(positions.values(), key=lambda p: p.distance_pct)
    hub = SimpleNamespace(
        get_all_positions_sorted=lambda: everything,
        status=SimpleNamespace(started_at=time.time() - 60),
        market=SimpleNamespace(assets={"BTC": object()}),
        positions=SimpleNamespace(positions=everything, oldest_position_age_seconds=lambda: 0.0,
                                  as_of=lambda ps: None, is_stale=lambda: False),
        hlp=SimpleNamespace(get_latest_snapshot=lambda: object()),
    )
    out = HubTools(hub).near_liquidation(max_distance_pct=2.0)
    assert out["count"] == 1
    assert out["positions"][0]["distance_to_liquidation_pct"] == pytest.approx(0.5)
    assert out["crossed_liquidation_price"]["count"] == 2


def test_mcp_position_reports_null_liquidation_price(positions):
    from hyperdata_terminal.mcp_server import HubTools

    row = HubTools._position(positions["no_liq"])
    assert row["liquidation_price"] is None
    assert row["distance_to_liquidation_pct"] is None


def test_formatter_labels_crossed_and_missing(positions):
    from hyperdata_terminal.utils.helpers import format_distance_pct

    assert format_distance_pct(positions["crossed_long"].distance_pct) == "crossed"
    assert format_distance_pct(positions["no_liq"].distance_pct) == "none"
