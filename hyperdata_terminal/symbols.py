"""Per-venue contract names for coins that trade in multiples.

Binance and Bybit list low-priced coins as 1000-unit contracts
(1000PEPEUSDT: the price is per 1000 PEPE and one unit of quantity is 1000
PEPE), and Hyperliquid as k-coins (kPEPE). Subscribing to plain PEPEUSDT
or PEPE hits topics that do not exist (on Bybit one bad topic in a
subscribe request fails the whole request), and normalizing 1000PEPEUSDT
by stripping the suffix gives "1000PEPE", which a PEPE filter misses.

Every venue symbol maps to (canonical coin, multiplier): the notional
(price times quantity) is unchanged, the per-coin price is price /
multiplier and the coin quantity is quantity * multiplier. Only names
checked against the venues' live symbol lists (2026-10-10) are mapped:
some 1000-prefixed names are distinct coins, not multiples (Binance lists
both CATUSDT and 1000CATUSDT), so a prefix is never stripped blindly. OKX
quotes these per coin with a large contract value, so it needs no map.
"""
from __future__ import annotations

_BINANCE = {
    "1000PEPEUSDT": ("PEPE", 1000), "1000BONKUSDT": ("BONK", 1000), "1000FLOKIUSDT": ("FLOKI", 1000),
    "1000SHIBUSDT": ("SHIB", 1000), "1000LUNCUSDT": ("LUNC", 1000), "1000XECUSDT": ("XEC", 1000),
    "1000SATSUSDT": ("SATS", 1000), "1000RATSUSDT": ("RATS", 1000),
    "1000000MOGUSDT": ("MOG", 1_000_000), "1MBABYDOGEUSDT": ("BABYDOGE", 1_000_000),
}
_BYBIT = {
    "1000PEPEUSDT": ("PEPE", 1000), "1000BONKUSDT": ("BONK", 1000), "1000FLOKIUSDT": ("FLOKI", 1000),
    "1000LUNCUSDT": ("LUNC", 1000), "1000XECUSDT": ("XEC", 1000), "1000RATSUSDT": ("RATS", 1000),
    "1000TURBOUSDT": ("TURBO", 1000), "1000TOSHIUSDT": ("TOSHI", 1000), "1000BTTUSDT": ("BTT", 1000),
    "1000000MOGUSDT": ("MOG", 1_000_000),
}
_HYPERLIQUID = {
    "kPEPE": ("PEPE", 1000), "kBONK": ("BONK", 1000), "kFLOKI": ("FLOKI", 1000), "kSHIB": ("SHIB", 1000),
    "kLUNC": ("LUNC", 1000), "kDOGS": ("DOGS", 1000), "kNEIRO": ("NEIRO", 1000),
}
_BY_VENUE = {"binance": _BINANCE, "bybit": _BYBIT, "hyperliquid": _HYPERLIQUID}
_REVERSE = {venue: {coin: (raw, mult) for raw, (coin, mult) in table.items()} for venue, table in _BY_VENUE.items()}


def canonical(venue: str, raw: str) -> tuple[str, int]:
    """(coin, multiplier) for a venue's contract name."""
    venue = venue.lower()
    table = _BY_VENUE.get(venue, {})
    key = raw if venue == "hyperliquid" else raw.upper()
    if key in table:
        return table[key]
    if venue == "okx":
        return raw.upper().split("-")[0], 1
    if venue in ("binance", "bybit"):
        name = raw.upper()
        for suffix in ("USDT", "USDC", "USD", "PERP"):
            if name.endswith(suffix):
                return name[: -len(suffix)], 1
        return name, 1
    return raw, 1


def venue_contract(venue: str, coin: str) -> tuple[str, int]:
    """(venue contract name, multiplier) for a canonical coin.

    Binance and Bybit names are the USDT-margined perpetual (BTC -> BTCUSDT).
    """
    venue = venue.lower()
    hit = _REVERSE.get(venue, {}).get(coin)
    if hit:
        return hit
    if venue in ("binance", "bybit"):
        return f"{coin}USDT", 1
    return coin, 1
