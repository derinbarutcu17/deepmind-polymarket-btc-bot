import os
from dotenv import load_dotenv

load_dotenv()

# --- Polymarket Variables ---
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET")
POLYMARKET_API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE")
POLYMARKET_HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")

# --- Binance Oracle Variables ---
BINANCE_API_URL = os.getenv("BINANCE_API_URL", "https://api.binance.com/api/v3/ticker/price?symbol=")

# --- Bot Settings ---
DRY_RUN = os.getenv("DRY_RUN", "True").lower() in ("true", "1", "t")

# --- Strategy Parameters ---
TRADE_SIZE_USD = float(os.getenv("TRADE_SIZE_USD", "10.0"))
TREND_WINDOW_SECONDS = int(os.getenv("TREND_WINDOW_SECONDS", "60"))

# Legacy single-market variables (Optional, can be left blank for 5-min auto-trade)
TARGET_MARKET_CONDITION_ID = os.getenv("TARGET_MARKET_CONDITION_ID", "")
TARGET_TOKEN_ID = os.getenv("TARGET_TOKEN_ID", "")

if not POLYMARKET_API_KEY or not POLYMARKET_API_SECRET or not POLYMARKET_API_PASSPHRASE:
    print("Warning: Polymarket API keys not fully configured in .env.")
