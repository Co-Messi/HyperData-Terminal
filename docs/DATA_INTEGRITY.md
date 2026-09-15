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

- CVD is computed from **both** Hyperliquid and Binance Futures trades, and
  every consumer shows the split: the CVD dashboard and the combined view
  render `CVD: +X [HL +a | BN +b]`, and `GET /v1/orderflow/{symbol}` returns
  `cumulative_cvd_by_venue` plus `venue_coverage` next to the combined
  `cumulative_cvd`. "BTC CVD" is never an unexplained sum.
- Each venue carries its own liveness state (`OrderFlowEngine.venue_freshness()`),
  one of:

  | status | meaning |
  |---|---|
  | `ok` | trades arriving within 30s |
  | `connecting` | (re)connected under 30s ago, no trade yet |
  | `silent` | socket open past the grace period, **zero frames received** — the handshake succeeded but the stream delivers nothing (Binance Futures is geo-blocked in some regions and behaves exactly like this) |
  | `frozen` | frames still arriving but none parse into a trade for 30s (schema change); the per-venue `parse_errors` counter says why |
  | `stale` | had trades, socket open, nothing for 30s |
  | `disconnected` | socket not open (never connected, or between reconnects) |

  A venue that is not `ok` shows its status in place of a number
  (`[HL +a | BN silent]`), flips the hub's `orderflow_engine` feed status to
  `partial`, produces an `order_flow_<venue>: warn` health check (fail only
  when every venue is dead), and turns the header badge to **⚠ PARTIAL**.
  Liveness is stamped only by a successfully **parsed trade** — subscription
  acks and unparseable frames count as frames, not as data.
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
- WebSocket-driven feeds report `connecting` until their first real message
  arrives — a successful `start()` only creates tasks and is never shown as
  `connected`.
- The position scanner is bounded: each cycle re-fetches at most 150
  addresses (round-robin) and serves the rest from a per-address cache whose
  distance-to-liquidation is recomputed from fresh mids. Every position
  carries `scanned_at`; `/v1/whales` and `/v1/positions/danger-zone` carry
  `as_of`; `/v1/health` carries `position_scan.scan_age_seconds`. A scanner
  that has not re-fetched a displayed position within 10 minutes reads
  `stale` (feed status, health check, whales panel) — never `connected`.
- The dashboard header badge reflects this: **✓ LIVE** / **⚠ PARTIAL** /
  **⚠ STALE** / **⚠ DRIFT**.

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
| Order flow freshness (blended) | engine `is_stale()` | some venue delivering trades |
| Order flow freshness per venue | engine `venue_freshness()` | `ok` (warn if this venue is out, fail if all are) |
| Orderbook freshness | engine `is_stale()` | not stale |
| Position scanner freshness | scanner `is_stale()` | last cycle and every displayed position under 10 min old (warn before the first cycle) |
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
exit flushes via an `atexit` handler, and the headless server (`run_api.py`)
installs SIGINT/SIGTERM handlers so `kill <pid>` shuts down cleanly.

Writes never run on the event loop: feed callbacks enqueue rows for a
dedicated writer thread (bounded queue; overflow is dropped and counted as
`dropped_writes` in the DB stats), and reads drain the queue first so a
query immediately after an event still sees it. The startup integrity check
(`PRAGMA quick_check`) is fetched and acted on — anything but `ok` quarantines
the file and starts fresh.

Old rows are pruned hourly (`DataStore.prune`, default `RETENTION_DAYS=7`) and
the WAL is checkpointed, so the DB stays bounded on long-running instances.

## API exposure

The REST/WebSocket API binds to **loopback (`127.0.0.1`) by default**. A
non-loopback bind (`HYPERDATA_API_HOST=0.0.0.0`) is refused at startup unless
either `HYPERDATA_API_KEY` is set — all non-health routes then require
`Authorization: Bearer <key>` or `X-API-Key: <key>` — or
`HYPERDATA_UNSAFE_PUBLIC_API=1` explicitly acknowledges the exposure. CORS is
never wildcard on any bind: browsers receive CORS headers (and may open the
WebSocket) only for origins allowlisted in `HYPERDATA_CORS_ORIGINS`
(comma-separated) — one allowlist drives both surfaces. On a loopback bind
the `Host` header must itself be loopback (DNS-rebinding guard). REST
requests are rate-limited per client IP, and WebSocket clients get bounded
per-client send queues plus inbound message size/rate limits. Numeric query
params are validated (bad values return `400`, not `500`).

## Timestamps

Liquidation, order-flow, and funding-rate records use **exchange event time**
where the payload provides it. Binance's spot price endpoint returns no
timestamp, so spot/basis records use local fetch time by necessity (documented
in code, not silently misleading).
