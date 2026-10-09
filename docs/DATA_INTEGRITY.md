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
| **Hyperliquid** | `partial` | Hyperliquid has **no public liquidation feed**. *Confirmed* events are liquidations an HLP vault took the other side of: Hyperliquid marks those fills with a `liquidation` object (`method: market` or `backstop`), and the HLP tracker polls every child vault's fills (~2 min delay). In practice most are `market` fills by Strategy A/B when HLP happened to be the book counterparty (several per hour in a live check), grouped by transaction hash so a liquidation both vaults filled counts once; Liquidator backstops are rarer. Liquidations filled by other traders are invisible. |

Separately, Hyperliquid trades ≥ `HL_LIQUIDATION_MIN_USD` (default $10k) are
reported as **large prints**: `confirmed=False`, shown with `~`. In a test
run 44 of 50 of these were ordinary trades, so the terminal **never adds them
to liquidation totals**: every dashboard total, the alert digest, the
`LiquidationCascade` strategy and the LLM agent prompt use
`get_stats(include_estimated=False)`, and the panels show large prints on
their own labelled line. `get_stats()` defaults to `include_estimated=True`
for API compatibility (`/v1/liquidations/stats?include_estimated=false` for
confirmed only) and always reports `confirmed_*` and `heuristic_*` separately.

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
  | `ok` | trades arriving within 30s (Hyperliquid: and every expected shard socket up) |
  | `partial` | Hyperliquid only: trades arriving, but at least one of its subscription shards is dark — flapping (its last two sockets died within 30s of connecting) or without a socket past the grace — so that shard's symbols are missing from the CVD; `shards_dark` / `dark_symbols` say which |
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
- Hyperliquid drops a WebSocket (close code 1006, no error message) when it
  receives a `trades` subscription for a coin it does not list. Subscriptions
  are therefore filtered against the live `meta` universe (refreshed hourly);
  an unlisted default symbol is skipped with one WARNING naming it and the
  alias Hyperliquid uses (`PEPE` → `kPEPE`). Symbols are also spread across
  sockets of at most 8 subscriptions so a coin delisted between refreshes
  takes down one shard, not the venue. A socket the server closes shortly
  after connecting is retried with backoff (1s doubling to 15s), never in a
  tight loop. Shard liveness is tracked individually: `/v1/health` →
  `orderflow_venues.hyperliquid` carries `sockets_open` vs
  `sockets_expected`, `shards_dark`, `shards_idle` (no listed symbols) and
  `dark_symbols`, and a venue with any dark shard reads `partial` rather
  than `ok` — one live shard can no longer hide six dead ones. Every shard
  connect is still counted in `connects`.

### Warmup: a 4h window that holds one minute is not a 4h signal

Right after start every timeframe contains the same few minutes of trades.
Each `CVDSnapshot` carries `coverage` (the share of its window that data
actually spans, 0..1). Dashboards show `warm NN%` instead of a signal for a
window under 95% covered, the aggregate reads `WARMING_UP` until the 1h
window is half covered (`OrderFlowEngine.display_signal`), and
`/v1/orderflow/{symbol}` returns `coverage` per timeframe. The raw
`get_multi_timeframe_signal()` used by strategies is unchanged.

## Regional fallbacks (Binance is blocked in some regions)

Binance futures answers HTTP 451 in several regions (the US among them) and
Bybit 403s in others. Sources that would otherwise go dark fall back in
order, and every value says which venue it came from:

| Data | Order | Field |
|---|---|---|
| Spot price (basis) | Binance (USDT) → Coinbase (USD) → OKX (USDT) | `SpotPriceSnapshot.source`, `hub.spot.active_source` |
| Long/short account ratio | Binance → Bybit → OKX | `LongShortSnapshot.source`, `hub.lsr.active_source` |
| BTC price cross-check | Binance perp mark → OKX perp mark | named in the health check detail |

A source that fails is skipped for 10 minutes instead of being retried on
every poll. A fallback L/S ratio counts a different venue's accounts, so the
health monitor does not cross-check it against Binance, and the
consistency check of funding against L/S (Binance funding against Binance accounts)
is skipped. Binance trade data for CVD has no substitute: it shows
`silent`, as described above.

## HLP vault

HLP is a parent vault holding idle USDC plus child vaults (Strategy A/B/X and
several Liquidators) that hold the positions.

- **AUM** is the latest point of the parent's `vaultDetails` portfolio (what
  Hyperliquid's UI shows), refreshed every 5 minutes along with the child
  list; a failed refresh is retried on the next 30s pass. Summing
  `clearinghouseState` instead misses Strategy X, which shows neither equity
  nor positions there (about $100M of the ~$180M at the time of writing).
  That sum is only a fallback, used when no reading is younger than 15
  minutes, and it is labelled: `(partial)` in the panel, `aum_source` and
  `aum_is_partial` in MCP, `aum_source` in `hlp_snapshots`.
- **Session PnL** is the change in Hyperliquid's own cumulative PnL series
  (`allTime.pnlHistory`) since the first reading this session. It is never
  the change in AUM: deposits and withdrawals move AUM by hundreds of
  thousands of dollars in minutes while PnL moves by hundreds. Until a
  reading exists the panel shows `--` and MCP returns `null`.
- **Positions** are netted per coin across vaults, because Strategy A and B
  usually hold opposite sides of the same coin. **Gross exposure** is summed
  per vault before netting, so the netting never hides how much is on.
  Strategy X's positions are not visible.
- A snapshot pass in which any vault request fails is discarded rather than
  shown as a partial sum.
- **Absorptions** come from the `liquidation` object on child vault fills,
  read with `userFillsByTime` from a per vault watermark, so an empty or
  lagging response can never replay old fills. A full 2000 fill page means a
  backlog: the tracker pages forward (up to 6 pages per vault per poll) and
  logs a warning if it still cannot catch up. One liquidation is often
  filled by both Strategy A and B: fills are grouped by transaction hash
  into one absorption with the sizes summed, emitted once as a confirmed
  Hyperliquid liquidation, and stored as one `hlp_trades` row (upserted by
  hash, so a restart or a later share of the same liquidation never adds a
  row). The first poll seeds 24 hours of absorptions from the quiet
  Liquidator vaults as history, which is stored but never emitted as a live
  liquidation; the busy strategy vaults start at session start. Ordinary
  market making fills (~300 a minute) are not stored.

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
  `as_of`; `/v1/health` carries `position_scan.scan_age_seconds`,
  `stale_after_seconds` and `full_pass_worst_case_seconds`. The tracked set
  is capped (3,000 in the store, re-synced into the scanner after every
  hourly prune, plus at most what discovery adds in between), and the
  staleness threshold is **derived** from the worst-case healthy full pass
  over that cap with a 1.5× margin — about 25 minutes — rather than
  hand-picked. A scanner that has not re-fetched a displayed position within
  that window reads `stale` (feed status, health check, whales panel) —
  never `connected`; a healthy one never does.
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
| BTC price | Binance perp mark (`premiumIndex`), else the OKX perp mark where Binance futures is blocked | within 0.5% of hub |
| BTC long/short ratio | Binance `globalLongShortAccountRatio` | within 20% (warn beyond); reported as `warn` ("not cross-checked") when the hub's ratio comes from a fallback venue, since nothing was verified |
| Deribit DVOL | hub | present |
| Order flow freshness (blended) | engine `is_stale()` | some venue delivering trades |
| Order flow freshness per venue | engine `venue_freshness()` | `ok` (warn if this venue is out, fail if all are) |
| Orderbook freshness | engine `is_stale()` | not stale |
| Position scanner freshness | scanner `is_stale()` | last cycle and every displayed position younger than `POSITION_STALE_AFTER_SECONDS` (~25 min, derived from the tracked-set cap) (warn before the first cycle) |
| Market data freshness | hub refresh stamp | < 30s |
| Funding/consistency | hub | sane bands, funding sign vs L/S agree |

Run it once from the CLI:

```bash
hyperdata verify --wait 30
```

It prints a PASS/WARN/FAIL report and exits non-zero on any failure (handy for
CI). The same data is available live at `GET /v1/health` under `data_health`,
alongside per-feed `feeds` status.

## Durability

SQLite runs in WAL mode and commits on a time interval
(`COMMIT_INTERVAL_SECONDS`, default 5s) as well as every 50 events, so an
uncatchable crash (SIGKILL/OOM) loses at most a few seconds of events. A graceful
exit flushes via an `atexit` handler, and the headless server (`hyperdata api`)
installs SIGINT/SIGTERM handlers so `kill <pid>` shuts down cleanly.

Writes never run on the event loop: feed callbacks enqueue rows for a
dedicated writer thread (bounded queue; overflow is dropped and counted as
`dropped_writes`), and reads drain the queue first so a query immediately
after an event still sees it. `/v1/health` → `persistence` carries
`write_queue_pending` and `dropped_writes` (informational — they do not
gate the top-level status). A drain that times out — a read about to miss
rows, or a shutdown about to abandon queued writes — is logged at ERROR
with the count, and `DataStore.flush()`/`close()` return `False`. The startup integrity check
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
