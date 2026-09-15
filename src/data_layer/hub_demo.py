"""Demo-mode mock data generators for HyperDataHub.

Extracted verbatim from hub.py (M13): roughly a third of the production
orchestrator was synthetic-data generation. Each coroutine takes the hub and
drives the same component state the live feeds would. HyperDataHub keeps
thin `_demo_*` delegators so task names, call sites and tests are unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import time

from src.data_layer.hlp_tracker import HLPPosition, HLPSnapshot, HLPTrade
from src.data_layer.liquidation_feed import LiquidationEvent
from src.data_layer.orderflow_engine import Trade
from src.data_layer.position_scanner import TrackedPosition
from src.data_layer.smart_money import SmartMoneySignal, WalletProfile

logger = logging.getLogger(__name__)


async def demo_liquidation_generator(hub) -> None:
    """Generate realistic mock liquidations."""
    import random
    exchanges = ["binance", "bybit", "okx", "hyperliquid"]
    weights = [0.30, 0.35, 0.20, 0.15]
    symbol_prices = {
        "BTC": 83500, "ETH": 3450, "SOL": 178, "DOGE": 0.165,
        "XRP": 0.62, "AVAX": 38, "LINK": 18.5, "ARB": 1.35,
    }
    while hub._running:
        exchange = random.choices(exchanges, weights=weights, k=1)[0]
        symbol = random.choice(list(symbol_prices.keys()))
        price = symbol_prices[symbol] * (1 + random.uniform(-0.02, 0.02))

        roll = random.random()
        if roll < 0.60:
            size_usd = random.uniform(500, 10_000)
        elif roll < 0.85:
            size_usd = random.uniform(10_000, 100_000)
        elif roll < 0.97:
            size_usd = random.uniform(100_000, 500_000)
        else:
            size_usd = random.uniform(500_000, 2_000_000)

        event = LiquidationEvent(
            timestamp=time.time(),
            exchange=exchange,
            symbol=symbol,
            side=random.choice(["long", "short"]),
            size_usd=size_usd,
            price=price,
            quantity=size_usd / price if price else 0,
        )
        await hub.liquidations.emit(event)
        await asyncio.sleep(random.uniform(0.3, 2.0))


async def demo_trade_generator(hub) -> None:
    """Generate realistic mock trades."""
    import random
    prices = {"BTC": 83500.0, "ETH": 3450.0, "SOL": 178.0}

    while hub._running:
        for symbol, base in prices.items():
            base += random.uniform(-base * 0.001, base * 0.001)
            prices[symbol] = base

            side = random.choices(["buy", "sell"], weights=[0.48, 0.52])[0]
            size = random.uniform(0.001, 0.5) if symbol == "BTC" else random.uniform(0.1, 50)

            trade = Trade(
                timestamp=time.time(),
                symbol=symbol,
                side=side,
                price=round(base, 2),
                size=round(size, 6),
                size_usd=round(size * base, 2),
            )
            hub.orderflow._process_trade(trade)

        await asyncio.sleep(random.uniform(0.05, 0.15))


async def demo_position_scan(hub) -> None:
    """Generate mock positions for demo mode."""
    import random
    positions = []
    symbol_prices = {
        "BTC": 83500, "ETH": 3450, "SOL": 178, "DOGE": 0.165,
        "XRP": 0.62, "AVAX": 38, "LINK": 18.5, "ARB": 1.35,
        "WIF": 2.80, "SUI": 3.45,
    }

    for _ in range(60):
        symbol = random.choice(list(symbol_prices.keys()))
        base_price = symbol_prices[symbol]
        current_price = base_price * (1 + random.uniform(-0.03, 0.03))
        side = random.choice(["long", "short"])
        leverage = random.choice([5, 10, 20, 25, 50, 100])
        size_usd = random.uniform(10_000, 10_000_000)

        offset = random.uniform(-3, 3)
        entry_price = current_price * (1 + offset / 100)

        mm = 0.03
        if side == "long":
            liq_price = entry_price * (1 - 1 / leverage + mm / leverage)
            pnl_pct = (current_price - entry_price) / entry_price
        else:
            liq_price = entry_price * (1 + 1 / leverage - mm / leverage)
            pnl_pct = (entry_price - current_price) / entry_price

        distance = abs(current_price - liq_price) / current_price * 100

        positions.append(TrackedPosition(
            address=f"0x{''.join(random.choices('0123456789abcdef', k=40))}",
            symbol=symbol,
            side=side,
            size_usd=size_usd,
            entry_price=entry_price,
            current_price=current_price,
            liq_price=liq_price,
            distance_pct=distance,
            leverage=leverage,
            unrealized_pnl=size_usd * pnl_pct,
            margin_used=size_usd / leverage,
            scanned_at=time.time(),
        ))

    hub.positions.positions = sorted(positions, key=lambda p: p.distance_pct)
    hub.positions.market_prices = {s: p for s, p in symbol_prices.items()}
    hub.positions.last_scan_at = time.time()
    hub.status.tracked_positions = len(positions)


async def demo_smart_money(hub) -> None:
    """Generate mock smart money wallets and signals for demo mode."""
    import random

    symbols = ["BTC", "ETH", "SOL", "DOGE", "XRP", "AVAX", "LINK", "ARB"]
    actions = ["OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT"]

    # Generate initial batch of wallet profiles
    for i in range(150):
        addr = f"0x{''.join(random.choices('0123456789abcdef', k=40))}"
        total_trades = random.randint(10, 500)
        winning = int(total_trades * random.uniform(0.2, 0.85))
        losing = total_trades - winning
        win_rate = winning / total_trades if total_trades else 0
        total_pnl = random.uniform(-500_000, 2_000_000) * (win_rate - 0.3)
        volume = random.uniform(100_000, 50_000_000)
        sharpe = random.uniform(-1.5, 3.0) * win_rate
        acct_value = random.uniform(1_000, 5_000_000)

        w = WalletProfile(
            address=addr,
            discovered_at=time.time() - random.uniform(0, 86400),
            last_seen=time.time() - random.uniform(0, 3600),
            last_analyzed=time.time(),
            total_trades=total_trades,
            winning_trades=winning,
            losing_trades=losing,
            total_realized_pnl=total_pnl,
            total_volume_usd=volume,
            largest_win=abs(total_pnl) * random.uniform(0.05, 0.3) if total_pnl > 0 else random.uniform(100, 50000),
            largest_loss=(-abs(total_pnl) * random.uniform(0.02, 0.15)
                          if total_pnl < 0 else -random.uniform(100, 30000)),
            avg_hold_time_seconds=random.uniform(60, 86400),
            win_rate=win_rate,
            sharpe_ratio=sharpe,
            account_value=acct_value,
            open_positions=random.randint(0, 5),
            active_symbols=random.sample(symbols, k=random.randint(0, 3)),
        )
        # Compute composite + pnl_score + sample-size confidence
        w.pnl_score = hub.smart_money._compute_pnl_score(w.total_realized_pnl)
        w.composite_score = hub.smart_money._compute_composite(w)
        w.confidence = hub.smart_money._compute_confidence(w)
        hub.smart_money.wallets[addr] = w

    hub.smart_money.rank_all()

    # Continuously generate signals
    while hub._running:
        try:
            # Occasionally add new wallets
            if random.random() < 0.1:
                addr = f"0x{''.join(random.choices('0123456789abcdef', k=40))}"
                hub.smart_money.wallets[addr] = WalletProfile(
                    address=addr,
                    discovered_at=time.time(),
                    last_seen=time.time(),
                    last_analyzed=0,
                    total_trades=random.randint(0, 5),
                )

            # Generate a signal from a ranked wallet
            ranked = [w for w in hub.smart_money.wallets.values()
                      if w.tier in ("smart", "dumb") and w.rank > 0]
            if ranked:
                wallet = random.choice(ranked)
                action = random.choice(actions)
                symbol = random.choice(symbols)
                size = random.uniform(5_000, 500_000)

                signal = SmartMoneySignal(
                    timestamp=time.time(),
                    address=wallet.address,
                    tier=wallet.tier,
                    action=action,
                    symbol=symbol,
                    size_usd=size,
                    wallet_rank=wallet.rank,
                    wallet_win_rate=wallet.win_rate,
                    wallet_pnl=wallet.total_realized_pnl,
                    signal_type="follow" if wallet.tier == "smart" else "fade",
                    wallet_confidence=wallet.confidence,
                )
                hub.smart_money.signals.append(signal)
                hub.smart_money._emit_signal(signal)

        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Demo smart money error")

        await asyncio.sleep(random.uniform(2, 8))


async def demo_hlp(hub) -> None:
    """Generate mock HLP vault data for demo mode."""
    import random

    symbols = ["BTC", "ETH", "SOL", "DOGE", "XRP", "AVAX", "LINK", "ARB",
               "WIF", "PEPE", "SUI", "APT", "OP", "SEI", "TIA", "JUP",
               "ONDO", "RENDER", "INJ", "FET"]
    symbol_prices = {
        "BTC": 83500, "ETH": 3450, "SOL": 178, "DOGE": 0.165,
        "XRP": 0.62, "AVAX": 38, "LINK": 18.5, "ARB": 1.35,
        "WIF": 2.80, "PEPE": 0.0000125, "SUI": 3.45, "APT": 11.8,
        "OP": 3.20, "SEI": 0.85, "TIA": 12.5, "JUP": 1.15,
        "ONDO": 1.45, "RENDER": 8.9, "INJ": 28.5, "FET": 2.35,
    }

    base_account_value = 210_000_000.0  # ~$210M AUM
    session_start_value = base_account_value

    while hub._running:
        try:
            # Build mock positions (HLP typically has 30-80 positions)
            num_positions = random.randint(35, 65)
            positions = []
            net_delta_usd = 0.0
            total_exposure_usd = 0.0
            total_unrealized_pnl = 0.0

            for _ in range(num_positions):
                sym = random.choice(symbols)
                base_price = symbol_prices[sym]
                current_price = base_price * (1 + random.uniform(-0.02, 0.02))

                # HLP tends to have large positions — range from $50K to $20M
                size_usd = random.uniform(50_000, 20_000_000)
                side = random.choice(["long", "short"])
                size = size_usd / current_price
                if side == "short":
                    size = -size

                entry_offset = random.uniform(-0.005, 0.005)
                entry_price = current_price * (1 + entry_offset)

                if side == "long":
                    unrealized_pnl = (current_price - entry_price) / entry_price * size_usd
                else:
                    unrealized_pnl = (entry_price - current_price) / entry_price * size_usd

                leverage = random.choice([3, 5, 10, 20])

                positions.append(HLPPosition(
                    symbol=sym,
                    side=side,
                    size=size,
                    size_usd=size_usd,
                    entry_price=entry_price,
                    current_price=current_price,
                    unrealized_pnl=unrealized_pnl,
                    leverage=leverage,
                ))

                signed_value = size_usd if side == "long" else -size_usd
                net_delta_usd += signed_value
                total_exposure_usd += size_usd
                total_unrealized_pnl += unrealized_pnl

            # Drift account value slightly
            base_account_value += random.uniform(-50_000, 80_000)
            total_margin = total_exposure_usd * 0.1  # ~10x effective

            snapshot = HLPSnapshot(
                timestamp=time.time(),
                account_value=base_account_value,
                total_margin_used=total_margin,
                positions=positions,
                net_delta_usd=net_delta_usd,
                total_exposure_usd=total_exposure_usd,
                num_positions=num_positions,
                total_unrealized_pnl=total_unrealized_pnl,
                session_pnl=base_account_value - session_start_value,
            )
            snapshot.delta_zscore = hub.hlp._compute_delta_zscore(net_delta_usd)
            hub.hlp.snapshots.append(snapshot)

            if hub.hlp._session_start_value == 0:
                hub.hlp._session_start_value = base_account_value

            # Generate a few mock fills per cycle
            num_fills = random.randint(1, 5)
            for _ in range(num_fills):
                sym = random.choice(symbols)
                price = symbol_prices[sym] * (1 + random.uniform(-0.01, 0.01))
                size = random.uniform(100, 50_000) / price
                side = random.choice(["buy", "sell"])
                directions = ["Open Long", "Open Short", "Close Long", "Close Short"]
                direction = random.choice(directions)

                # ~20% of opens are liquidation absorptions
                is_liq = direction.startswith("Open") and random.random() < 0.2
                closed_pnl = 0.0
                if direction.startswith("Close"):
                    closed_pnl = random.uniform(-5_000, 10_000)

                trade = HLPTrade(
                    timestamp=time.time(),
                    symbol=sym,
                    side=side,
                    price=price,
                    size=size,
                    size_usd=price * size,
                    direction=direction,
                    closed_pnl=closed_pnl,
                    is_liquidation=is_liq,
                )
                hub.hlp.trades.append(trade)
                for cb in hub.hlp._callbacks:
                    try:
                        cb(trade)
                    except Exception:
                        logger.exception("Demo HLP trade callback error")

        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Demo HLP error")

        await asyncio.sleep(random.uniform(1.0, 3.0))


async def demo_market_refresh(hub) -> None:
    """Generate mock market data for demo mode."""
    import random

    from src.data_layer.market_data import AssetInfo

    mock_assets = [
        ("BTC", 83500, 500), ("ETH", 3450, 50), ("SOL", 178, 5),
        ("DOGE", 0.165, 0.005), ("XRP", 0.62, 0.02), ("AVAX", 38, 1.5),
        ("LINK", 18.5, 0.5), ("ARB", 1.35, 0.1), ("WIF", 2.80, 0.15),
        ("PEPE", 0.0000125, 0.000002), ("SUI", 3.45, 0.2), ("APT", 11.8, 0.5),
        ("OP", 3.20, 0.15), ("SEI", 0.85, 0.05), ("TIA", 12.5, 0.8),
        ("JUP", 1.15, 0.08), ("ONDO", 1.45, 0.1), ("RENDER", 8.9, 0.4),
        ("INJ", 28.5, 1.5), ("FET", 2.35, 0.15),
    ]

    assets = {}
    for sym, base, jitter in mock_assets:
        price = base + random.uniform(-jitter, jitter)
        funding = random.uniform(-0.0005, 0.0005)
        if random.random() < 0.15:
            funding = random.choice([-1, 1]) * random.uniform(0.001, 0.005)

        mark = price * (1 + random.uniform(-0.001, 0.001))
        index = price * (1 + random.uniform(-0.002, 0.002))
        premium = (mark - index) / index * 100 if index > 0 else 0.0
        assets[sym] = AssetInfo(
            symbol=sym,
            price=price,
            funding_rate=funding,
            open_interest=random.uniform(5e6, 2e9),
            volume_24h=random.uniform(1e7, 5e9),
            price_change_24h_pct=random.uniform(-0.08, 0.08),
            mark_price=mark,
            index_price=index,
            premium_pct=premium,
        )

    hub.market.assets = assets
    hub.market.last_update = time.monotonic()


async def demo_deribit(hub) -> None:
    """Generate synthetic Deribit DVOL data for demo mode."""
    import random

    from src.data_layer.deribit import DeribitIVSnapshot

    btc_iv = 55.0
    eth_iv = 75.0
    while hub._running:
        try:
            btc_iv += random.uniform(-0.8, 0.8)
            btc_iv = max(45.0, min(65.0, btc_iv))
            eth_iv += random.uniform(-1.2, 1.2)
            eth_iv = max(60.0, min(90.0, eth_iv))

            now = time.time()
            hub.deribit.snapshots["BTC"] = DeribitIVSnapshot(
                timestamp=now, underlying="BTC", mark_iv=btc_iv,
                bid_iv=0.0, ask_iv=0.0, oi_usd=0.0,
                index_price=hub.market.assets.get("BTC", None) and hub.market.assets["BTC"].price or 83500.0,
            )
            hub.deribit.snapshots["ETH"] = DeribitIVSnapshot(
                timestamp=now, underlying="ETH", mark_iv=eth_iv,
                bid_iv=0.0, ask_iv=0.0, oi_usd=0.0,
                index_price=hub.market.assets.get("ETH", None) and hub.market.assets["ETH"].price or 3450.0,
            )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Demo Deribit error")
        await asyncio.sleep(random.uniform(2.0, 5.0))


async def demo_basis(hub) -> None:
    """Generate synthetic spot/perp basis data for demo mode."""
    import random

    from src.data_layer.spot_prices import SpotPriceSnapshot

    bases = {"BTC": 0.05, "ETH": 0.03, "SOL": 0.08}
    while hub._running:
        try:
            now = time.time()
            for sym in ["BTC", "ETH", "SOL"]:
                bases[sym] += random.uniform(-0.02, 0.02)
                bases[sym] = max(-0.05, min(0.15, bases[sym]))
                # Occasional spike
                if random.random() < 0.05:
                    bases[sym] += random.choice([-1, 1]) * random.uniform(0.05, 0.1)
                    bases[sym] = max(-0.15, min(0.25, bases[sym]))

                asset = hub.market.assets.get(sym)
                perp_price = asset.price if asset else {"BTC": 83500, "ETH": 3450, "SOL": 178}[sym]
                spot_price = perp_price / (1 + bases[sym] / 100)
                hub.spot.prices[sym] = SpotPriceSnapshot(
                    timestamp=now, symbol=sym,
                    spot_price=spot_price, perp_price=perp_price,
                    basis_pct=bases[sym],
                )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Demo basis error")
        await asyncio.sleep(random.uniform(3.0, 6.0))


async def demo_lsr(hub) -> None:
    """Generate synthetic long/short ratio data for demo mode."""
    import random

    from src.data_layer.long_short_ratio import LongShortSnapshot

    ratios = {"BTC": 1.1, "ETH": 0.95, "SOL": 1.2}
    while hub._running:
        try:
            now = time.time()
            for sym in ["BTC", "ETH", "SOL"]:
                ratios[sym] += random.uniform(-0.03, 0.03)
                ratios[sym] = max(0.8, min(1.4, ratios[sym]))
                ls = ratios[sym]
                long_r = ls / (1 + ls)
                short_r = 1.0 - long_r
                hub.lsr.ratios[sym] = LongShortSnapshot(
                    timestamp=now, symbol=sym,
                    long_ratio=long_r, short_ratio=short_r,
                    long_short_ratio=ls,
                )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Demo LSR error")
        await asyncio.sleep(random.uniform(5.0, 10.0))
