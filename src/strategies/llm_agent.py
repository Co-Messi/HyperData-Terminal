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

import asyncio
import concurrent.futures
import json
import logging
import os
import time
from collections import deque

import aiohttp

from .base import Signal, Strategy

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

    # Budget guardrail: an LLM call per check interval adds up. Configurable
    # via LLM_MAX_EVALS_PER_HOUR; evaluations beyond the budget are skipped.
    DEFAULT_MAX_EVALS_PER_HOUR = 60

    def __init__(self, symbol: str = "BTC") -> None:
        self.symbol = symbol

        # Read config from environment
        self.base_url = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
        self.model = os.environ.get("LLM_MODEL", "llama3")
        self.api_key = os.environ.get("LLM_API_KEY", "")
        try:
            self.max_evals_per_hour = int(
                os.environ.get("LLM_MAX_EVALS_PER_HOUR", self.DEFAULT_MAX_EVALS_PER_HOUR)
            )
        except ValueError:
            self.max_evals_per_hour = self.DEFAULT_MAX_EVALS_PER_HOUR
        self._eval_times: deque[float] = deque(maxlen=max(self.max_evals_per_hour, 1))
        # One long-lived worker thread — not a new executor per evaluation.
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="llm-agent"
        )

    @property
    def name(self) -> str:
        return "llm_agent"

    def _within_budget(self, now: float | None = None) -> bool:
        """Sliding-window cap on LLM calls per hour."""
        now = time.time() if now is None else now
        while self._eval_times and now - self._eval_times[0] > 3600:
            self._eval_times.popleft()
        if len(self._eval_times) >= self.max_evals_per_hour:
            return False
        self._eval_times.append(now)
        return True

    def _refund_eval_slot(self) -> None:
        """Return the most recent budget slot.

        Called when the call failed at the TRANSPORT level (timeout, refused
        connection, HTTP error) — no tokens were consumed, so a flaky
        provider must not exhaust the hourly budget. Parse failures keep
        their slot: the provider did the work and billed for it.
        """
        if self._eval_times:
            self._eval_times.pop()

    async def evaluate(self, hub) -> Signal | None:
        """Build a market summary and ask the LLM for a decision.

        Async: the blocking HTTP call runs in the persistent worker thread
        and is awaited, so a slow LLM response cannot stall the paper
        trader's event loop (other strategies keep evaluating).
        """
        # If no API key and not using a local model, warn and skip
        if not self.api_key and "localhost" not in self.base_url:
            logger.warning(
                "LLM_API_KEY not set and not using localhost — skipping LLM agent. "
                "Set LLM_BASE_URL, LLM_MODEL, and LLM_API_KEY in your .env file."
            )
            return None

        if not self._within_budget():
            logger.warning(
                "LLM eval budget exhausted (%d/hour) — skipping evaluation",
                self.max_evals_per_hour,
            )
            return None

        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(self._pool, self._sync_evaluate, hub),
                timeout=20,
            )
        except asyncio.TimeoutError:
            self._refund_eval_slot()
            logger.warning("LLM evaluation timed out after 20s")
            return None
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
            # Transport-level failure: no tokens consumed — give the budget
            # slot back so a down provider can't burn the hourly allowance.
            self._refund_eval_slot()
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
        except (KeyError, IndexError, TypeError, AttributeError):
            logger.warning("Unexpected LLM response format: %s", json.dumps(data)[:200])
            return None

        return self._parse_response(text)

    def _parse_response(self, text: str) -> Signal | None:
        """Parse LLM response text into a Signal — deterministic, reject-on-ambiguous.

        The first NON-EMPTY line must be exactly BUY, SELL, or HOLD
        (case-insensitive, surrounding punctuation tolerated; leading blank
        lines are ignored). Substring matching is deliberately NOT done:
        "I would not BUY here" must never resolve to a BUY.
        """
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            logger.warning("LLM returned empty response")
            return None
        action_word = lines[0].upper().strip(".!:*# ")
        reason = " ".join(lines[1:]) if len(lines) > 1 else ""

        if action_word not in ("BUY", "SELL", "HOLD"):
            logger.warning("LLM returned ambiguous action, rejecting: %r", lines[0][:100])
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
            # get_stats returns total_count / total_volume_usd (not count/volume_usd).
            parts.append(f"5min Liquidations: {liq_stats.get('total_count', 0)}")
            parts.append(f"5min Liq Volume: ${liq_stats.get('total_volume_usd', 0):,.0f}")
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
        return asyncio.run(self._async_evaluate(hub))
