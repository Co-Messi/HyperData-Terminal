"""One Hyperliquid request budget for the whole process, shared between
processes on the same machine.

Hyperliquid limits each IP address to 1200 request weight per minute
(https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits):

- ``l2Book``, ``allMids``, ``clearinghouseState``, ``orderStatus``,
  ``spotClearinghouseState`` and ``exchangeStatus`` weigh 2, ``userRole``
  60, every other info request 20;
- ``recentTrades``, ``userFills``, ``userFillsByTime`` (and a few more) add
  1 per 20 items returned, ``candleSnapshot`` 1 per 60.

Every Hyperliquid info request in this package goes through ``hl_info()``,
which waits for room in a sliding 60 second window (charging the known
weight before the request and the per-item weight after the response),
and pauses every caller on HTTP 429 for the server's ``Retry-After`` (or an
exponential backoff when it sends none).

The budget (default 1000 a minute, leaving headroom for anything else on the
same IP, such as a trading front end) is split between every hyperdata
process on the machine: each live process registers in a per-user directory
(``HYPERDATA_HL_REGISTRY_DIR`` overrides it; it does not depend on the data
dir, because two processes with different data dirs still share one IP) and
takes ``budget / processes``, less whatever the others used in the last
minute while a newcomer ramps up. A 429 one process sees pauses the others
too.

Within a process, "elastic" callers (the position scanner and the smart
money engine, which can always do less) share what is left after a reserve
for the fixed cadence callers (market data, HLP vault, symbol lists), so
those always have room.
"""
from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import math
import os
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
IP_LIMIT_PER_MIN = 1200
DEFAULT_BUDGET_PER_MIN = 1000
WINDOW_SECONDS = 60.0
# Never let one process burst more than this share of its per-minute
# allowance inside 10 seconds: Hyperliquid documents a per-minute limit, but
# a minute's worth in one second is the burst that draws 429s.
BURST_WINDOW_SECONDS = 10.0
BURST_SHARE = 0.4

LIGHT_TYPES = frozenset({
    "l2Book", "allMids", "clearinghouseState", "orderStatus", "spotClearinghouseState", "exchangeStatus",
})
HEAVY_TYPES = {"userRole": 60}
PER_20_ITEM_TYPES = frozenset({
    "recentTrades", "historicalOrders", "userFills", "userFillsByTime", "fundingHistory", "userFunding",
    "nonUserFundingUpdates", "twapHistory", "userTwapSliceFills", "userTwapSliceFillsByTime",
    "delegatorHistory", "delegatorRewards", "validatorStats",
})

# Elastic callers and their share of the pool left after the reserve.
ELASTIC_WEIGHTS = {"position_scanner": 0.6, "smart_money": 0.4}
# The scanner needs no more than this to keep its documented pace.
ELASTIC_MAX_PER_MIN = {"position_scanner": 450.0}
# Kept free for fixed cadence callers (market data ~40, HLP ~100 a minute).
ESSENTIAL_RESERVE_PER_MIN = 250.0

HEARTBEAT_SECONDS = 5.0
STALE_INSTANCE_SECONDS = 20.0
MAX_BACKOFF_SECONDS = 60.0


def request_weight(payload: dict) -> int:
    """The weight Hyperliquid charges up front for an info request."""
    typ = str(payload.get("type", ""))
    if typ in LIGHT_TYPES:
        return 2
    return HEAVY_TYPES.get(typ, 20)


def response_extra_weight(payload: dict, data: Any) -> int:
    """The per-item weight Hyperliquid adds after answering."""
    typ = str(payload.get("type", ""))
    if not isinstance(data, list):
        return 0
    if typ in PER_20_ITEM_TYPES:
        return len(data) // 20
    if typ == "candleSnapshot":
        return len(data) // 60
    return 0


def _budget_from_env() -> float:
    raw = os.environ.get("HYPERDATA_HL_WEIGHT_PER_MIN", "").strip()
    if not raw:
        return float(DEFAULT_BUDGET_PER_MIN)
    try:
        value = float(raw)
    except ValueError:
        logger.warning("HYPERDATA_HL_WEIGHT_PER_MIN=%r is not a number; using %d", raw, DEFAULT_BUDGET_PER_MIN)
        return float(DEFAULT_BUDGET_PER_MIN)
    if not math.isfinite(value) or value <= 0:
        return float(DEFAULT_BUDGET_PER_MIN)
    return min(value, float(IP_LIMIT_PER_MIN))


def default_registry_dir() -> Path:
    """Per-user directory where live processes register (not the data dir)."""
    override = os.environ.get("HYPERDATA_HL_REGISTRY_DIR")
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Caches" / "hyperdata" / "hl-instances"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(base) / "hyperdata" / "hl-instances"
    base = os.environ.get("XDG_CACHE_HOME") or str(home / ".cache")
    if not base:
        base = tempfile.gettempdir()
    return Path(base) / "hyperdata" / "hl-instances"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Seconds from a Retry-After header (delta seconds or an HTTP date)."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError):
        return None
    return max(0.0, when - (time.time() if now is None else now))


class HyperliquidRateLimited(Exception):
    """Hyperliquid answered HTTP 429; every caller is paused."""


class HLRateGovernor:
    def __init__(self, budget_per_min: float | None = None, registry_dir: Path | None = None,
                 clock=time.time, sleep=None) -> None:
        self.budget_per_min = float(budget_per_min) if budget_per_min else _budget_from_env()
        self.registry_dir = registry_dir
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._window: deque[tuple[float, float, str]] = deque()
        self._used = 0.0
        self.active_elastic: set[str] = set(ELASTIC_WEIGHTS)
        self.paused_until = 0.0
        self._consecutive_429 = 0
        self.http_429_total = 0
        self.http_429_by_component: dict[str, int] = {}
        self.weight_by_component: dict[str, float] = {}
        self.requests_total = 0
        self.waited_seconds = 0.0
        # Cross-process view, refreshed by the heartbeat.
        self._others_used = 0.0
        self._instances = 1
        self._others_paused_until = 0.0
        self._ws_open = 0
        self._others_ws_open = 0
        self._registered: Path | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._starts = 0

    # ── window bookkeeping ───────────────────────────────────────

    def _prune(self, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        while self._window and self._window[0][0] <= cutoff:
            _, w, _ = self._window.popleft()
            self._used -= w
        if not self._window:
            self._used = 0.0

    def used_last_minute(self, component: str | None = None, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        self._prune(now)
        if component is None:
            return self._used
        return sum(w for _, w, c in self._window if c == component)

    def _used_since(self, since: float, component: str | None = None) -> float:
        return sum(w for t, w, c in self._window if t > since and (component is None or c == component))

    # ── allocation ───────────────────────────────────────────────

    @property
    def instances(self) -> int:
        return self._instances

    def share_per_min(self) -> float:
        """This process's share of the machine-wide budget right now."""
        fair = self.budget_per_min / max(1, self._instances)
        if self._instances <= 1:
            return fair
        # A newcomer (or a process whose peers just started) only takes
        # what the others are not using yet; floor so nobody starves.
        return max(min(fair, self.budget_per_min - self._others_used), 0.05 * self.budget_per_min)

    def allocation(self, component: str) -> float:
        """Weight a minute an elastic component may use (share for others)."""
        share = self.share_per_min()
        if component not in ELASTIC_WEIGHTS:
            return share
        reserve = min(ESSENTIAL_RESERVE_PER_MIN, 0.5 * share)
        pool = max(0.0, share - reserve)
        active = {c: w for c, w in ELASTIC_WEIGHTS.items() if c in self.active_elastic} or ELASTIC_WEIGHTS
        alloc = pool * active.get(component, 0.0) / sum(active.values())
        cap = ELASTIC_MAX_PER_MIN.get(component)
        return min(alloc, cap) if cap else alloc

    def set_active_elastic(self, components) -> None:
        self.active_elastic = set(components)

    def _wait_seconds(self, weight: float, component: str, now: float) -> float:
        """0 if the request may go now, else how long to wait before rechecking."""
        paused = max(self.paused_until, self._others_paused_until)
        if paused > now:
            return paused - now
        self._prune(now)
        share = self.share_per_min()
        limits = [(self._used, share, None)]
        if component in ELASTIC_WEIGHTS:
            limits.append((self.used_last_minute(component, now), self.allocation(component), component))
            elastic_used = sum(self.used_last_minute(c, now) for c in ELASTIC_WEIGHTS)
            limits.append((elastic_used, max(0.0, share - min(ESSENTIAL_RESERVE_PER_MIN, 0.5 * share)), "elastic"))
        for used, limit, comp in limits:
            if used + weight > limit and used > 0:
                # Wait until enough of the oldest weight leaves the window.
                need = used + weight - limit
                freed = 0.0
                for t, w, c in self._window:
                    if comp is not None and comp != "elastic" and c != comp:
                        continue
                    if comp == "elastic" and c not in ELASTIC_WEIGHTS:
                        continue
                    freed += w
                    if freed >= need:
                        return max(0.05, t + WINDOW_SECONDS - now)
                return 1.0
        burst_limit = max(weight, BURST_SHARE * share)
        burst_used = self._used_since(now - BURST_WINDOW_SECONDS)
        if burst_used > 0 and burst_used + weight > burst_limit:
            return 0.25
        return 0.0

    async def acquire(self, weight: float, component: str = "other") -> None:
        """Wait until `weight` fits this process's budget, then charge it."""
        started = None
        while True:
            now = self._clock()
            wait = self._wait_seconds(weight, component, now)
            if wait <= 0:
                self._charge(weight, component, now)
                if started is not None:
                    self.waited_seconds += now - started
                return
            if started is None:
                started = now
            await self._sleep(min(wait, 5.0))

    def _charge(self, weight: float, component: str, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        self._window.append((now, float(weight), component))
        self._used += weight
        self.weight_by_component[component] = self.weight_by_component.get(component, 0.0) + weight

    def charge(self, weight: float, component: str = "other") -> None:
        """Add weight Hyperliquid charged after answering (per item returned)."""
        if weight > 0:
            self._charge(weight, component)

    # ── 429 ──────────────────────────────────────────────────────

    def on_429(self, retry_after: str | None = None, component: str = "other") -> float:
        """Pause every caller; returns the pause in seconds."""
        now = self._clock()
        self.http_429_total += 1
        self.http_429_by_component[component] = self.http_429_by_component.get(component, 0) + 1
        self._consecutive_429 += 1
        backoff = min(MAX_BACKOFF_SECONDS, 2.0 ** self._consecutive_429)
        server = parse_retry_after(retry_after, now)
        pause = max(backoff, server) if server is not None else backoff
        self.paused_until = max(self.paused_until, now + pause)
        logger.warning(
            "[hl-rate] HTTP 429 from Hyperliquid (%s, %d in a row): pausing every Hyperliquid request %.0fs",
            component, self._consecutive_429, pause,
        )
        return pause

    def on_success(self) -> None:
        self._consecutive_429 = 0

    # ── websocket accounting (Hyperliquid allows 10 per IP) ─────

    def ws_opened(self) -> None:
        self._ws_open += 1

    def ws_closed(self) -> None:
        self._ws_open = max(0, self._ws_open - 1)

    # ── cross-process registry ───────────────────────────────────

    def _registry(self) -> Path:
        return self.registry_dir or default_registry_dir()

    async def start(self) -> None:
        """Register this process and begin heartbeating (live hubs call this)."""
        self._starts += 1
        if self._starts > 1:
            return
        try:
            directory = self._registry()
            directory.mkdir(parents=True, exist_ok=True)
            self._registered = directory / f"{os.getpid()}-{time.time_ns()}.json"
            self._heartbeat()
        except OSError as exc:
            self._registered = None
            logger.warning("[hl-rate] cannot register in %s (%s); assuming this is the only process",
                           self._registry(), exc)
        self._heartbeat_task = asyncio.get_running_loop().create_task(self._heartbeat_loop(), name="hl-rate")

    async def stop(self) -> None:
        self._starts = max(0, self._starts - 1)
        if self._starts:
            return
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        if self._registered is not None:
            try:
                self._registered.unlink()
            except OSError:
                pass
            self._registered = None
        self._instances = 1
        self._others_used = 0.0

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                await asyncio.to_thread(self._heartbeat)
            except Exception:
                logger.debug("[hl-rate] heartbeat failed", exc_info=True)

    def _heartbeat(self) -> None:
        """Publish our usage and read every other live process's."""
        now = time.time()
        if self._registered is not None:
            record = {
                "pid": os.getpid(), "heartbeat": now, "used_60s": self.used_last_minute(),
                "paused_until": self.paused_until, "ws_open": self._ws_open,
                "budget_per_min": self.budget_per_min,
            }
            tmp = self._registered.with_suffix(".tmp")
            tmp.write_text(json.dumps(record))
            os.replace(tmp, self._registered)
        others_used = 0.0
        others_ws = 0
        paused = 0.0
        instances = 1
        directory = self._registry()
        for path in directory.glob("*.json") if directory.is_dir() else []:
            if self._registered is not None and path == self._registered:
                continue
            try:
                record = json.loads(path.read_text())
                pid = int(record.get("pid", 0))
                beat = float(record.get("heartbeat", 0))
            except (OSError, ValueError, TypeError):
                continue
            if now - beat > STALE_INSTANCE_SECONDS or not _pid_alive(pid):
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            instances += 1
            others_used += float(record.get("used_60s", 0) or 0)
            others_ws += int(record.get("ws_open", 0) or 0)
            paused = max(paused, float(record.get("paused_until", 0) or 0))
        if instances != self._instances:
            logger.info("[hl-rate] %d hyperdata processes share the Hyperliquid budget; this one gets "
                        "%.0f weight a minute", instances, self.budget_per_min / instances)
        self._instances = instances
        self._others_used = others_used
        self._others_ws_open = others_ws
        self._others_paused_until = paused
        if self._ws_open + others_ws > 10:
            logger.warning("[hl-rate] %d Hyperliquid websockets open on this machine; Hyperliquid allows "
                           "10 per IP", self._ws_open + others_ws)

    # ── reporting ────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        now = self._clock()
        return {
            "budget_per_min": self.budget_per_min,
            "processes_sharing": self._instances,
            "share_per_min": round(self.share_per_min(), 1),
            "used_last_60s": round(self.used_last_minute(now=now), 1),
            "others_used_last_60s": round(self._others_used, 1),
            "allocation_per_min": {c: round(self.allocation(c), 1) for c in sorted(self.active_elastic)},
            "http_429_total": self.http_429_total,
            "http_429_by_component": dict(self.http_429_by_component),
            "paused_for_seconds": round(max(0.0, max(self.paused_until, self._others_paused_until) - now), 1),
            "seconds_waited_total": round(self.waited_seconds, 1),
            "websockets_open": self._ws_open,
            "websockets_open_machine": self._ws_open + self._others_ws_open,
        }


_governor: HLRateGovernor | None = None


def get_governor() -> HLRateGovernor:
    global _governor
    if _governor is None:
        _governor = HLRateGovernor()
    return _governor


def reset_governor(governor: HLRateGovernor | None = None) -> HLRateGovernor:
    """Replace the process-wide governor (tests), dropping the old one's
    registration so it is not counted as another process."""
    global _governor
    old = _governor
    if old is not None and old is not governor:
        task, old._heartbeat_task = old._heartbeat_task, None
        if task is not None and not task.done():
            try:
                task.cancel()
            except RuntimeError:
                pass  # its event loop is already closed
        if old._registered is not None:
            try:
                old._registered.unlink()
            except OSError:
                pass
            old._registered = None
    _governor = governor or HLRateGovernor()
    return _governor


async def hl_info(
    session: aiohttp.ClientSession, payload: dict, *, component: str,
    timeout: aiohttp.ClientTimeout | None = None,
) -> Any:
    """POST one info request through the governor; returns the parsed JSON.

    Raises HyperliquidRateLimited on 429 (after pausing every caller) and
    aiohttp.ClientResponseError on any other non-2xx answer.
    """
    gov = get_governor()
    await gov.acquire(request_weight(payload), component)
    gov.requests_total += 1
    async with session.post(
        HL_INFO_URL, json=payload, headers={"Content-Type": "application/json"},
        timeout=timeout or aiohttp.ClientTimeout(total=10, connect=3, sock_connect=3, sock_read=5),
    ) as resp:
        status = getattr(resp, "status", 200)
        if status == 429:
            headers = getattr(resp, "headers", None) or {}
            gov.on_429(headers.get("Retry-After"), component)
            raise HyperliquidRateLimited(f"Hyperliquid 429 on {payload.get('type')}")
        if isinstance(status, int) and status >= 400:
            resp.raise_for_status()
        data = await resp.json()
    gov.on_success()
    gov.charge(response_extra_weight(payload, data), component)
    return data
