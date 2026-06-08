"""Tests for the data-integrity / trustworthiness features.

Covers the staleness watchdog (W1), CVD trade dedup + per-venue split (W4),
and the health monitor's verdict classification (W3).
"""
from __future__ import annotations

import time

import pytest

from data_layer.health_monitor import DataHealthMonitor, HealthCheck
from data_layer.orderbook import STALE_AFTER_SECONDS as OB_STALE
from data_layer.orderbook import OrderBookEngine
from data_layer.orderflow_engine import STALE_AFTER_SECONDS, OrderFlowEngine

# ── Staleness watchdog (W1) ──────────────────────────────────────

def test_orderflow_stale_until_data():
    e = OrderFlowEngine(["BTC"])
    assert e.is_stale() is True            # nothing received yet
    assert e.data_age() == float("inf")

    e.last_hl_message_at = time.time()
    assert e.is_stale() is False

    e.last_hl_message_at = time.time() - (STALE_AFTER_SECONDS + 1)
    assert e.is_stale() is True


def test_orderflow_combined_age_uses_freshest_venue():
    e = OrderFlowEngine(["BTC"])
    e.last_hl_message_at = time.time() - 1000      # HL stale
    e.last_binance_message_at = time.time()        # Binance fresh
    # Combined age should follow the freshest venue, so it is NOT stale.
    assert e.is_stale() is False


def test_orderbook_snapshot_stale_flag():
    e = OrderBookEngine(["BTC"])
    e._update_book("BTC", {"levels": [[{"px": "100", "sz": "1"}], [{"px": "101", "sz": "1"}]]})
    snap = e.get_snapshot("BTC")
    assert snap is not None and snap.stale is False

    # Age the feed past the threshold; the flag must flip on read.
    e.last_message_at = time.time() - (OB_STALE + 1)
    snap = e.get_snapshot("BTC")
    assert snap is not None and snap.stale is True


# ── CVD dedup + per-venue separation (W4) ───────────────────────

def test_cvd_dedup_and_per_venue():
    e = OrderFlowEngine(["BTC"])
    hl = {"channel": "trades", "data": [
        {"coin": "BTC", "px": "80000", "sz": "0.1", "side": "B", "time": 1700000000000, "tid": 1},
    ]}
    e._handle_message(hl)
    e._handle_message(hl)  # exact replay — must be ignored

    bn = {"data": {"s": "BTCUSDT", "p": "80000", "q": "0.1", "m": False, "T": 1700000000000, "a": 1}}
    e._handle_binance_trade(bn)
    e._handle_binance_trade(bn)  # exact replay — must be ignored

    cvd = e.get_cumulative_cvd("BTC")
    assert cvd["hyperliquid"] == pytest.approx(8000.0)
    assert cvd["binance"] == pytest.approx(8000.0)
    assert cvd["combined"] == pytest.approx(16000.0)


# ── Health monitor verdict classification (W3) ──────────────────

def _checks(*specs):
    return [HealthCheck(cat, name, status, "") for (cat, name, status) in specs]


def test_health_stale_outranks_drift():
    m = DataHealthMonitor(hub=None)
    res = m._summarize(_checks(
        ("freshness", "order_flow", "fail"),
        ("xref", "btc_price", "fail"),
    ))
    # A frozen feed is the most urgent signal — it must win over xref drift.
    assert res["overall"] == "stale"


def test_health_drift_when_xref_fails():
    m = DataHealthMonitor(hub=None)
    res = m._summarize(_checks(
        ("xref", "btc_price", "fail"),
        ("freshness", "order_flow", "pass"),
    ))
    assert res["overall"] == "drift"


def test_health_ok_and_warn():
    m = DataHealthMonitor(hub=None)
    assert m._summarize(_checks(("freshness", "order_flow", "pass")))["overall"] == "ok"
    assert m._summarize(_checks(("completeness", "assets", "warn")))["overall"] == "warn"
