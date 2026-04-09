# HyperData Terminal — Growth Playbook

Ready-to-post content for every platform. Copy-paste and go.
Post in this order, spaced 2 days apart for maximum impact.

---

## Day 1: r/algotrading (180K+ members)

**Title:**
```
I built a free terminal that streams live crypto data from 5 exchanges simultaneously
```

**Body:**
```
Got tired of having 5 browser tabs open for exchange data. Built HyperData Terminal — a Rich TUI that streams live liquidations, order flow, whale positions, and funding rates from Hyperliquid, Binance, Bybit, OKX, and Deribit simultaneously.

Features:
- Liquidation heatmap (like Coinglass, but free and in your terminal)
- CVD order flow analysis
- Whale position tracking from Hyperliquid
- Pluggable paper trading engine — write your own strategy in ~30 lines of Python
- REST API for building your own tools on top

Just `pip install -e . && hyperdata` — interactive menu lets you pick what to view.

Free, open source, Apache 2.0: https://github.com/Co-Messi/HyperData-Terminal

Happy to hear what features would make this useful for your workflow.
```

---

## Day 3: r/hyperliquid (50K+ members)

**Title:**
```
Open-sourced a terminal dashboard with native Hyperliquid WebSocket — liquidation heatmap, whale tracking, CVD
```

**Body:**
```
Built this for my own trading setup — streams directly from Hyperliquid's WebSocket API.

What it shows:
- Liquidation heatmap — see exactly where positions will get wiped, for every asset
- Whale tracker — largest open positions, their PnL, liquidation distance
- Positions closest to liquidation (the "danger zone")
- CVD order flow from Binance
- Multi-exchange liquidation stream (HL + Binance + Bybit + OKX)

One command: `hyperdata`

No API key needed. Free. https://github.com/Co-Messi/HyperData-Terminal

Built with Python + Rich. Would love feedback from the HL community.
```

---

## Day 5: r/Python (1.2M members)

**Title:**
```
Show r/Python: Built a Rich TUI that streams live crypto market data from 5 exchanges via WebSocket
```

**Body:**
```
Project: HyperData Terminal — a Rich-based TUI that connects to 5 exchange WebSocket feeds simultaneously and renders live market data in your terminal.

Tech stack:
- Python 3.12+
- Rich for TUI rendering (Live, Layout, Panel, Table)
- aiohttp for async WebSocket connections
- SQLite for persistence
- 14 data components running concurrently via asyncio

The interesting engineering challenge was keeping 5 concurrent WebSocket connections alive while rendering at 4fps without flickering. Rich's Live context manager + careful async lifecycle management made it work.

Also includes a pluggable paper trading engine where you subclass a Strategy class and implement one method.

`pip install -e . && hyperdata`

Code: https://github.com/Co-Messi/HyperData-Terminal

Feedback welcome — especially on the Rich TUI patterns. First time building something this complex with Rich.
```

---

## Day 7: Hacker News (Show HN)

**Title:**
```
Show HN: HyperData Terminal – stream live crypto data from 5 exchanges in your terminal
```

**URL:** `https://github.com/Co-Messi/HyperData-Terminal`

**Best time to post:** Tuesday–Thursday, 8–9 AM PST

---

## Twitter/X Post (post same day as Reddit)

```
Just shipped HyperData Terminal — open-source Rich TUI that streams live data from @HyperliquidX @binance @Bybit_Official @OKX @DeribitExchange simultaneously.

Liquidation heatmap, whale tracking, CVD order flow, paper trading — all in your terminal.

Free: github.com/Co-Messi/HyperData-Terminal

[ATTACH GIF/SCREENSHOT]
```

---

## Product Hunt (Day 10+, after Reddit/HN traction)

**Tagline:** `Bloomberg Terminal vibes, $0 price tag`

**Description:**
```
HyperData Terminal streams live crypto market data from 5 exchanges directly into your terminal. Track liquidations, whale positions, order flow, and funding rates — all updating in real-time via WebSocket.

Built for traders who live in the terminal. No browser needed, no subscription, no API keys. Just pip install and go.

Features:
- 6 live dashboards (liquidation heatmap, whale tracker, CVD, market overview, and more)
- Paper trading engine with pluggable strategies
- LLM agent integration (GPT, Ollama, Groq)
- REST API for building custom tools
- Apache 2.0 licensed
```

**Category:** Developer Tools > CLI Tools

---

## Discord Communities

Post in #share-your-project or equivalent channels:

| Server | Angle |
|--------|-------|
| Hyperliquid Discord | "Built a terminal with native HL WebSocket support" |
| Freqtrade Discord | "Complements your bot with live market data terminal" |
| Python Discord | "Rich TUI showcase — 5 concurrent WebSocket feeds" |

---

## GitHub Awesome Lists (submit PRs)

- awesome-trading
- awesome-python
- awesome-systematic-trading (4K stars)
- awesome-rich (if exists)

---

## Automation You Can Set Up

1. **Buffer/Typefully** — schedule all social posts in advance
2. **GitHub star-history** — add star-history.com badge to README after hitting 100 stars
3. **GitHub Discussions** — enable in repo settings for community Q&A
4. **GitHub social preview** — upload the swoosh logo as social preview image (Settings → Social preview)

---

## Key Rules

- **Never post multiple subreddits on the same day** (looks like spam)
- **Respond to EVERY comment** within hours (engagement drives algorithm)
- **Lead with the problem you solved**, not the features
- **GIF/screenshot is mandatory** for Twitter and Reddit
- **Don't say "AI-powered"** on r/algotrading (they hate AI hype). Say "pluggable strategy engine"
