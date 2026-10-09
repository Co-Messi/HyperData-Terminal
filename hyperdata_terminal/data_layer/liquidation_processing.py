"""Liquidation event processing: dedup, cascade detection, symbol cleanup,
leverage estimation.

Extracted verbatim from HyperDataAPI (M13) — the REST/WebSocket server was
carrying data-layer business logic. HyperDataAPI keeps thin delegating
methods (`_is_duplicate_liq`, `_check_cascade`, `_clean_symbol`) and
attribute views so existing callers and tests are unaffected.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class LiquidationProcessor:
    """Per-exchange dedup + cascade bypass for the liquidation broadcast path."""

    # Symbol cleanup: remove numeric prefixes, map weird names
    SYM_MAP = {
        "PLAY": "PLAYAI", "1000CHEE": "CHEE", "1000PEPE": "PEPE",
        "1000SHIB": "SHIB", "1000FLOKI": "FLOKI", "1000BONK": "BONK",
        "1000LUNC": "LUNC", "1000X": "X", "1000CAT": "CAT",
        "1000SATS": "SATS", "1000RATS": "RATS",
        "龙虾": "LOBSTER", "BSB": "BSB", "PTB": "PTB",
        "ON": "ON", "NOM": "NOM",
    }
    MIN_LIQ_SIZE_USD = 500
    DEDUP_WINDOW = 3
    DEDUP_MAX = 500
    CASCADE_WINDOW = 30
    CASCADE_BYPASS_DURATION = 30
    CASCADE_TRACKER_MAX = 200  # entries kept per symbol/side/exchange key

    # Plausible epoch-seconds range for exchange event times (2001..5138).
    # A timestamp outside this range means a connector skipped ms→s
    # normalization (or sent 0) — such events cannot be safely hashed.
    TS_SANE_MIN = 1e9
    TS_SANE_MAX = 1e11

    def __init__(self) -> None:
        self.liq_seen: dict[str, float] = {}
        self.liq_count = {"hyperliquid": 0, "binance": 0, "okx": 0, "bybit": 0}
        self.cascade_tracker: dict[str, list] = {}
        self.cascade_bypass: dict[str, float] = {}
        self.cascade_bypass_started: dict[str, float] = {}
        self.liq_stats = {"received": 0, "broadcast": 0, "deduped": 0, "filtered": 0}
        self.liq_stats_ts = time.time()

    def is_duplicate(self, ev) -> bool:
        """Duplicate check within the dedup window, keyed per exchange.

        Buckets on the EXCHANGE event timestamp (not local receive time) so
        two records of the same event dedup identically regardless of local
        delivery jitter. Events without a plausible exchange timestamp are
        never deduped — substituting the local clock would collide distinct
        events that merely arrived together. The hash uses the exact size:
        replayed duplicates carry identical payloads, while distinct events
        of similar size must not collapse into one. The cascade bypass is
        also per-exchange: a Binance cascade must not let Hyperliquid's
        heuristic events skip dedup.
        """
        now = time.time()

        ev_ts = ev.timestamp
        if not (self.TS_SANE_MIN < ev_ts < self.TS_SANE_MAX):
            logger.warning(
                "[liq] %s event has implausible timestamp %r — skipping dedup",
                ev.exchange, ev_ts,
            )
            return False

        bypass_key = f"{ev.symbol}_{ev.side}_{ev.exchange}"
        if bypass_key in self.cascade_bypass and now < self.cascade_bypass[bypass_key]:
            return False

        h = f"{ev.symbol}_{ev.side}_{ev.size_usd:.2f}_{ev.exchange}_{int(ev_ts // self.DEDUP_WINDOW)}"

        if len(self.liq_seen) > self.DEDUP_MAX:
            cutoff = now - self.DEDUP_WINDOW * 2
            self.liq_seen = {k: v for k, v in self.liq_seen.items() if v > cutoff}

        if h in self.liq_seen:
            return True
        self.liq_seen[h] = now
        return False

    def check_cascade(self, ev) -> str | None:
        """Track rapid successive liquidations. Returns cascade label if detected.

        Keys on the RAW event fields (symbol/side/exchange) — the same domain
        is_duplicate reads its bypass with — so a detected cascade actually
        lifts dedup for the venue that is cascading.
        """
        now = time.time()
        key = f"{ev.symbol}_{ev.side}_{ev.exchange}"

        if key not in self.cascade_tracker:
            self.cascade_tracker[key] = []

        self.cascade_tracker[key] = [
            (ts, sz) for ts, sz in self.cascade_tracker[key]
            if now - ts < self.CASCADE_WINDOW
        ]

        self.cascade_tracker[key].append((now, ev.size_usd))
        # Bound per-key memory: only the most recent window entries matter.
        if len(self.cascade_tracker[key]) > self.CASCADE_TRACKER_MAX:
            self.cascade_tracker[key] = self.cascade_tracker[key][-self.CASCADE_TRACKER_MAX:]

        entries = self.cascade_tracker[key]
        if len(entries) >= 3:
            # Bypass dedup only for this exchange's stream: cascades on one
            # venue say nothing about duplicates on another. The bypass has
            # an ABSOLUTE cap: without it, events passing dedup during the
            # bypass re-trigger cascade detection and extend it forever
            # (replayed duplicates would keep the floodgate open).
            first = self.cascade_bypass_started.setdefault(key, now)
            cap = first + 2 * self.CASCADE_BYPASS_DURATION
            self.cascade_bypass[key] = min(now + self.CASCADE_BYPASS_DURATION, cap)
            total = sum(sz for _, sz in entries)
            return f"cascade ${total:,.0f} ({len(entries)}x in {self.CASCADE_WINDOW}s)"

        # Quiet again: allow a future cascade to start a fresh bypass window.
        if key in self.cascade_bypass_started and now > self.cascade_bypass.get(key, 0):
            del self.cascade_bypass_started[key]

        return None

    def log_stats(self) -> None:
        """Log 60-second liquidation throughput stats."""
        now = time.time()
        if now - self.liq_stats_ts >= 60:
            s = self.liq_stats
            total = s["received"]
            if total > 0:
                drop_pct = s["deduped"] / total * 100
                logger.info(
                    "[LIQ] 60s: received=%d broadcast=%d deduped=%d filtered=%d (%.0f%% drop)",
                    s["received"], s["broadcast"], s["deduped"], s["filtered"], drop_pct,
                )
            self.liq_stats = {"received": 0, "broadcast": 0, "deduped": 0, "filtered": 0}
            self.liq_stats_ts = now

    def clean_symbol(self, sym: str) -> str:
        sym = sym.upper()
        if sym in self.SYM_MAP:
            return self.SYM_MAP[sym]
        if sym.startswith("1000") and len(sym) > 4:
            return sym[4:]
        return sym

    @staticmethod
    def estimate_leverage(ev) -> int | None:
        """Rough leverage from notional vs. liquidated size; None if implausible."""
        if ev.price > 0 and ev.quantity > 0:
            notional = ev.price * ev.quantity
            if notional > 0 and ev.size_usd > 0:
                est_lev = round(notional / max(ev.size_usd, 1))
                if 2 <= est_lev <= 200:
                    return est_lev
        return None
