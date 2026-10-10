"""Telegram/Discord liquidation cascade alerts (H4): a configured alert must
actually be sent, once per window per cooldown, from confirmed
liquidations only, with every interpolated value escaped for Telegram HTML."""
from __future__ import annotations

import asyncio
import time

import pytest

from hyperdata_terminal.data_layer.alerts import AlertManager, CascadeRule
from hyperdata_terminal.data_layer.liquidation_feed import LiquidationEvent


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Records every POST; answers with a fixed status."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.posts: list[tuple[str, dict]] = []
        self.closed = False

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return _Resp(self.status)

    async def close(self):
        self.closed = True


RULES = (
    CascadeRule(300.0, 1_000_000.0, 900.0, "5 minutes"),
    CascadeRule(3600.0, 5_000_000.0, 3600.0, "1 hour"),
)


def _manager(monkeypatch, session: _Session, telegram=True, discord=True) -> AlertManager:
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    if telegram:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    if discord:
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    mgr = AlertManager(rules=RULES)
    mgr._session = session
    return mgr


def _liq(size: float, symbol: str = "BTC", side: str = "long", exchange: str = "okx", confirmed: bool = True):
    return LiquidationEvent(time.time(), exchange, symbol, side, size, 80_000.0, size / 80_000.0, confirmed)


async def _drain(mgr: AlertManager) -> None:
    while mgr._tasks:
        await asyncio.gather(*list(mgr._tasks))


@pytest.mark.asyncio
async def test_cascade_sends_once_to_every_channel(monkeypatch):
    session = _Session()
    mgr = _manager(monkeypatch, session)
    now = time.time()
    for i in range(5):
        mgr._check_liquidation(_liq(300_000.0), now=now + i)
    await _drain(mgr)

    assert mgr.alerts_sent == 1  # crossed $1M on the 4th event; the 5th is inside the cooldown
    urls = [u for u, _ in session.posts]
    assert urls == ["https://api.telegram.org/bot123:abc/sendMessage", "https://discord.example/webhook"]
    tg = session.posts[0][1]
    assert tg["parse_mode"] == "HTML" and tg["chat_id"] == "42"
    assert "$1.2M confirmed in 5 minutes" in tg["text"]
    assert "Longs liquidated $1.2M" in session.posts[1][1]["content"]


@pytest.mark.asyncio
async def test_estimated_large_prints_never_trigger(monkeypatch):
    session = _Session()
    mgr = _manager(monkeypatch, session)
    for _ in range(20):
        mgr._check_liquidation(_liq(500_000.0, exchange="hyperliquid", confirmed=False))
    await _drain(mgr)
    assert session.posts == [] and mgr.cascades_detected == 0


@pytest.mark.asyncio
async def test_each_window_has_its_own_cooldown(monkeypatch):
    session = _Session()
    mgr = _manager(monkeypatch, session, discord=False)
    now = time.time()
    mgr._check_liquidation(_liq(2_000_000.0), now=now)          # 5m fires
    mgr._check_liquidation(_liq(2_000_000.0), now=now + 60)     # 5m in cooldown
    mgr._check_liquidation(_liq(2_000_000.0), now=now + 120)    # 1h reaches $6M: fires
    mgr._check_liquidation(_liq(2_000_000.0), now=now + 1000)   # 5m cooldown over: fires again
    await _drain(mgr)
    texts = [p[1]["text"] for p in session.posts]
    assert len(texts) == 3
    assert "in 5 minutes" in texts[0] and "in 1 hour" in texts[1] and "in 5 minutes" in texts[2]


@pytest.mark.asyncio
async def test_telegram_html_is_escaped(monkeypatch):
    session = _Session()
    mgr = _manager(monkeypatch, session, discord=False)
    mgr._check_liquidation(_liq(2_000_000.0, symbol="<b>X&Y"))
    await _drain(mgr)
    text = session.posts[0][1]["text"]
    assert "&lt;b&gt;X&amp;Y" in text
    assert "<b>X&Y" not in text
    assert text.startswith("<b>Liquidation cascade")  # our own markup survives


@pytest.mark.asyncio
async def test_failed_delivery_is_counted_not_reported_as_sent(monkeypatch):
    session = _Session(status=500)
    mgr = _manager(monkeypatch, session)
    mgr._check_liquidation(_liq(2_000_000.0))
    await _drain(mgr)
    assert mgr.alerts_sent == 0 and mgr.alerts_failed == 1


def test_cli_alerts_test_without_credentials_fails_cleanly(monkeypatch, capsys):
    from hyperdata_terminal import cli

    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "_load_env", lambda: None)
    with pytest.raises(SystemExit) as exc:
        cli.main(["alerts", "--test"])
    assert exc.value.code == 2
    assert "no alert channel configured" in capsys.readouterr().err


def test_cli_alerts_test_sends_one_message(monkeypatch, capsys):
    from hyperdata_terminal import cli
    from hyperdata_terminal.data_layer import alerts

    session = _Session()
    monkeypatch.setattr(cli, "_load_env", lambda: None)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(alerts.aiohttp, "ClientSession", lambda *a, **k: session)
    with pytest.raises(SystemExit) as exc:
        cli.main(["alerts", "--test"])
    assert exc.value.code == 0
    assert [u for u, _ in session.posts] == ["https://discord.example/webhook"]
    assert "discord: delivered" in capsys.readouterr().out
