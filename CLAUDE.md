# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Commands

```bash
# Dev install (editable, with the MCP extra)
pip install -e ".[mcp]" pytest pytest-asyncio ruff

# Run tests (network calls are mocked; tests marked `live` need --live)
python -m pytest tests/ -q
python -m pytest tests/ -v --live

# Lint
ruff check hyperdata_terminal tests

# Terminal dashboards (live data from 5 exchanges)
hyperdata                      # menu
hyperdata all --no-boot        # one dashboard directly: liq, stream, heatmap, cvd, market, whales, all

# Headless API server
hyperdata api --port 8420

# Paper trading, MCP server, data-integrity report
hyperdata paper -s cvd_momentum -s ./my_strategy.py
hyperdata mcp
hyperdata verify --wait 30

# Regenerate the README demo GIF (needs vhs)
vhs assets/demo.tape
```

`run_dashboard.py` and `run_api.py` at the repo root are thin shims over the CLI for old clones.

See `docs/DATA_INTEGRITY.md` for coverage caveats (confirmed vs sampled
liquidations, Hyperliquid large prints, regional fallbacks, CVD warmup), the
staleness watchdog, and the health checks.

## Architecture

The package is `hyperdata_terminal/` (PyPI: `hyperdata-terminal`; the PyPI name
`hyperdata` belongs to someone else). Entry point: `hyperdata_terminal/cli.py`.

**HyperDataHub** (`hyperdata_terminal/data_layer/hub.py`) is the central orchestrator. It owns all data components and manages their async lifecycles via `start()`/`stop()`.

```
Exchanges (Hyperliquid, Binance, Bybit, OKX, Coinbase, Deribit)
    |  WebSocket + REST
    v
HyperDataHub (14 data components)
    |
    +---> Terminal dashboards (Rich TUI)          terminal.py, dashboards/
    +---> REST API + WebSocket (/v1/*)            api_server.py
    +---> MCP server (stdio)                      mcp_server.py
    +---> Paper trading (pluggable strategies)    strategies/
```

**Data components** (all in `hyperdata_terminal/data_layer/`): each is a dataclass for the data model plus a collector/engine class with `start()`/`stop()`; the hub wires them together.

Components: liquidation_feed (4 exchanges), orderflow_engine (CVD), position_scanner, market_data, funding_rates, long_short_ratio, orderbook, spot_prices, deribit (DVOL), smart_money, hlp_tracker, alerts, persistence (SQLite), address_store.

**API Server** (`api_server.py`): aiohttp.web embedded in the hub's event loop. All endpoints under `/v1/`. WebSocket at `/v1/ws` streams events. Loopback by default; no cross-origin access unless `HYPERDATA_CORS_ORIGINS` lists the origin.

**MCP Server** (`mcp_server.py`): `HubTools` holds the tool logic as plain methods over a hub (unit-tested with a fake hub); `build_server()` wires them into `mcp.server.mcpserver.MCPServer` (mcp 2.x) with the hub started in the lifespan. stdout is the protocol: nothing in the data layer may print. Tool wrappers must stay `async def`: the SDK runs plain `def` tools on a worker thread, which races the hub's event loop.

**Paper Trading** (`strategies/`): subclass `Strategy`, implement `evaluate(hub)`, return a `Signal`. `strategies/loader.py` resolves `--strategy` specs (built-in name, `.py` path, `module:Class`).

**Persistence** (`data_layer/persistence.py`): SQLite in WAL mode at `<data dir>/hyperdata.db`, written by a dedicated writer thread.

**Data dir** (`paths.py`): `HYPERDATA_DATA_DIR`, else an existing `<checkout>/data`, else the per-user data dir. Never site-packages. Resolved once at import; tests set the env var in `conftest.py` before importing the package.

## Import Conventions

- Everything, including tests: `from hyperdata_terminal.data_layer.X import Y`
- No `sys.path` manipulation anywhere.

## Key Patterns

- **Persistent aiohttp sessions**: Components create `aiohttp.ClientSession()` in `start()`, close in `stop()`. Never create sessions per-request.
- **WebSocket broadcast**: `_broadcast()` enqueues per client; each client has a bounded queue drained by its own writer task.
- **Live by default**: every CLI entry point runs `HyperDataHub(demo=False)`. A `demo=True` path (synthetic generators in `hub_demo.py`) exists only as an offline dev tool; the health monitor is disabled under demo.
- **Confirmed vs estimated**: Hyperliquid large prints are `LiquidationEvent(confirmed=False)`. Anything that totals liquidations for display, alerts or strategies uses `get_stats(include_estimated=False)`.
- **HLP**: AUM and session PnL from the parent's `vaultDetails` (never PnL from the AUM change), positions netted per coin, gross exposure per vault, fills via `userFillsByTime` watermarks with forward paging, absorptions grouped by (transaction hash, coin, side) and upserted by that key (one transaction can carry several coins).
- **Estimated liquidations live in `LiquidationFeed.estimated_events`**, apart from confirmed `events`, so they can never evict confirmed ones.
- **Source fallbacks**: spot (Binance, Coinbase, OKX) and L/S (Binance, Bybit, OKX) record `source` on every snapshot and skip a failing source for 10 minutes if it is geoblocked (401/403/451), 30 seconds otherwise (`utils.helpers.source_cooldown_seconds`).
- **Symbol normalization**: `normalize_symbol()` strips USDT/USD/PERP suffixes.

## External Data Sources

- **Hyperliquid**: Positions, trades, funding, OI, HLP vault (parent + child vaults). WebSocket + REST. No key.
- **Binance**: Futures trades, liquidations, orderbook, spot, L/S. WebSocket + REST. No key. Returns 451 in some regions.
- **Bybit**: Liquidations; L/S fallback. WebSocket + REST. No key. Returns 403 in some regions.
- **OKX**: Liquidations; spot, L/S and price cross-check fallback. WebSocket + REST. No key.
- **Coinbase**: Spot fallback for basis. REST. No key.
- **Deribit**: DVOL implied volatility. REST. No key.
