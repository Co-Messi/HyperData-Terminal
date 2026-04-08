# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Commands

```bash
# Run tests
python -m pytest tests/ -v --ignore=tests/test_position_scanner.py

# Run with live exchange connections
python -m pytest tests/ -v --live

# Lint
ruff check src/

# Start terminal dashboard (live data)
python3 run_dashboard.py

# Start in demo mode (synthetic data)
python3 run_dashboard.py --demo

# Start headless API server
python3 run_api.py --port 8420
```

## Architecture

**HyperDataHub** (`src/data_layer/hub.py`) is the central orchestrator. It owns all data components and manages their async lifecycles via `start()`/`stop()`.

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

**Data components** (all in `src/data_layer/`): Each follows the same pattern — a dataclass for the data model, a collector/engine class with `start()`/`stop()`, and the hub wires them together.

Components: liquidation_feed (4 exchanges), orderflow_engine (CVD), position_scanner, market_data, funding_rates, long_short_ratio, orderbook, spot_prices, deribit (DVOL), smart_money, hlp_tracker, alerts, persistence (SQLite), address_store.

**API Server** (`src/api_server.py`): aiohttp.web embedded in the hub's event loop. All endpoints under `/v1/`. WebSocket at `/v1/ws` streams events. CORS enabled.

**Paper Trading** (`src/strategies/`): Pluggable strategy engine. Subclass `Strategy`, implement `evaluate(hub)`, return a `Signal`. The `PaperTrader` runs strategies on real market data with fake money.

**Persistence** (`src/data_layer/persistence.py`): SQLite in WAL mode at `data/hyperdata.db`. Batch commits every 50 events. Thread-safe via `threading.Lock`.

## Import Conventions

- Hub imports: `from src.data_layer.X import Y`
- Test imports: `from data_layer.X import Y` (conftest.py adds `src/` to sys.path)
- API server: `from src.X import Y`

## Key Patterns

- **Persistent aiohttp sessions**: Components create `aiohttp.ClientSession()` in `start()`, close in `stop()`. Never create sessions per-request.
- **WebSocket broadcast**: `_broadcast()` iterates client list copy, uses `_safe_send()` with 2-second timeout. Dead clients removed immediately.
- **Demo mode**: When `hub.demo=True`, real exchange connections are skipped. Demo generators produce synthetic data.
- **Symbol normalization**: `normalize_symbol()` strips USDT/USD/PERP suffixes.

## External Data Sources

- **Hyperliquid**: Positions, liquidations, funding. WebSocket + REST. No key.
- **Binance**: Trades, liquidations, orderbook. WebSocket. No key.
- **Bybit**: Liquidations. WebSocket. No key.
- **OKX**: Liquidations. WebSocket. No key.
- **Deribit**: DVOL implied volatility. REST. No key.
