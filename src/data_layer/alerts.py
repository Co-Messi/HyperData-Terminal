"""
Alert system — sends notifications for significant market events.
Supports Telegram and Discord webhooks.

Usage:
    alerts = AlertManager()
    alerts.attach(hub)  # Automatically monitors for alert conditions

Environment variables:
    TELEGRAM_BOT_TOKEN - Telegram bot token
    TELEGRAM_CHAT_ID - Chat/channel ID to send to
    DISCORD_WEBHOOK_URL - Discord webhook URL
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


@dataclass
class AlertConfig:
    """Thresholds for triggering alerts."""
    min_liquidation_usd: float = 500_000      # Alert on liqs > $500K
    min_whale_position_usd: float = 5_000_000  # Alert on whale positions > $5M
    extreme_funding_annual: float = 1.0        # Alert on >100% annualized funding
    liquidation_cascade_count: int = 50        # Alert if >50 liqs in 5 minutes
    liquidation_cascade_volume_usd: float = 500_000  # Alert if >$500K volume in 5 minutes
    liquidation_cascade_window: int = 300      # 5 minutes
    smart_money_max_rank: int = 5              # Alert on top N smart money wallets
    hlp_zscore_threshold: float = 2.0          # Alert on HLP Z-score extremes
    cooldown_seconds: float = 300.0            # 5 min cooldown between similar alerts (was 60s — too noisy)


class AlertManager:
    INTEL_INTERVAL = 4 * 3600  # 4 hours

    def __init__(self, config: AlertConfig | None = None):
        self.config = config or AlertConfig()
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.discord_webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
        self._session: aiohttp.ClientSession | None = None
        self._last_alert_times: dict[str, float] = {}
        self._recent_liq_count = 0
        self._recent_liq_volume = 0.0
        self._recent_liq_long_count = 0
        self._recent_liq_long_volume = 0.0
        self._recent_liq_short_count = 0
        self._recent_liq_short_volume = 0.0
        self._recent_liq_top_event = None  # largest single liq in the window
        self._recent_liq_reset = time.time()
        self.enabled = bool(self.telegram_token and self.telegram_chat_id) or bool(self.discord_webhook)
        self.alerts_sent = 0

        # Market intelligence summaries
        self._hub = None
        self._intel_task: asyncio.Task | None = None

        if not self.enabled:
            logger.info("AlertManager: No TELEGRAM_BOT_TOKEN/CHAT_ID or DISCORD_WEBHOOK_URL set. Alerts disabled.")
            logger.info("AlertManager: Set env vars to enable. See .env.example")

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        if self._hub:
            self._intel_task = asyncio.create_task(self._intel_loop())

    async def stop(self) -> None:
        if self._intel_task:
            self._intel_task.cancel()
            try:
                await self._intel_task
            except asyncio.CancelledError:
                pass
        self._intel_task = None
        if self._session and not self._session.closed:
            await self._session.close()

    def attach(self, hub) -> None:
        """Attach to hub event bus."""
        self._hub = hub
        hub.on_liquidation(self._check_liquidation)
        hub.on_signal(self._check_smart_money)
        # Start loops if we're already running (attach called after start)
        if self._session and not self._intel_task:
            self._intel_task = asyncio.create_task(self._intel_loop())

    # Per-key cooldown overrides (seconds)
    _COOLDOWN_OVERRIDES = {
        "cascade_alert": 600,   # 10 minutes between cascade alerts
    }

    def _should_alert(self, key: str) -> bool:
        """Check cooldown for a specific alert type."""
        now = time.time()
        last = self._last_alert_times.get(key, 0)
        cooldown = self._COOLDOWN_OVERRIDES.get(key, self.config.cooldown_seconds)
        if now - last < cooldown:
            return False
        self._last_alert_times[key] = now
        return True

    def _reset_cascade_window(self) -> None:
        """Reset all cascade tracking counters."""
        self._recent_liq_count = 0
        self._recent_liq_volume = 0.0
        self._recent_liq_long_count = 0
        self._recent_liq_long_volume = 0.0
        self._recent_liq_short_count = 0
        self._recent_liq_short_volume = 0.0
        self._recent_liq_top_event = None
        self._recent_liq_reset = time.time()

    def _check_liquidation(self, event) -> None:
        """Check if a liquidation event warrants an alert."""
        # Track cascade window — reset if window expired
        now = time.time()
        if now - self._recent_liq_reset > self.config.liquidation_cascade_window:
            self._reset_cascade_window()

        # Accumulate cascade stats
        self._recent_liq_count += 1
        self._recent_liq_volume += event.size_usd

        if event.side == "long":
            self._recent_liq_long_count += 1
            self._recent_liq_long_volume += event.size_usd
        else:
            self._recent_liq_short_count += 1
            self._recent_liq_short_volume += event.size_usd

        # Track top liquidation in the window
        if self._recent_liq_top_event is None or event.size_usd > self._recent_liq_top_event.size_usd:
            self._recent_liq_top_event = event

        # Large single liquidation — DISABLED (too noisy, use trade alerts instead)
        # if event.size_usd >= self.config.min_liquidation_usd:
        #     if self._should_alert(f"large_liq_{event.symbol}"):
        #         ...
        pass

        # Liquidation cascade — trigger on count OR volume threshold
        cascade_triggered = (
            self._recent_liq_count >= self.config.liquidation_cascade_count
            or self._recent_liq_volume >= self.config.liquidation_cascade_volume_usd
        )
        # Liquidation cascade — DISABLED (too noisy, use trade alerts instead)
        # if cascade_triggered:
        #     ...
        if cascade_triggered:
            self._reset_cascade_window()  # still reset the window, just don't send

    def _check_smart_money(self, signal) -> None:
        """Check if a smart money signal warrants an alert (top-5 ranked, follow type)."""
        if signal.wallet_rank > self.config.smart_money_max_rank or signal.wallet_rank <= 0:
            return
        if signal.signal_type != "follow":
            return
        if not self._should_alert(f"smart_money_{signal.address[:10]}"):
            return

        # Smart money alerts — DISABLED (use trade alerts instead)
        pass

    async def _check_hlp_zscore(self, hlp_stats: dict) -> None:
        """Check if HLP Z-score is at an extreme (>2.0 or <-2.0)."""
        zscore = hlp_stats.get("delta_zscore", 0)
        if abs(zscore) <= self.config.hlp_zscore_threshold:
            return
        if not self._should_alert("hlp_zscore"):
            return

        # HLP Z-score alerts — DISABLED (too noisy)
        pass

    # ── Market Intelligence ─────────────────────────────────────────

    async def _intel_loop(self) -> None:
        """Market intelligence summary — DISABLED (use daily trade summary instead)."""
        while True:
            await asyncio.sleep(self.INTEL_INTERVAL)
            # Disabled — trade alerts handle notifications now
            pass

    async def send_intel_now(self) -> bool:
        """Send an immediate market intelligence report (for testing)."""
        if not self.enabled or not self._hub:
            return False
        report = self._build_intel_report()
        await self._send(report)
        return True

    @staticmethod
    def _fmt_usd(v: float) -> str:
        """Format a USD value with appropriate suffix."""
        if abs(v) >= 1_000_000_000:
            return f"${v / 1_000_000_000:,.1f}B"
        if abs(v) >= 1_000_000:
            return f"${v / 1_000_000:,.1f}M"
        if abs(v) >= 1_000:
            return f"${v / 1_000:,.1f}K"
        return f"${v:,.0f}"

    @staticmethod
    def _fmt_usd_signed(v: float) -> str:
        """Format a signed USD value with appropriate suffix."""
        sign = "+" if v >= 0 else ""
        if abs(v) >= 1_000_000_000:
            return f"{sign}${v / 1_000_000_000:,.1f}B"
        if abs(v) >= 1_000_000:
            return f"{sign}${v / 1_000_000:,.1f}M"
        if abs(v) >= 1_000:
            return f"{sign}${v / 1_000:,.1f}K"
        return f"{sign}${v:,.0f}"

    def _build_intel_report(self) -> str:
        """Build the 4-hour market intelligence summary."""
        hub = self._hub
        lines = []
        lines.append("📊 HYPERDATA MARKET INTELLIGENCE")
        lines.append("━" * 30)
        lines.append("")

        # ── 1. Top 3 CVD Momentum ──────────────────────────────────
        lines.append("⚡ TOP 3 CVD MOMENTUM")
        try:
            cvd_data = []
            for sym in hub.orderflow.symbols:
                if sym not in hub.orderflow.buckets:
                    continue
                snap = hub.orderflow.get_snapshot(sym, "4h")
                cvd_data.append((sym, snap.cvd, snap.ofi, snap.signal))
            # Sort by absolute OFI descending
            cvd_data.sort(key=lambda x: abs(x[2]), reverse=True)
            for i, (sym, cvd, ofi, signal) in enumerate(cvd_data[:3], 1):
                cvd_str = self._fmt_usd_signed(cvd)
                lines.append(f"{i}. {sym} — CVD: {cvd_str} | OFI: {ofi:+.2f} | {signal}")
            if not cvd_data:
                lines.append("   No data available")
        except Exception:
            lines.append("   Data unavailable")
        lines.append("")

        # ── 2. Liquidations (4h window) ────────────────────────────
        lines.append("💥 LIQUIDATIONS (4h)")
        try:
            liq_stats = hub.liquidations.get_stats(window_minutes=240)
            total_count = liq_stats["total_count"]
            total_vol = liq_stats["total_volume_usd"]
            long_c = liq_stats["long_count"]
            long_v = liq_stats["long_volume_usd"]
            short_c = liq_stats["short_count"]
            short_v = liq_stats["short_volume_usd"]

            lines.append(f"Total: {total_count:,} events | {self._fmt_usd(total_vol)} volume")
            lines.append(
                f"Long: {long_c:,} ({self._fmt_usd(long_v)}) | "
                f"Short: {short_c:,} ({self._fmt_usd(short_v)})"
            )

            # Find the top single liquidation in the window
            cutoff = time.time() - 240 * 60
            top_liq = None
            for ev in hub.liquidations.events:
                if ev.timestamp < cutoff:
                    continue
                if top_liq is None or ev.size_usd > top_liq.size_usd:
                    top_liq = ev
            if top_liq:
                lines.append(
                    f"Top: {top_liq.symbol} {top_liq.side.upper()} "
                    f"{self._fmt_usd(top_liq.size_usd)} on {top_liq.exchange.capitalize()}"
                )

            # Exchange breakdown (abbreviated)
            by_ex = liq_stats.get("by_exchange", {})
            if by_ex:
                ex_abbrev = {"binance": "BIN", "bybit": "BYB", "okx": "OKX", "hyperliquid": "HYP"}
                ex_parts = []
                for ex_name, ex_data in sorted(by_ex.items(), key=lambda x: -x[1]["count"]):
                    abbr = ex_abbrev.get(ex_name, ex_name[:3].upper())
                    ex_parts.append(f"{abbr}:{ex_data['count']}")
                lines.append(f"By exchange: {' '.join(ex_parts)}")
        except Exception:
            lines.append("   Data unavailable")
        lines.append("")

        # ── 3. Smart Money ─────────────────────────────────────────
        lines.append("🧠 SMART MONEY")
        try:
            sm_stats = hub.smart_money.get_stats()
            total_wallets = sm_stats["total_wallets"]
            ranked_wallets = sm_stats["ranked_wallets"]
            lines.append(f"Tracked: {total_wallets:,} wallets | Ranked: {ranked_wallets:,}")

            # Top signal from a ranked wallet recently
            recent_signals = hub.smart_money.get_recent_signals(50)
            follow_signals = [s for s in recent_signals if s.signal_type == "follow" and s.wallet_rank > 0]
            if follow_signals:
                # Get the most notable one (lowest rank = best wallet)
                top_sig = min(follow_signals, key=lambda s: s.wallet_rank)
                action_str = top_sig.action.replace("_", " ")
                lines.append(
                    f"Top signal: #{top_sig.wallet_rank} {action_str} "
                    f"{top_sig.symbol} {self._fmt_usd(top_sig.size_usd)}"
                )

            # Compute sentiment: % of follow signals that are bullish
            if follow_signals:
                bullish = sum(1 for s in follow_signals if s.action in ("OPEN_LONG", "CLOSE_SHORT"))
                pct_bull = bullish / len(follow_signals) * 100 if follow_signals else 50
                if pct_bull >= 60:
                    sentiment = "bullish (smart money net long)"
                elif pct_bull <= 40:
                    sentiment = "bearish (smart money net short)"
                else:
                    sentiment = "neutral"
                lines.append(f"Sentiment: {pct_bull:.0f}% bullish ({sentiment})")
            else:
                lines.append("No recent follow signals")
        except Exception:
            lines.append("   Data unavailable")
        lines.append("")

        # ── 4. HLP Vault ──────────────────────────────────────────
        lines.append("🏛️ HLP VAULT")
        try:
            hlp_stats = hub.hlp.get_stats()
            aum = hlp_stats["account_value"]
            delta = hlp_stats["net_delta"]
            zscore = hlp_stats["delta_zscore"]
            session_pnl = hlp_stats["session_pnl"]
            absorptions = hlp_stats["liquidation_absorptions"]

            lines.append(f"AUM: {self._fmt_usd(aum)} | Delta: {self._fmt_usd_signed(delta)} | Z: {zscore:+.1f}")
            lines.append(f"Session PnL: {self._fmt_usd_signed(session_pnl)}")

            # Count absorptions in the 4h window
            recent_absorptions = hub.hlp.get_liquidation_absorptions(minutes=240)
            lines.append(f"Absorptions: {len(recent_absorptions)} in 4h")
        except Exception:
            lines.append("   Data unavailable")

        # ── Footer ─────────────────────────────────────────────────
        lines.append("")
        lines.append("━" * 30)
        lines.append("🤖 HyperData Terminal | hyperdata.dev")

        return "\n".join(lines)

    async def _send(self, message: str) -> None:
        """Send alert to all configured channels."""
        if not self._session:
            self._session = aiohttp.ClientSession()

        self.alerts_sent += 1

        # Telegram
        if self.telegram_token and self.telegram_chat_id:
            try:
                url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
                await self._session.post(url, json={
                    "chat_id": self.telegram_chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                })
                logger.info("Telegram alert sent")
            except Exception:
                logger.exception("Telegram send failed")

        # Discord
        if self.discord_webhook:
            try:
                await self._session.post(self.discord_webhook, json={
                    "content": message,
                })
                logger.info("Discord alert sent")
            except Exception:
                logger.exception("Discord send failed")

        # Always log to console
        logger.warning("ALERT: %s", message.replace("\n", " | "))

    async def send_test(self) -> bool:
        """Send a test alert to verify configuration."""
        if not self.enabled:
            return False
        await self._send("🧪 HyperData Alert Test — Connection verified!")
        return True
