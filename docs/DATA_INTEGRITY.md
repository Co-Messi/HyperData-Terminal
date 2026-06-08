# Data Integrity

HyperData Terminal is built to be *trustworthy*, not just pretty: it never shows
frozen data as live, it is honest about what is complete vs sampled, and it
continuously verifies itself against external references. This document explains
exactly what that means so you know how far to trust each number.

## Liquidation coverage (it is a sample, not a census)

Liquidation counts/volume are **not** a complete record of every liquidation.
Each exchange is collected differently — `get_stats()` and `/v1/liquidations/stats`
report a `coverage` block plus a per-exchange `method` tag:

| Exchange | Method | What it means |
|---|---|---|
| **OKX** | `confirmed` | Real `liquidation-orders` feed across all SWAP instruments. |
| **Bybit** | `confirmed` | Real `allLiquidation` v5 feed across the tracked symbols (those with a Bybit linear perp). Subscriptions are batched because Bybit caps args per request. |
| **Binance** | `sampled` | The `!forceOrder` stream is **throttled by Binance to ~1 liquidation per symbol per second**. Large cascades are undercounted *at the source* — this cannot be fixed client-side, only disclosed. |
| **Hyperliquid** | `heuristic` | Hyperliquid has **no liquidation feed**. Events are *inferred* from trades ≥ `HL_LIQUIDATION_MIN_USD` (default $10k) and may include ordinary large fills. Carried as `confirmed=False` and shown as estimated (`~` / `?`) in the UI. |

`get_stats()` also returns `confirmed_count` and `heuristic_count` so consumers
can weight accordingly. The HL threshold is a tunable heuristic: raising it cuts
false positives but misses smaller liquidations.

## Order flow / CVD

- CVD is computed from **both** Hyperliquid and Binance trades. The combined
  figure is the default, but per-venue series are available via
  `OrderFlowEngine.get_cumulative_cvd(symbol)` → `{combined, hyperliquid, binance}`
  so "BTC CVD" is never an unexplained sum.
- Trades are de-duplicated per venue (HL `tid`, Binance aggTrade `id`) so a
  reconnect/resubscribe replay cannot double-count into the never-resetting
  cumulative CVD.

## Staleness watchdog (frozen feeds never read as live)

Every WebSocket feed connects with `heartbeat=20`, so a half-open TCP connection
raises and reconnects instead of silently freezing. On top of that:

- Each feed tracks `last_message_at`; `is_stale()` trips after a per-feed
  threshold (order flow 30s, orderbook 15s).
- The orderbook engine force-reconnects a socket that is open but silent past
  2× the threshold; the hub does the same for the Hyperliquid trade socket.
- `OrderBookSnapshot.stale` is recomputed at read time.
- The dashboard header badge reflects this: **✓ LIVE** / **⚠ STALE** / **⚠ DRIFT**.

Liquidations are intentionally *not* aged out — they are sporadic, so a quiet
market is not a broken feed.

## Continuous self-verification

A `DataHealthMonitor` cross-references live hub data against public APIs and runs
freshness/completeness/consistency checks. The hub runs it every 30s (live mode
only) and caches the result; the dashboard badge and `/v1/health` read it.

| Check | Source | Pass condition |
|---|---|---|
| BTC price | Binance spot ticker | within 0.5% of hub |
| BTC long/short ratio | Binance `globalLongShortAccountRatio` | within 20% (warn beyond) |
| Deribit DVOL | hub | present |
| Order flow / orderbook freshness | engine `is_stale()` | not stale |
| Market data freshness | hub refresh stamp | < 30s |
| Funding/consistency | hub | sane bands, funding sign vs L/S agree |

Run it once from the CLI:

```bash
python3 src/verify_data.py --wait 30
```

It prints a PASS/WARN/FAIL report and exits non-zero on any failure (handy for
CI). The same data is available live at `GET /v1/health` under `data_health`,
alongside per-feed `feeds` status.

## Durability

SQLite runs in WAL mode and commits on a time interval
(`COMMIT_INTERVAL_SECONDS`, default 5s) as well as every 50 events, so an
uncatchable crash (SIGKILL/OOM) loses at most a few seconds of events. A graceful
exit flushes via an `atexit` handler.

## Timestamps

Liquidation, order-flow, and funding-rate records use **exchange event time**
where the payload provides it. Binance's spot price endpoint returns no
timestamp, so spot/basis records use local fetch time by necessity (documented
in code, not silently misleading).
