<div align="center">

# HyperData Terminal

**Hyperliquid whales, liquidation heatmaps and order flow from five exchanges, live in your terminal.**<br>
Free and open source. Public feeds only: no API keys, no account, nothing to sign.

[![PyPI](https://img.shields.io/pypi/v/hyperdata-terminal.svg)](https://pypi.org/project/hyperdata-terminal/)
[![CI](https://github.com/Co-Messi/HyperData-Terminal/actions/workflows/ci.yml/badge.svg)](https://github.com/Co-Messi/HyperData-Terminal/actions)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

<img src="https://raw.githubusercontent.com/Co-Messi/HyperData-Terminal/main/assets/demo.gif" width="900" alt="hyperdata all: liquidation watch, confirmed liquidations across exchanges, order flow with warmup labels, the HLP vault, market intelligence, smart money and the largest Hyperliquid positions, updating live">

```bash
pipx install hyperdata-terminal
hyperdata
```

</div>

## Why people run it

- **See where leverage gets wiped before it happens.** A liquidation heatmap and a "closest to liquidation" list built from thousands of live Hyperliquid wallets, for every listed asset. The kind of view paid dashboards charge for.
- **Watch the biggest books on Hyperliquid.** Size, entry, liquidation price, PnL and leverage of the largest open positions, plus Hyperliquid's own market maker (the HLP vault: AUM, net delta, gross exposure) and the liquidations it absorbs.
- **Know when the data is wrong.** Every feed is cross-checked and labelled LIVE, PARTIAL, STALE or DRIFT. Confirmed liquidations are never mixed with guesses. Order flow says when a timeframe is still warming up instead of printing a fake signal. See [docs/DATA_INTEGRITY.md](docs/DATA_INTEGRITY.md).
- **Plug it into your AI agent.** `hyperdata mcp` gives Claude, Cursor or any MCP client ten read only tools over the same live data.
- **Build on it.** A local REST and WebSocket API, and a paper trading engine that runs your strategy file on live prices.

## Install

```bash
pipx install hyperdata-terminal         # or: uv tool install hyperdata-terminal
uvx hyperdata-terminal                  # or run once without installing
pip install hyperdata-terminal          # into the current environment
pipx install git+https://github.com/Co-Messi/HyperData-Terminal   # latest main
```

Requires Python 3.12 or newer. Want the MCP server too? `pipx install "hyperdata-terminal[mcp]"`.

## Use

| Command | What you get |
|---|---|
| `hyperdata` | Menu of every dashboard |
| `hyperdata all` | Everything at once (the view in the GIF) |
| `hyperdata heatmap --symbol ETH` | Price levels where positions get liquidated |
| `hyperdata whales` | Largest open Hyperliquid positions |
| `hyperdata liq` | BTC positions closest to liquidation |
| `hyperdata stream` | Liquidations across Hyperliquid, Binance, Bybit and OKX |
| `hyperdata cvd --symbol SOL` | Cumulative volume delta and order flow signals |
| `hyperdata market` | Top Hyperliquid perps and funding extremes |
| `hyperdata api --port 8420` | Headless REST and WebSocket API |
| `hyperdata paper -s ./my_strategy.py` | Paper trade a strategy on live data |
| `hyperdata mcp` | MCP server for AI agents (stdio) |
| `hyperdata verify` | One shot data integrity report, exits 1 on failure |

Add `--no-boot` to skip the start animation. `Ctrl+C` returns to the menu or exits. Positions take about 30 seconds to load on the first scan.

## Dashboards

| Dashboard | What it shows |
|---|---|
| **Liquidation Watch** | Positions closest to liquidation, bucketed by distance (under 1%, 2%, 5%) with long and short totals. |
| **Liquidation Heatmap** | USD that gets liquidated at each price level around the current price: longs below, shorts above. Any listed asset. |
| **Whale Tracker** | The largest open Hyperliquid positions with entry, liquidation price, distance, PnL and leverage. |
| **Liquidation Stream** | Confirmed liquidations from OKX, Bybit, Binance and Hyperliquid, with per exchange coverage notes. |
| **CVD Order Flow** | Buy versus sell aggression for 1m to 4h windows, combined from Hyperliquid and Binance trades and always shown per venue. |
| **Market Overview** | The top 20 Hyperliquid perps by volume (price, 24h change, funding, open interest, volume) and the 10 most extreme funding rates, hourly and annualized. |
| **All** | Everything above plus the HLP vault, smart money ranking, Deribit implied volatility, spot basis and long/short ratios. |

## MCP server for AI agents

```bash
pipx install "hyperdata-terminal[mcp]"
claude mcp add hyperdata -- hyperdata mcp          # Claude Code
```

For Claude Desktop, Cursor and other clients, add it to the MCP config:

```json
{
  "mcpServers": {
    "hyperdata": { "command": "hyperdata", "args": ["mcp"] }
  }
}
```

Then ask things like *"Where are the biggest BTC liquidation clusters right now?"*, *"Which Hyperliquid whales are within 2% of liquidation?"* or *"Is anything showing extreme funding, and does order flow agree?"*

| Tool | Returns |
|---|---|
| `get_market_overview` | Perps ranked by open interest, volume, move or funding |
| `get_asset` | One asset: funding on every venue, long/short ratio, basis, order flow, positions near liquidation |
| `get_liquidation_heatmap` | Liquidation USD per price level, largest clusters |
| `get_positions_near_liquidation` | Tracked positions within N% of liquidation |
| `get_whale_positions` | Largest open positions |
| `get_liquidations` | Confirmed liquidations with coverage notes; estimated ones only on request |
| `get_order_flow` | CVD and imbalance per timeframe, per venue |
| `get_hlp_vault` | HLP AUM, net delta and z score, top positions, absorbed liquidations |
| `get_funding_extremes` | Assets with extreme funding, compared across venues |
| `get_data_health` | The self verification report |

Every result carries a `meta` block with uptime and warmup warnings, so an agent can tell "not loaded yet" from "nothing happened". The server is read only: no keys, no orders, no account access.

## REST API

```bash
hyperdata api --port 8420
curl http://localhost:8420/v1/liquidations/stats?minutes=240
curl http://localhost:8420/v1/positions/danger-zone
```

<details>
<summary>All endpoints</summary>

| Endpoint | Description |
|---|---|
| `GET /v1/live` | Minimal liveness probe (always unauthenticated) |
| `GET /v1/health` | Status, per feed freshness and the data integrity report. `status` is `initializing` until the first self check, then `ok`, `warn` or `degraded`, never `ok` for a terminal that is not. |
| `GET /v1/market` | All assets: prices, OI, funding |
| `GET /v1/market/{symbol}` | Single asset detail |
| `GET /v1/liquidations` | Recent confirmed liquidation events; `?include_estimated=true` adds Hyperliquid large prints (each row has `confirmed`) |
| `GET /v1/liquidations/stats` | Confirmed aggregates over `?minutes=` (max 1440) with `window_coverage`, `covered_since` and `truncated`; `?include_estimated=true` adds large prints |
| `GET /v1/orderflow/{symbol}` | CVD per timeframe with window `coverage`, per venue CVD |
| `GET /v1/funding-rates` | Funding across exchanges |
| `GET /v1/funding-rates/{symbol}` | Single asset funding |
| `GET /v1/long-short-ratio` | Long/short account ratio |
| `GET /v1/basis` | Perp versus spot basis |
| `GET /v1/deribit/iv` | DVOL implied volatility |
| `GET /v1/orderbook/{symbol}` | Orderbook snapshot |
| `GET /v1/whales` | Top whale positions |
| `GET /v1/positions/danger-zone` | Positions within `?threshold=` percent of Hyperliquid's own liquidation price (none estimated, none already crossed) |
| `GET /v1/public/metrics` | Server metrics and component health |
| `WS /v1/ws` | Event stream: liquidations, trades, signals |

</details>

> **Local only by default.** The API binds to `127.0.0.1` and serves wallet level positions and order flow. A non loopback bind (`HYPERDATA_API_HOST=0.0.0.0`) is refused unless you set `HYPERDATA_API_KEY` (then every non health route needs `Authorization: Bearer <key>` or `X-API-Key`) or explicitly accept the risk with `HYPERDATA_UNSAFE_PUBLIC_API=1`. Browsers get no cross origin access unless you list the origin in `HYPERDATA_CORS_ORIGINS`, and on loopback the `Host` header must be loopback too (DNS rebinding guard).

## Paper trading

A strategy is one class with one method, in a file anywhere on disk:

```python
# my_strategy.py
from hyperdata_terminal.strategies import Signal, Strategy

class CvdBreakout(Strategy):
    name = "cvd_breakout"

    def evaluate(self, hub) -> Signal | None:
        snap = hub.orderflow.get_snapshot("BTC", "5m")
        if snap.warming_up:          # window not filled yet
            return None
        if snap.cvd > 100_000:
            return Signal("BTC", "BUY", size_usd=100, reason="buyers in control")
        if snap.cvd < -100_000:
            return Signal("BTC", "SELL", size_usd=100, reason="sellers in control")
        return None
```

```bash
hyperdata paper -s ./my_strategy.py -s funding_rate_arb --interval 30
```

Helper modules next to your strategy file can be imported, at the top of the file. Trades print as they happen and are logged to SQLite; `Ctrl+C` prints the portfolio.

How fills work, so the paper result means something:

- Every strategy has its own book. Two strategies on BTC never close each other's positions, and each trade is logged under the strategy that made it.
- A buy fills at the Hyperliquid price plus 2 bps of slippage and a sell at it minus 2 bps, and every fill pays a 4.5 bps taker fee (Hyperliquid's base rate). Change them with `--slippage-bps` and `--fee-bps`.
- Positions are 1x: the full size is posted from the balance. Adding to a position averages the entry by coins. A loss larger than what was posted takes the balance below zero; it is not hidden.
- Funding is not paid or collected, so `funding_rate_arb` shows only its price moves. Nothing is ever liquidated.
- An opposite signal closes the position; `--reverse` also opens the other side.
- Each run starts flat, and positions still open at exit are closed at the current price (logged as `session end`), so the trade log always adds up. Built in strategies: `cvd_momentum`, `funding_rate_arb`, `liquidation_cascade`, `whale_follow`, and `llm_agent`, which asks any OpenAI compatible model (OpenAI, Ollama, LM Studio, Groq, Together) for a decision. The full list of what `hub` exposes is in [`strategies/base.py`](hyperdata_terminal/strategies/base.py).

## Data sources

| Source | Data | Connection |
|---|---|---|
| **Hyperliquid** | Trades, positions, funding, OI, HLP vault, absorbed liquidations | WebSocket + REST |
| **Binance** | Futures trades for CVD, liquidations (throttled at source), orderbook, spot, long/short | WebSocket + REST |
| **Bybit** | Liquidations, long/short fallback | WebSocket + REST |
| **OKX** | Liquidations, spot and long/short fallback, price cross check | WebSocket + REST |
| **Coinbase** | Spot fallback for basis | REST |
| **Deribit** | DVOL implied volatility | REST |

Coverage is honest, not a census. Hyperliquid has no public liquidation feed, so its confirmed liquidations are the ones an HLP vault took the other side of (a few an hour, counted once per liquidation even when two vaults filled it); trades of $10K or more are shown separately as estimated large prints and never counted as liquidations. Binance futures REST is blocked in some regions (the US among them): there spot, long/short and the price cross check fall back to Coinbase, Bybit or OKX, each value naming its source, and a Binance stream that delivers nothing reads `silent` instead of passing for live. Details: [docs/DATA_INTEGRITY.md](docs/DATA_INTEGRITY.md).

## Configuration

Everything works without configuration. Optional settings go in a `.env` in the directory you run from, or in `.env` inside the data directory ([`.env.example`](.env.example)). Settings that change network exposure or where data and keys go (`HYPERDATA_API_HOST`, `HYPERDATA_API_PORT`, `HYPERDATA_API_KEY`, `HYPERDATA_UNSAFE_PUBLIC_API`, `HYPERDATA_CORS_ORIGINS`, `HYPERDATA_DATA_DIR`, `LLM_BASE_URL`) are read only from the environment or the data directory's `.env`: a `.env` in a cloned repository cannot open the API to the network or send your `LLM_API_KEY` elsewhere, and `hyperdata` says when it ignored one.

| Variable | Used for |
|---|---|
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | The `llm_agent` strategy. The key is sent only over https or to a local server (localhost, 127.0.0.1, ::1) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DISCORD_WEBHOOK_URL` | Liquidation cascade alerts: one message when confirmed liquidations reach $10M in 5 minutes or $50M in an hour (`HYPERDATA_ALERT_CASCADE_5M_USD`, `HYPERDATA_ALERT_CASCADE_1H_USD`), at most once per 15 or 60 minutes. `hyperdata alerts --test` checks the setup. |
| `HYPERDATA_API_HOST`, `HYPERDATA_API_KEY`, `HYPERDATA_CORS_ORIGINS` | API exposure (see the security note above) |
| `HYPERDATA_DATA_DIR` | Where SQLite stores and logs live. Default: the per user data directory (`~/Library/Application Support/hyperdata`, `~/.local/share/hyperdata`, `%LOCALAPPDATA%\hyperdata`), or `./data` in an existing source checkout |

## How it works

```
Hyperliquid  Binance  Bybit  OKX  Coinbase  Deribit      (public WebSocket + REST)
        \       |       |     |      |       /
         HyperDataHub: 14 async components, one event loop, SQLite writer thread
        /        |            |             \
  dashboards   REST + WS    MCP server    paper trading
   (Rich)       (/v1/*)     (stdio)       (your strategies)
```

The hub owns every data component and its lifecycle; dashboards, API, MCP server and strategies all read the same in memory state.

## Development

```bash
git clone https://github.com/Co-Messi/HyperData-Terminal.git
cd HyperData-Terminal
pip install -e ".[mcp]" pytest pytest-asyncio ruff
python -m pytest tests -q          # live exchange tests need --live
ruff check hyperdata_terminal tests
```

The demo GIF is reproducible: `vhs assets/demo.tape`.

## Contributing

Issues and pull requests are welcome. Good places to start: a new exchange adapter (Bitget, Gate, Hyperliquid spot), a new dashboard over data the hub already has, or a new MCP tool. Please run the tests and `ruff` before opening a PR.

## License

Apache License 2.0, see [LICENSE](LICENSE). Market data belongs to the exchanges that publish it; this project only reads public endpoints. Nothing here is financial advice.
