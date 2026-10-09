from __future__ import annotations

# Exchange endpoints
HYPERLIQUID_API_URL = "https://api.hyperliquid.xyz"
HYPERLIQUID_INFO_URL = f"{HYPERLIQUID_API_URL}/info"
HYPERLIQUID_WS_URL = "wss://api.hyperliquid.xyz/ws"
BINANCE_FUTURES_WS = "wss://fstream.binance.com/ws"
BYBIT_WS = "wss://stream.bybit.com/v5/public/linear"
OKX_WS = "wss://ws.okx.com:8443/ws/v5/public"

# Scanning
DEFAULT_SYMBOLS = [
    # Top 50 by volume — covers >95% of Hyperliquid trading activity
    "BTC", "ETH", "SOL", "DOGE", "XRP", "AVAX", "LINK", "ARB",
    "WIF", "PEPE", "SUI", "APT", "OP", "SEI", "TIA", "JUP",
    "ONDO", "RENDER", "INJ", "FET", "NEAR", "ATOM", "DOT", "ADA",
    "FIL", "LTC", "BCH", "AAVE", "UNI", "MKR", "HYPE", "TAO",
    "PENGU", "TRUMP", "ENA", "W", "PYTH", "JTO", "BONK", "FLOKI",
    "STX", "IMX", "MANTA", "ZEC", "COMP", "SNX", "CRV", "LDO",
    "RUNE", "DYDX",
]
SCAN_INTERVAL_SECONDS = 5
POSITION_SCAN_INTERVAL = 30

# Liquidation zones
LIQ_ZONE_1_PCT = 1.0
LIQ_ZONE_2_PCT = 2.0
LIQ_ZONE_5_PCT = 5.0
LIQ_ZONE_15_PCT = 15.0
MIN_POSITION_SIZE_USD = 10000

# Rate limiting
MAX_REQUESTS_PER_SECOND = 10
WEBSOCKET_RECONNECT_DELAY = 5
WEBSOCKET_MAX_RECONNECT_DELAY = 60

# Data retention
MAX_LIQUIDATION_EVENTS = 10000
MAX_TRADES_PER_SYMBOL = 1000
DISCOVERED_ADDRESSES_FILE = "data/discovered_addresses.json"

# Dashboard
DASHBOARD_REFRESH_RATE = 5  # seconds
