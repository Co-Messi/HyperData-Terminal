"""
Data health monitor — continuous self-verification against external sources.

Cross-references the hub's live data against public exchange APIs (Binance spot,
premium index, long/short ratio) and checks per-feed freshness/staleness so the
terminal can *prove* its numbers rather than just display them.

Two consumers:
  - the hub runs ``run_checks()`` on an interval and caches the result, which the
    REST API (``/v1/health``) and the dashboard health badge read via ``latest()``
  - ``src/verify_data.py`` runs it once as a CLI for a manual integrity report

A check is one of:
  - "pass": value present and within tolerance
  - "warn": missing/degraded but not necessarily broken (e.g. sparse coverage)
  - "fail": actively wrong — a stale feed or a cross-reference outside tolerance
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

# External reference endpoints (public, no key).
BINANCE_SPOT_PRICE = "https://api.binance.com/api/v3/ticker/price"
BINANCE_PREMIUM_INDEX = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_LSR = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"

# Tolerances for cross-reference checks.
PRICE_TOLERANCE_PCT = 0.5    # hub BTC price vs Binance spot
LSR_TOLERANCE_PCT = 20.0     # different venues, generous tolerance

# Freshness thresholds (seconds). Order flow / orderbook delegate to the engines'
# own is_stale() (set in the data layer) so the threshold lives in one place.
MARKET_FRESH_SECONDS = 30.0
DERIBIT_FRESH_SECONDS = 180.0


@dataclass
class HealthCheck:
    category: str   # 'xref' | 'freshness' | 'completeness' | 'consistency'
    name: str
    status: str     # 'pass' | 'warn' | 'fail'
    detail: str

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
        }


def _pct_diff(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return abs(a - b) / b * 100


async def _fetch_json(session: aiohttp.ClientSession, url: str, params: dict | None = None):
    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception:
        return None
    return None


class DataHealthMonitor:
    """Runs integrity checks against the hub and caches the latest result."""

    def __init__(self, hub) -> None:
        self.hub = hub
        self._result: dict | None = None

    def latest(self) -> dict | None:
        """Most recent cached result (None until run_checks() has run once)."""
        return self._result

    async def run_checks(self) -> dict:
        """Run every check group, summarize, cache, and return the result."""
        checks: list[HealthCheck] = []

        # Cross-reference needs network; never let it crash the whole run.
        try:
            async with aiohttp.ClientSession() as session:
                checks.extend(await self._check_cross_references(session))
        except Exception:
            logger.exception("health monitor: cross-reference checks errored")

        for fn in (self._check_freshness, self._check_completeness, self._check_consistency):
            try:
                checks.extend(fn())
            except Exception:
                logger.exception("health monitor: %s errored", fn.__name__)

        self._result = self._summarize(checks)
        return self._result

    # ── Check groups ──────────────────────────────────────────────

    async def _check_cross_references(self, session: aiohttp.ClientSession) -> list[HealthCheck]:
        out: list[HealthCheck] = []
        hub = self.hub

        # BTC price: hub vs Binance spot.
        hub_btc = hub.market.assets.get("BTC")
        hub_price = hub_btc.price if hub_btc else 0.0
        ext = await _fetch_json(session, BINANCE_SPOT_PRICE, {"symbol": "BTCUSDT"})
        ext_price = float(ext["price"]) if ext else 0.0
        if hub_price > 0 and ext_price > 0:
            diff = _pct_diff(hub_price, ext_price)
            status = "pass" if diff < PRICE_TOLERANCE_PCT else "fail"
            out.append(HealthCheck(
                "xref", "btc_price", status,
                f"hub=${hub_price:,.2f} binance=${ext_price:,.2f} diff={diff:.3f}%",
            ))
        else:
            out.append(HealthCheck("xref", "btc_price", "warn", "price unavailable"))

        # BTC long/short ratio: hub vs Binance.
        hub_lsr_snap = hub.lsr.get_latest("BTC")
        hub_lsr = hub_lsr_snap.long_short_ratio if hub_lsr_snap else 0.0
        ext_lsr_data = await _fetch_json(
            session, BINANCE_LSR, {"symbol": "BTCUSDT", "period": "5m", "limit": "1"}
        )
        ext_lsr = (
            float(ext_lsr_data[0]["longShortRatio"])
            if ext_lsr_data and len(ext_lsr_data) > 0 else 0.0
        )
        if hub_lsr > 0 and ext_lsr > 0:
            diff = _pct_diff(hub_lsr, ext_lsr)
            status = "pass" if diff < LSR_TOLERANCE_PCT else "warn"
            out.append(HealthCheck(
                "xref", "btc_long_short_ratio", status,
                f"hub={hub_lsr:.2f} binance={ext_lsr:.2f} diff={diff:.1f}%",
            ))
        else:
            out.append(HealthCheck("xref", "btc_long_short_ratio", "warn", "ratio unavailable"))

        # Deribit DVOL present.
        snap = hub.deribit.get_latest("BTC")
        iv = snap.mark_iv if snap else 0.0
        out.append(HealthCheck(
            "xref", "deribit_dvol", "pass" if iv > 0 else "warn",
            f"BTC DVOL={iv:.1f}%",
        ))
        return out

    def _check_freshness(self) -> list[HealthCheck]:
        out: list[HealthCheck] = []
        hub = self.hub
        now = time.time()

        # Order flow / orderbook delegate to the engines' staleness (W1).
        of_stale = hub.orderflow.is_stale()
        out.append(HealthCheck(
            "freshness", "order_flow",
            "fail" if of_stale else "pass",
            f"{hub.orderflow.data_age():.0f}s since last trade",
        ))
        ob_stale = hub.orderbook.is_stale()
        out.append(HealthCheck(
            "freshness", "orderbook",
            "fail" if ob_stale else "pass",
            f"{hub.orderbook.data_age():.0f}s since last book",
        ))

        # Market data freshness via the hub's wall-clock refresh stamp.
        mkt_age = (now - hub.status.last_market_refresh) if hub.status.last_market_refresh > 0 else float("inf")
        out.append(HealthCheck(
            "freshness", "market_data",
            "pass" if mkt_age < MARKET_FRESH_SECONDS else "warn",
            f"{mkt_age:.0f}s since refresh" if mkt_age != float("inf") else "no refresh yet",
        ))

        # Deribit IV freshness.
        snap = hub.deribit.get_latest("BTC")
        ts = snap.timestamp if snap else 0.0
        iv_age = (now - ts) if ts > 0 else float("inf")
        out.append(HealthCheck(
            "freshness", "deribit_iv",
            "pass" if iv_age < DERIBIT_FRESH_SECONDS else "warn",
            f"{iv_age:.0f}s since update" if iv_age != float("inf") else "no data yet",
        ))
        return out

    def _check_completeness(self) -> list[HealthCheck]:
        # Completeness issues warn but never fail the badge — they reflect
        # coverage breadth, not correctness.
        out: list[HealthCheck] = []
        hub = self.hub

        total = len(hub.market.assets)
        with_price = sum(1 for a in hub.market.assets.values() if a.price > 0)
        out.append(HealthCheck(
            "completeness", "assets_priced",
            "pass" if with_price >= 50 else "warn",
            f"{with_price}/{total} assets priced",
        ))

        fr = hub.funding.rates
        fr_count = len(fr.get("binance", {})) + len(fr.get("bybit", {}))
        out.append(HealthCheck(
            "completeness", "funding_symbols",
            "pass" if fr_count > 0 else "warn",
            f"{fr_count} funding symbols",
        ))
        return out

    def _check_consistency(self) -> list[HealthCheck]:
        out: list[HealthCheck] = []
        hub = self.hub

        # Funding sign vs long/short dominance should usually agree.
        btc = hub.market.assets.get("BTC")
        lsr_snap = hub.lsr.get_latest("BTC")
        if btc and lsr_snap and lsr_snap.long_short_ratio > 0:
            fr_positive = btc.funding_rate > 0
            lsr_long_dom = lsr_snap.long_short_ratio > 1.0
            consistent = fr_positive == lsr_long_dom
            out.append(HealthCheck(
                "consistency", "funding_vs_lsr",
                "pass" if consistent else "warn",
                f"funding {'+' if fr_positive else '-'}, "
                f"{'longs' if lsr_long_dom else 'shorts'} dominant",
            ))
        return out

    # ── Summary ───────────────────────────────────────────────────

    def _summarize(self, checks: list[HealthCheck]) -> dict:
        counts = {"pass": 0, "warn": 0, "fail": 0}
        for c in checks:
            counts[c.status] = counts.get(c.status, 0) + 1

        # Badge semantics: a stale feed is the most urgent signal (frozen data),
        # then a cross-reference drift (wrong data), then generic warnings.
        feed_stale = any(c.category == "freshness" and c.status == "fail" for c in checks)
        xref_drift = any(c.category == "xref" and c.status == "fail" for c in checks)
        if feed_stale:
            overall = "stale"
        elif xref_drift:
            overall = "drift"
        elif counts["fail"]:
            overall = "fail"
        elif counts["warn"]:
            overall = "warn"
        else:
            overall = "ok"

        return {
            "overall": overall,
            "counts": counts,
            "checks": [c.as_dict() for c in checks],
            "updated_at": time.time(),
        }
