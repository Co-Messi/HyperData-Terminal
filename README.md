# HyperData Terminal

<img width="1432" height="776" alt="image" src="https://github.com/user-attachments/assets/74fd120a-5468-436f-9d51-0d5a6e7d35a3" />
<img width="1428" height="781" alt="image" src="https://github.com/user-attachments/assets/51d800fb-c484-48cd-8a53-27cbd37662d0" />


Open-source crypto market data terminal with real-time dashboards, REST API, and a pluggable paper trading engine.

Live data from Hyperliquid, Binance, Bybit, OKX, and Deribit — streamed via WebSocket, rendered in your terminal.

## Features

- **5 Terminal Dashboards** — Liquidation watch, liquidation stream, order flow (CVD), market overview, whale tracker
- **14 Data Components** — Liquidations from 4 exchanges, CVD orderflow, whale positions, funding rates, open interest, orderbook depth, DVOL implied volatility, long/short ratios, spot prices, smart money signals
- **REST API** — 17+ endpoints for market data, WebSocket streaming for real-time events
- **Paper Trading** — Pluggable strategy engine with fake money on real prices. Write your own strategy in ~30 lines.
- **LLM Agent** — Optional AI-powered trading decisions via any OpenAI-compatible API (GPT, Ollama, Groq, etc.)

## Quick Start

```bash
# Clone and install
git clone https://github.com/Co-Messi/HyperData-Terminal.git
cd HyperData-Terminal
pip install -e .

# Run
hyperdata
```

That's it. Live data from 5 exchanges, rendered in your terminal.

## Dashboards

```bash
hyperdata                  # All dashboards (live data)
hyperdata -d liq           # Liquidation watch — positions near liquidation
hyperdata -d stream        # Liquidation stream — real-time feed
hyperdata -d cvd           # Order flow — CVD, buy/sell volume
hyperdata -d market        # Market overview — prices, funding, OI
hyperdata -d whale         # Whale tracker — largest Hyperliquid positions
hyperdata --demo           # Demo mode (synthetic data)
hyperdata --list           # Show available dashboards
```

## REST API

```bash
# Start the API server
python run_api.py --port 8420

# Health check
curl http://localhost:8420/v1/health

# Market data
curl http://localhost:8420/v1/market
curl http://localhost:8420/v1/market/BTC
curl http://localhost:8420/v1/liquidations
curl http://localhost:8420/v1/orderflow?symbol=BTC&period=1h
curl http://localhost:8420/v1/funding-rates
curl http://localhost:8420/v1/whales
curl http://localhost:8420/v1/danger-zone
curl http://localhost:8420/v1/smart-money/signals

# WebSocket streaming
wscat -c ws://localhost:8420/v1/ws
```

## Paper Trading

HyperData Terminal includes a paper trading engine that uses **real market data** with **fake money**. Write your own strategy, plug it in, and watch it trade.

### Write Your Own Strategy

Create a file in `src/strategies/examples/`:

```python
from src.strategies.base import Strategy, Signal

class MyStrategy(Strategy):
    name = "my_strategy"

    def evaluate(self, hub) -> Signal | None:
        # Access any live market data via hub:
        #   hub.orderflow    — CVD, buy/sell volume
        #   hub.market       — prices, OI, funding
        #   hub.liquidations — recent liquidation events
        #   hub.funding      — funding rates by exchange
        #   hub.lsr          — long/short ratios
        #   hub.positions    — whale positions (Hyperliquid)
        #   hub.spot         — spot prices
        #   hub.deribit      — DVOL implied volatility

        snap = hub.orderflow.get_snapshot("BTC", "5m")
        if not snap:
            return None

        cvd = snap.buy_volume - snap.sell_volume
        if cvd > 100_000:
            return Signal("BTC", "BUY", size_usd=100, confidence=0.7, reason="Strong buy pressure")
        elif cvd < -100_000:
            return Signal("BTC", "SELL", size_usd=100, confidence=0.7, reason="Strong sell pressure")

        return None
```

### Included Examples

| Strategy | Data Source | Logic |
|----------|-----------|-------|
| `CVDMomentum` | `hub.orderflow` | Buy on positive CVD, sell on negative |
| `FundingRateArb` | `hub.funding` | Short when funding high, long when negative |
| `LiquidationCascade` | `hub.liquidations` | Buy the dip after large liquidation events |
| `WhaleFollow` | `hub.positions` | Follow whale position changes |

### LLM Agent

The LLM agent sends market data to any OpenAI-compatible API and asks for trading decisions:

```bash
# Set up in .env
LLM_BASE_URL=https://api.openai.com/v1    # or http://localhost:11434/v1 for Ollama
LLM_MODEL=gpt-4o                           # or llama3, mixtral, etc.
LLM_API_KEY=sk-...
```

The agent automatically constructs a market summary (price, CVD, funding rate, recent liquidations) and asks the model whether to buy, sell, or hold. You can customize the prompt in `src/strategies/llm_agent.py`.

## Configuration

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|----------|----------|-------------|
| `LLM_BASE_URL` | For LLM agent | OpenAI-compatible API endpoint |
| `LLM_MODEL` | For LLM agent | Model name (gpt-4o, llama3, etc.) |
| `LLM_API_KEY` | For LLM agent | API key |
| `BINANCE_API_KEY` | No | Enhances market data quality |
| `TELEGRAM_BOT_TOKEN` | No | For alert notifications |
| `DISCORD_WEBHOOK_URL` | No | For alert notifications |

## Data Sources

All data is fetched live from public exchange APIs:

| Source | Data | Connection |
|--------|------|------------|
| Hyperliquid | Positions, liquidations, funding | WebSocket + REST |
| Binance | Trades, liquidations, orderbook | WebSocket |
| Bybit | Liquidations | WebSocket |
| OKX | Liquidations | WebSocket |
| Deribit | DVOL implied volatility | REST |

No API keys required for basic functionality. All exchanges provide public WebSocket feeds.

## Architecture

```
Exchanges (Hyperliquid, Binance, Bybit, OKX, Deribit)
    |  WebSocket + REST
    v
HyperDataHub (14 data components)
    |
    +---> Terminal Dashboards (Rich TUI)
    +---> REST API + WebSocket (/v1/*)
    +---> Paper Trading Engine (pluggable strategies)
```

## Tests

```bash
# Run all tests
python -m pytest tests/ -v

# Skip live exchange tests
python -m pytest tests/ -v --ignore=tests/test_position_scanner.py

# Single test
python -m pytest tests/test_orderflow.py -v
```

## Contributing

1. Fork the repo
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Run tests (`python -m pytest tests/ -v`)
5. Run linter (`ruff check src/`)
6. Commit and push
7. Open a Pull Request

## License

Apache License 2.0 — see [LICENSE](LICENSE) for details.
