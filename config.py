"""Bot configuration — all tunable parameters centralized here."""
import os
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()

# ── Polymarket API ───────────────────────────────────────────────────────
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET")
POLYMARKET_API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE")
POLYMARKET_HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")

# ── Oracle ───────────────────────────────────────────────────────────────
BINANCE_API_URL = os.getenv("BINANCE_API_URL", "https://api.binance.com/api/v3/ticker/price?symbol=")

# ── Mode ─────────────────────────────────────────────────────────────────
DRY_RUN = os.getenv("DRY_RUN", "True").lower() in ("true", "1", "t")

# ── Sizing (Decimal) ────────────────────────────────────────────────────
TRADE_SIZE_USD = float(os.getenv("TRADE_SIZE_USD", "5.0"))
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "20.0"))

# ── Strategy ─────────────────────────────────────────────────────────────
SHORT_EMA_PERIOD = int(os.getenv("SHORT_EMA_PERIOD", "5"))
LONG_EMA_PERIOD = int(os.getenv("LONG_EMA_PERIOD", "15"))
VOLATILITY_K = float(os.getenv("VOLATILITY_K", "1.5"))

# ── Risk ─────────────────────────────────────────────────────────────────
CIRCUIT_BREAKER_USD = Decimal(os.getenv("CIRCUIT_BREAKER_USD", "15.0"))
MAKER_FILL_RATE_THRESHOLD = float(os.getenv("MAKER_FILL_RATE_THRESHOLD", "0.40"))

# ── Network ──────────────────────────────────────────────────────────────
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# ── Validation ───────────────────────────────────────────────────────────
if not POLYMARKET_API_KEY or not POLYMARKET_API_SECRET or not POLYMARKET_API_PASSPHRASE:
    print("Warning: Polymarket API keys not fully configured in .env.")
