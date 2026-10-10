"""Tests for multi-exchange funding rate collector."""
from __future__ import annotations

import time

import pytest

from hyperdata_terminal.data_layer.funding_rates import FundingRateCollector, FundingRateSnapshot, normalise_fr_symbol
from tests.fixture_data import load_fixture


def test_normalise_fr_symbol():
    assert normalise_fr_symbol("BTCUSDT") == "BTC"
    assert normalise_fr_symbol("ETHUSDT") == "ETH"
    assert normalise_fr_symbol("SOLUSDT") == "SOL"
    assert normalise_fr_symbol("BTC") == "BTC"
    assert normalise_fr_symbol("BTCPERP") == "BTC"


def test_funding_rate_snapshot_fields():
    snap = FundingRateSnapshot(
        timestamp=time.time(),
        exchange="binance",
        symbol="BTC",
        funding_rate_hourly=0.0001,
        funding_rate_annualized=0.0001 * 8760,
    )
    assert snap.exchange == "binance"
    assert snap.symbol == "BTC"
    assert abs(snap.funding_rate_annualized - 0.876) < 0.001


def test_binance_uses_each_symbols_funding_interval():
    """Binance funds some symbols every 4h (16 of the 50 tracked ones were
    off the 8h grid in a live capture); dividing every rate by 8 halved
    their hourly and annualized figures. Intervals come from fundingInfo,
    which lists only adjusted symbols; the rest use the standard 8h."""
    collector = FundingRateCollector()
    collector._apply_binance_intervals(load_fixture("binance/fundingInfo_doc_shape.json"))
    premium = load_fixture("binance/premiumIndex_doc_example.json")
    wif = dict(premium[0], symbol="WIFUSDT")  # same documented row, a 4h symbol
    collector._parse_binance([*premium, wif])

    btc = collector.rates["binance"]["BTC"]
    assert btc.interval_hours == 8
    assert btc.rate_per_interval == pytest.approx(0.00038246)
    assert btc.funding_rate_hourly == pytest.approx(0.00038246 / 8)
    assert btc.funding_rate_annualized == pytest.approx(0.00038246 / 8 * 8760)
    wif_snap = collector.rates["binance"]["WIF"]
    assert wif_snap.interval_hours == 4
    assert wif_snap.funding_rate_hourly == pytest.approx(0.00038246 / 4)
    assert wif_snap.funding_rate_annualized == pytest.approx(0.00038246 / 4 * 8760)


def test_binance_rates_wait_for_the_interval_list():
    """Without fundingInfo the interval of a symbol is unknown: publish
    nothing rather than a number that may be half the truth."""
    collector = FundingRateCollector()
    collector._parse_binance(load_fixture("binance/premiumIndex_doc_example.json"))
    assert collector.rates["binance"] == {}
    assert collector.binance_unknown_interval_rows == 1


def test_binance_keeps_last_known_intervals_when_a_refresh_fails():
    collector = FundingRateCollector()
    collector._apply_binance_intervals(load_fixture("binance/fundingInfo_doc_shape.json"))
    collector._apply_binance_intervals(None)  # a failed refresh
    assert collector.binance_intervals["WIFUSDT"] == 4


def test_bybit_uses_the_tickers_funding_interval():
    """Captured Bybit tickers: WIF, 1000BONK and TIA fund every 4h."""
    collector = FundingRateCollector()
    tickers = load_fixture("bybit/tickers_linear.json")
    collector._parse_bybit(tickers)
    rows = {r["symbol"]: r for r in tickers["result"]["list"]}
    for raw, sym in (("BTCUSDT", "BTC"), ("WIFUSDT", "WIF"), ("TIAUSDT", "TIA")):
        snap = collector.rates["bybit"][sym]
        interval = float(rows[raw]["fundingIntervalHour"])
        assert snap.interval_hours == interval
        assert snap.funding_rate_hourly == pytest.approx(float(rows[raw]["fundingRate"]) / interval)
    assert collector.rates["bybit"]["WIF"].interval_hours == 4
    assert collector.rates["bybit"]["BTC"].interval_hours == 8


def test_bybit_falls_back_to_instruments_info_minutes():
    collector = FundingRateCollector()
    collector._apply_bybit_intervals(load_fixture("bybit/instruments_info_doc_example.json"))
    tickers = load_fixture("bybit/tickers_linear.json")
    for row in tickers["result"]["list"]:
        row.pop("fundingIntervalHour")
    collector._parse_bybit(tickers)
    # BTCUSDT: fundingInterval 480 minutes in instruments-info.
    assert collector.rates["bybit"]["BTC"].interval_hours == 8
    # No interval anywhere for the others: skipped, not assumed.
    assert "WIF" not in collector.rates["bybit"]
    assert collector.bybit_unknown_interval_rows == 4


def test_collector_get_all_for_symbol():
    collector = FundingRateCollector()
    now = time.time()
    collector.rates["binance"]["BTC"] = FundingRateSnapshot(now, "binance", "BTC", 0.0001, 0.0001 * 8760)
    collector.rates["bybit"]["BTC"] = FundingRateSnapshot(now, "bybit", "BTC", 0.00015, 0.00015 * 8760)
    result = collector.get_all_for_symbol("BTC")
    assert len(result) == 2
    exchanges = {s.exchange for s in result}
    assert "binance" in exchanges
    assert "bybit" in exchanges


def test_collector_get_latest():
    collector = FundingRateCollector()
    now = time.time()
    collector.rates["binance"]["ETH"] = FundingRateSnapshot(now, "binance", "ETH", -0.00005, -0.00005 * 8760)
    snap = collector.get_latest("binance", "ETH")
    assert snap is not None
    assert snap.symbol == "ETH"
    assert collector.get_latest("okx", "ETH") is None


def _collector_with_captured_bybit() -> FundingRateCollector:
    collector = FundingRateCollector()
    collector._parse_bybit(load_fixture("bybit/tickers_linear.json"))
    return collector


@pytest.mark.asyncio
async def test_api_reports_the_funding_interval():
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from hyperdata_terminal.api_server import HyperDataAPI

    hub = SimpleNamespace(funding=_collector_with_captured_bybit(), market=SimpleNamespace(assets={}))
    api = HyperDataAPI(hub=hub)
    app = web.Application()
    app.router.add_get("/v1/funding-rates/{symbol}", api.handle_funding_symbol)
    app.router.add_get("/v1/funding-rates", api.handle_funding_rates)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        wif = (await (await client.get("/v1/funding-rates/WIF")).json())["rates"]["bybit"]
        assert wif["interval_hours"] == 4
        assert wif["hourly"] == pytest.approx(wif["rate_per_interval"] / 4)
        table = await (await client.get("/v1/funding-rates")).json()
        assert table["WIF"]["bybit_interval_hours"] == 4
        assert table["BTC"]["bybit_interval_hours"] == 8
    finally:
        await client.close()


def test_mcp_reports_the_funding_interval():
    from types import SimpleNamespace

    from hyperdata_terminal.mcp_server import HubTools

    asset = SimpleNamespace(symbol="WIF", price=1.0, funding_rate=0.0001, open_interest=1.0, volume_24h=1.0,
                            price_change_24h_pct=0.0)
    hub = SimpleNamespace(
        funding=_collector_with_captured_bybit(),
        market=SimpleNamespace(assets={"WIF": asset}),
        lsr=SimpleNamespace(get_latest=lambda s: None),
        spot=SimpleNamespace(get_latest=lambda s: None),
        orderflow=SimpleNamespace(buckets={}),
        positions=SimpleNamespace(positions=[]),
        status=SimpleNamespace(started_at=time.time()),
        hlp=SimpleNamespace(get_latest_snapshot=lambda: None),
    )
    out = HubTools(hub).asset("WIF")
    assert out["funding"]["bybit_interval_hours"] == 4
    assert out["funding"]["hyperliquid_interval_hours"] == 1


def test_funding_interval_is_persisted(tmp_path):
    from hyperdata_terminal.data_layer.persistence import DataStore

    store = DataStore(tmp_path / "f.db")
    try:
        store.save_funding_rate(_collector_with_captured_bybit().rates["bybit"]["WIF"])
        (row,) = store.get_funding_rates(exchange="bybit", symbol="WIF", hours=24 * 365 * 10)
        assert row["interval_hours"] == 4
    finally:
        store.close()
