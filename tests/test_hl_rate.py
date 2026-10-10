"""Process-wide (and machine-wide) Hyperliquid weight governor (H6)."""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from hyperdata_terminal.data_layer import hl_rate
from hyperdata_terminal.data_layer.hl_rate import (
    HLRateGovernor,
    HyperliquidRateLimited,
    parse_retry_after,
    request_weight,
    response_extra_weight,
)


class FakeTime:
    """A clock that only moves when the governor sleeps."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _gov(budget: float = 1000.0, t: FakeTime | None = None, **kw) -> tuple[HLRateGovernor, FakeTime]:
    t = t or FakeTime()
    return HLRateGovernor(budget_per_min=budget, clock=t.clock, sleep=t.sleep, **kw), t


def test_weights_follow_the_documented_table():
    assert request_weight({"type": "clearinghouseState"}) == 2
    assert request_weight({"type": "allMids"}) == 2
    assert request_weight({"type": "l2Book"}) == 2
    assert request_weight({"type": "metaAndAssetCtxs"}) == 20
    assert request_weight({"type": "userRole"}) == 60
    assert response_extra_weight({"type": "userFills"}, [{}] * 2000) == 100
    assert response_extra_weight({"type": "recentTrades"}, [{}] * 45) == 2
    assert response_extra_weight({"type": "candleSnapshot"}, [{}] * 130) == 2
    assert response_extra_weight({"type": "clearinghouseState"}, {"x": 1}) == 0


@pytest.mark.asyncio
async def test_a_process_never_exceeds_its_share_in_any_minute():
    gov, t = _gov(budget=600.0)
    sent: list[tuple[float, float]] = []

    async def caller(component: str, weight: float, n: int) -> None:
        for _ in range(n):
            await gov.acquire(weight, component)
            sent.append((t.now, weight))

    await asyncio.gather(
        caller("market_data", 20, 40), caller("position_scanner", 2, 400), caller("hlp", 20, 30),
    )
    sent.sort()
    worst = 0.0
    for i, (start, _) in enumerate(sent):
        worst = max(worst, sum(w for ts, w in sent[i:] if ts < start + 60))
    assert worst <= 600.0


@pytest.mark.asyncio
async def test_elastic_callers_keep_to_their_allocation_and_leave_room():
    gov, t = _gov(budget=1000.0)
    gov.set_active_elastic({"position_scanner", "smart_money"})
    # Smart money tries to spend a whole minute's budget on userFills.
    for _ in range(30):
        await gov.acquire(20, "smart_money")
        gov.charge(100, "smart_money")
    assert gov.used_last_minute("smart_money") <= gov.allocation("smart_money") + 120
    # A fixed cadence caller is not stuck behind it.
    before = t.now
    await gov.acquire(20, "market_data")
    assert t.now - before < 61


@pytest.mark.asyncio
async def test_http_429_pauses_every_caller_for_retry_after(monkeypatch):
    gov, t = _gov()
    pause = gov.on_429("17", component="position_scanner")
    assert pause == 17 and gov.http_429_total == 1
    start = t.now
    await gov.acquire(2, "market_data")  # a different component waits too
    assert t.now - start >= 17
    # Without a header: exponential backoff (2s, 4s, ...), capped.
    assert gov.on_429(None) == 4.0
    gov.on_success()
    assert gov.on_429(None) == 2.0
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412480.0 - 10) == pytest.approx(10.0)


class _Resp:
    def __init__(self, status, data=None, headers=None):
        self.status = status
        self._data = data
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._data

    def raise_for_status(self):
        raise RuntimeError(f"HTTP {self.status}")


class _Session:
    def __init__(self, resp):
        self.resp = resp
        self.calls = 0
        self.closed = False

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        return self.resp


@pytest.mark.asyncio
async def test_hl_info_charges_per_item_weight_and_raises_on_429():
    gov, _ = _gov()
    hl_rate.reset_governor(gov)
    data = await hl_rate.hl_info(_Session(_Resp(200, [{}] * 2000)), {"type": "userFills"}, component="smart_money")
    assert len(data) == 2000
    assert gov.used_last_minute() == 20 + 100
    with pytest.raises(HyperliquidRateLimited):
        await hl_rate.hl_info(_Session(_Resp(429, headers={"Retry-After": "5"})), {"type": "allMids"},
                              component="position_scanner")
    assert gov.http_429_by_component == {"position_scanner": 1}
    assert gov.paused_until > 0


@pytest.mark.asyncio
async def test_processes_on_one_machine_split_the_budget(tmp_path):
    a = HLRateGovernor(budget_per_min=1000.0, registry_dir=tmp_path)
    b = HLRateGovernor(budget_per_min=1000.0, registry_dir=tmp_path)
    await a.start()
    await b.start()
    try:
        a._heartbeat()
        b._heartbeat()
        assert a.instances == b.instances == 2
        assert a.share_per_min() == b.share_per_min() == 500.0
        # A peer that already used 900 in the last minute leaves a newcomer
        # only what is left until its window drains.
        for _ in range(45):
            b._charge(20, "market_data")
        b._heartbeat()
        a._heartbeat()
        assert a.share_per_min() == pytest.approx(100.0)
    finally:
        await a.stop()
        await b.stop()
    assert list(tmp_path.glob("*.json")) == []


def test_dead_and_stale_registrations_are_pruned(tmp_path):
    gov = HLRateGovernor(budget_per_min=1000.0, registry_dir=tmp_path)
    (tmp_path / "dead.json").write_text(json.dumps({"pid": 2 ** 22 + 7, "heartbeat": time.time(), "used_60s": 900}))
    (tmp_path / "old.json").write_text(json.dumps({"pid": os.getpid(), "heartbeat": time.time() - 3600}))
    gov._heartbeat()
    assert gov.instances == 1 and gov.share_per_min() == 1000.0
    assert list(tmp_path.glob("*.json")) == []


def test_a_peers_429_pauses_this_process(tmp_path):
    gov = HLRateGovernor(budget_per_min=1000.0, registry_dir=tmp_path)
    (tmp_path / "peer.json").write_text(json.dumps({
        "pid": os.getpid(), "heartbeat": time.time(), "used_60s": 10, "paused_until": time.time() + 30,
    }))
    gov._heartbeat()
    assert gov._wait_seconds(2, "market_data", time.time()) > 20


def test_budget_env_is_clamped_to_the_ip_limit(monkeypatch):
    monkeypatch.setenv("HYPERDATA_HL_WEIGHT_PER_MIN", "5000")
    assert HLRateGovernor().budget_per_min == 1200
    monkeypatch.setenv("HYPERDATA_HL_WEIGHT_PER_MIN", "junk")
    assert HLRateGovernor().budget_per_min == 1000
