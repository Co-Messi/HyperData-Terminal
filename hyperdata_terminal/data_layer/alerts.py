"""
Telegram and Discord alerts.

One alert fires: the liquidation cascade. When CONFIRMED liquidations
(Binance, Bybit, OKX, and Hyperliquid liquidations an HLP vault absorbed;
never Hyperliquid large prints) across all symbols reach a threshold
inside a window, one message goes to every configured channel. Each window
has its own cooldown, so a cascade produces one message per window per
cooldown, not one per liquidation.

    window      default threshold   cooldown   env override
    5 minutes   $10M                15 min     HYPERDATA_ALERT_CASCADE_5M_USD
    1 hour      $50M                60 min     HYPERDATA_ALERT_CASCADE_1H_USD

Channels (any or both):
    TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
    DISCORD_WEBHOOK_URL

``hyperdata alerts --test`` sends one test message to verify the setup.

Usage:
    alerts = AlertManager()
    await alerts.start()
    alerts.attach(hub)
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import os
import re
import time
from dataclasses import dataclass

import aiohttp
from dotenv import load_dotenv

from hyperdata_terminal.data_layer.cascade import CascadeDetector, WindowTotals

load_dotenv()

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CascadeRule:
    """Alert when confirmed liquidations in `window_seconds` reach `threshold_usd`."""
    window_seconds: float
    threshold_usd: float
    cooldown_seconds: float
    label: str


DEFAULT_CASCADE_5M_USD = 10_000_000.0
DEFAULT_CASCADE_1H_USD = 50_000_000.0


def _env_usd(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %.0f", name, raw, default)
        return default
    if not (math.isfinite(value) and value > 0):
        logger.warning("%s must be a positive number; using %.0f", name, default)
        return default
    return value


def cascade_rules_from_env() -> tuple[CascadeRule, ...]:
    return (
        CascadeRule(300.0, _env_usd("HYPERDATA_ALERT_CASCADE_5M_USD", DEFAULT_CASCADE_5M_USD), 900.0, "5 minutes"),
        CascadeRule(3600.0, _env_usd("HYPERDATA_ALERT_CASCADE_1H_USD", DEFAULT_CASCADE_1H_USD), 3600.0, "1 hour"),
    )


def fmt_usd(v: float) -> str:
    """$1.2B / $3.4M / $5.6K / $789."""
    if abs(v) >= 1_000_000_000:
        return f"${v / 1_000_000_000:,.1f}B"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:,.1f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:,.1f}K"
    return f"${v:,.0f}"


class AlertManager:
    # Deadline for webhook posts: a stalled Telegram/Discord endpoint must
    # not wedge whatever task is delivering the alert.
    _SEND_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=3, sock_connect=3, sock_read=5)

    def __init__(self, rules: tuple[CascadeRule, ...] | None = None):
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.discord_webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
        self.rules = rules if rules is not None else cascade_rules_from_env()
        self.detector = CascadeDetector(tuple(r.window_seconds for r in self.rules))
        self._last_fired: dict[float, float] = {}
        self._session: aiohttp.ClientSession | None = None
        self._tasks: set[asyncio.Task] = set()
        self._hub = None
        self.alerts_sent = 0      # delivered to at least one channel
        self.alerts_failed = 0    # fired, but no channel accepted it
        self.cascades_detected = 0
        if not self.enabled:
            logger.info(
                "AlertManager: no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID or DISCORD_WEBHOOK_URL set; "
                "cascades are detected and logged but not sent"
            )

    @property
    def enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id) or bool(self.discord_webhook)

    def channels(self) -> list[str]:
        out = []
        if self.telegram_token and self.telegram_chat_id:
            out.append("telegram")
        if self.discord_webhook:
            out.append("discord")
        return out

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        tasks = [t for t in self._tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def attach(self, hub) -> None:
        """Watch the hub's liquidation stream for cascades."""
        self._hub = hub
        hub.on_liquidation(self._check_liquidation)

    # ── Cascade detection ────────────────────────────────────────

    def _check_liquidation(self, event, now: float | None = None) -> None:
        if not self.detector.add(event, now):
            return  # estimated (Hyperliquid large print): never counted
        now = time.time() if now is None else now
        for rule in self.rules:
            totals = self.detector.totals(rule.window_seconds, now)
            if totals.volume_usd < rule.threshold_usd:
                continue
            if now - self._last_fired.get(rule.window_seconds, -math.inf) < rule.cooldown_seconds:
                continue
            self._last_fired[rule.window_seconds] = now
            self.cascades_detected += 1
            text, html_text = self.format_cascade(rule, totals)
            self._spawn(self._send(text, html_text))

    def _spawn(self, coro) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()  # no loop (a synchronous caller): nothing can be sent
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def format_cascade(self, rule: CascadeRule, totals: WindowTotals) -> tuple[str, str]:
        """(plain text for Discord and logs, HTML for Telegram). Every value
        interpolated into the HTML is escaped: symbols come from exchanges."""
        top = ", ".join(f"{sym} {fmt_usd(v)}" for sym, v in totals.top_symbols(3)) or "n/a"
        venues = ", ".join(f"{ex} {fmt_usd(v)}" for ex, v in sorted(totals.by_exchange.items()))
        lines = [
            f"Liquidation cascade: {fmt_usd(totals.volume_usd)} confirmed in {rule.label} "
            f"(threshold {fmt_usd(rule.threshold_usd)})",
            f"Longs liquidated {fmt_usd(totals.long_usd)}, shorts {fmt_usd(totals.short_usd)}, "
            f"{totals.count} events",
            f"Top: {top}",
            f"By venue: {venues}",
            "Confirmed liquidations only. Binance is throttled at the source; Hyperliquid "
            "counts only liquidations an HLP vault absorbed.",
        ]
        down = self._venues_down()
        if down:
            lines.append(f"Not receiving: {', '.join(down)} (totals are partial)")
        plain = "\n".join(lines)
        esc = html.escape
        html_lines = [f"<b>{esc(lines[0])}</b>", *(esc(line) for line in lines[1:])]
        return plain, "\n".join(html_lines)

    def _venues_down(self) -> list[str]:
        feed = getattr(self._hub, "liquidations", None)
        fn = getattr(feed, "venues_down", None)
        try:
            return list(fn()) if callable(fn) else []
        except Exception:
            return []

    # ── Delivery ─────────────────────────────────────────────────

    async def _deliver(self, text: str, html_text: str | None) -> dict[str, bool]:
        """Post to every configured channel; {channel: delivered}."""
        if self._session is None or getattr(self._session, "closed", False) is True:
            self._session = aiohttp.ClientSession()
        results: dict[str, bool] = {}
        if self.telegram_token and self.telegram_chat_id:
            payload = {"chat_id": self.telegram_chat_id, "text": html_text or html.escape(text),
                       "parse_mode": "HTML"}
            try:
                url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
                async with self._session.post(url, json=payload, timeout=self._SEND_TIMEOUT) as resp:
                    results["telegram"] = resp.status == 200
                    if resp.status != 200:
                        logger.warning("Telegram send returned %d", resp.status)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Exception type only: aiohttp error messages can embed the
                # request URL, which contains the bot token.
                results["telegram"] = False
                logger.warning("Telegram send failed: %s", type(exc).__name__)
        if self.discord_webhook:
            try:
                async with self._session.post(
                    self.discord_webhook, json={"content": text}, timeout=self._SEND_TIMEOUT,
                ) as resp:
                    results["discord"] = resp.status in (200, 204)
                    if not results["discord"]:
                        logger.warning("Discord send returned %d", resp.status)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Exception type only: error messages can embed the webhook URL.
                results["discord"] = False
                logger.warning("Discord send failed: %s", type(exc).__name__)
        return results

    async def _send(self, message: str, html_message: str | None = None) -> bool:
        """Send one alert to every configured channel. True if any accepted it."""
        results = await self._deliver(message, html_message)
        delivered = any(results.values())
        if delivered:
            self.alerts_sent += 1
        else:
            self.alerts_failed += 1
        # Log that an alert fired, never its payload: alert bodies can carry
        # wallet addresses, which must not sit in rotating plaintext logs.
        first_line = message.strip().splitlines()[0] if message.strip() else ""
        redacted = re.sub(r"0x[0-9a-fA-F]{40}", "0x…[redacted]", first_line)
        if delivered:
            logger.warning("ALERT sent via %s (%d total): %.80s",
                           ", ".join(c for c, ok in results.items() if ok), self.alerts_sent, redacted)
        elif results:
            logger.warning("ALERT not delivered (every channel failed): %.80s", redacted)
        else:
            logger.warning("ALERT not delivered (no channel configured): %.80s", redacted)
        return delivered

    async def send_test(self) -> dict[str, bool]:
        """Send one test message to every configured channel; {channel: delivered}."""
        text = "HyperData Terminal: test alert. If you can read this, cascade alerts reach this channel."
        results = await self._deliver(text, html.escape(text))
        if any(results.values()):
            self.alerts_sent += 1
        return results
