"""
LLM-powered trading strategy.

Sends a market data summary to any OpenAI-compatible API and asks for a
BUY / SELL / HOLD decision. Works with any OpenAI-compatible API:
OpenAI, Ollama, LM Studio, Groq, Together, etc.

Configure via environment variables (or .env file):
    LLM_BASE_URL  — API base URL   (default: http://localhost:11434/v1)
    LLM_MODEL     — Model name     (default: llama3)
    LLM_API_KEY   — API key        (default: empty, not needed for Ollama)
"""

from __future__ import annotations

import json
import logging
import os

import aiohttp

from .base import Strategy, Signal

logger = logging.getLogger(__name__)

# Try to load .env if python-dotenv is installed (optional dependency)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# System prompt sent to the LLM
SYSTEM_PROMPT = (
    "You are a crypto trading assistant. Based on the market data provided, "
    "respond with exactly one word on the first line: BUY, SELL, or HOLD. "
    "On the second line, give a brief reason (one sentence max)."
)


class LLMAgent(Strategy):
    """Strategy that delegates trading decisions to a language model."""

    def __init__(self, symbol: str = "BTC") -> None:
        self.symbol = symbol

        # Read config from environment
        self.base_url = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
        self.model = os.environ.get("LLM_MODEL", "llama3")
        self.api_key = os.environ.get("LLM_API_KEY", "")

    @property
    def name(self) -> str:
        return "llm_agent"

    def evaluate(self, hub) -> Signal | None:
        """Build a market summary and ask the LLM for a decision.

        NOTE: This calls an async HTTP endpoint. Since evaluate() is
        synchronous, we use asyncio to run the coroutine. If you're
        already in an async context, see _async_evaluate() directly.
        """
        import asyncio

        # If no API key and not using a local model, warn and skip
        if not self.api_key and "localhost" not in self.base_url:
            logger.warning(
                "LLM_API_KEY not set and not using localhost — skipping LLM agent. "
                "Set LLM_BASE_URL, LLM_MODEL, and LLM_API_KEY in your .env file."
            )
            return None

        try:
            # Run blocking LLM call in a thread so it doesn't stall the async loop
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self._sync_evaluate, hub)
                return future.result(timeout=20)
        except Exception:
            logger.exception("LLM agent error")
            return None

    def _sync_evaluate(self, hub) -> Signal | None:
        """Synchronous LLM call via urllib — no async dependency."""
        import json as _json
        import urllib.request

        summary = self._build_market_summary(hub)
        if summary is None:
            return None

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        payload = _json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": (
                    "You are a crypto trading assistant. Based on the market data provided, "
                    "respond with exactly one word on the first line: BUY, SELL, or HOLD. "
                    "On the second line, give a brief reason (under 20 words)."
                )},
                {"role": "user", "content": summary},
            ],
            "temperature": 0.3,
            "max_tokens": 60,
        }).encode()

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read())
            text = data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning("LLM API call failed: %s", e)
            return None

        return self._parse_response(text)

    async def _async_evaluate(self, hub) -> Signal | None:
        """Async version — call this directly from an async paper trader."""
        # ---- Build market summary from hub data ----
        summary = self._build_market_summary(hub)
        if summary is None:
            return None

        # ---- Call the LLM ----
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": summary},
            ],
            "max_tokens": 100,
            "temperature": 0.3,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning("LLM API returned %d: %s", resp.status, body[:200])
                        return None
                    data = await resp.json()
        except asyncio.TimeoutError:
            logger.warning("LLM API timed out after 15s")
            return None
        except aiohttp.ClientError as e:
            logger.warning("LLM API connection error: %s", e)
            return None

        # ---- Parse response ----
        try:
            text = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError):
            logger.warning("Unexpected LLM response format: %s", json.dumps(data)[:200])
            return None

        lines = text.split("\n", 1)
        action_word = lines[0].strip().upper()
        reason = lines[1].strip() if len(lines) > 1 else ""

        # Validate action
        if action_word not in ("BUY", "SELL", "HOLD"):
            # Try to find the action word somewhere in the first line
            for word in ("BUY", "SELL", "HOLD"):
                if word in action_word:
                    action_word = word
                    break
            else:
                logger.warning("LLM returned unparseable action: %s", lines[0])
                return None

        if action_word == "HOLD":
            return None

        return Signal(
            symbol=self.symbol,
            action=action_word,
            size_usd=100.0,
            confidence=0.6,
            reason=f"[LLM] {reason}",
        )

    def _parse_response(self, text: str) -> Signal | None:
        """Parse LLM response text into a Signal."""
        lines = text.split("\n", 1)
        action_word = lines[0].strip().upper()
        reason = lines[1].strip() if len(lines) > 1 else ""

        if action_word not in ("BUY", "SELL", "HOLD"):
            for word in ("BUY", "SELL", "HOLD"):
                if word in action_word:
                    action_word = word
                    break
            else:
                logger.warning("LLM returned unparseable action: %s", lines[0])
                return None

        if action_word == "HOLD":
            return None

        return Signal(
            symbol=self.symbol,
            action=action_word,
            size_usd=100.0,
            confidence=0.6,
            reason=f"[LLM] {reason}",
        )

    def _build_market_summary(self, hub) -> str | None:
        """Collect current market data into a text summary for the LLM."""
        asset = hub.market.assets.get(self.symbol)
        if asset is None:
            logger.debug("No market data for %s yet", self.symbol)
            return None

        parts = [f"Symbol: {self.symbol}", f"Price: ${asset.price:,.2f}"]

        # 5-minute CVD
        try:
            snap = hub.orderflow.get_snapshot(self.symbol, "5m")
            if snap:
                parts.append(f"5m CVD: ${snap.cvd:,.0f}")
                parts.append(f"5m Buy Vol: ${snap.buy_volume:,.0f}")
                parts.append(f"5m Sell Vol: ${snap.sell_volume:,.0f}")
                parts.append(f"5m OFI: {snap.ofi:+.3f}")
        except Exception:
            pass

        # Funding rate
        parts.append(f"Funding Rate: {asset.funding_rate:.6f}")

        # Recent liquidations
        try:
            liq_stats = hub.liquidations.get_stats(window_minutes=5)
            parts.append(f"5min Liquidations: {liq_stats.get('count', 0)}")
            parts.append(f"5min Liq Volume: ${liq_stats.get('volume_usd', 0):,.0f}")
        except Exception:
            pass

        # Long/short ratio
        try:
            lsr = hub.lsr.get_latest(self.symbol)
            if lsr:
                parts.append(f"L/S Ratio: {lsr.long_short_ratio:.3f}")
        except Exception:
            pass

        return "\n".join(parts)

    def _sync_call(self, hub) -> Signal | None:
        """Synchronous wrapper for threading fallback."""
        import asyncio
        return asyncio.run(self._async_evaluate(hub))
