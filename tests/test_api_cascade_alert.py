"""The API's WebSocket cascade alert (M3): incremental totals, one alert per
cooldown, labelled market wide, confirmed liquidations only."""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

from hyperdata_terminal.api_server import HyperDataAPI
from hyperdata_terminal.data_layer.liquidation_feed import LiquidationEvent


def _api_with_listener():
    hub = MagicMock()
    api = HyperDataAPI(hub=hub)
    sent: list[dict] = []
    api._broadcast = lambda event_type, data: sent.append({**data, "event": event_type})
    return api, hub, sent


def _ev(symbol: str, size: float, confirmed: bool = True, side: str = "long") -> LiquidationEvent:
    return LiquidationEvent(time.time(), "okx", symbol, side, size, 100.0, size / 100.0, confirmed)


def test_one_market_wide_alert_per_cooldown_without_rescanning():
    api, hub, sent = _api_with_listener()
    for i in range(200):  # a cascade: $200M across several symbols (distinct sizes, so none dedupe)
        api._on_liquidation(_ev(["BTC", "ETH", "DOGE"][i % 3], 1_000_000.0 + i))
    alerts = [m for m in sent if m["event"] == "alert"]
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["scope"] == "market" and alert["asset"] == "ALL"
    assert alert["volume_usd"] > HyperDataAPI.CASCADE_ALERT_USD
    assert {s["symbol"] for s in alert["top_symbols"]} == {"BTC", "ETH", "DOGE"}
    hub.liquidations.get_stats.assert_not_called()  # no O(buffer) pass per event
    json.dumps(alert)


def test_alert_again_after_the_cooldown(monkeypatch):
    api, _, sent = _api_with_listener()
    for i in range(6):
        api._on_liquidation(_ev("BTC", 1_000_000.0 + i))
    api._cascade_alert_at -= HyperDataAPI.CASCADE_ALERT_COOLDOWN_S + 1
    api._on_liquidation(_ev("BTC", 2_000_000.0))
    assert len([m for m in sent if m["event"] == "alert"]) == 2


def test_estimated_prints_never_alert():
    api, _, sent = _api_with_listener()
    for i in range(50):
        api._on_liquidation(_ev("BTC", 1_000_000.0 + i, confirmed=False))
    assert [m for m in sent if m["event"] == "alert"] == []
