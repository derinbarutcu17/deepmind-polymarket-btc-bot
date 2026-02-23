import requests
import logging
import json
import re
from config import BINANCE_API_URL

logger = logging.getLogger(__name__)

def get_binance_btc_price() -> float:
    """
    Fetches the current real-time BTC price from the public Binance API.
    Used as a fallback if the primary Oracle is unavailable.
    """
    try:
        # Append BTCUSDT since config lacks the token suffix.
        response = requests.get(BINANCE_API_URL + "BTCUSDT", timeout=5)
        response.raise_for_status()
        data = response.json()
        price = float(data.get('price', 0.0))
        return price
    except Exception as e:
        logger.error(f"Error fetching Binance BTC price: {e}")
        return 0.0

def get_chainlink_btc_price() -> float:
    """
    Fetches high-frequency BTC/USD price data.
    Since Chainlink Data Streams require enterprise API auth, this uses
    the Pyth Network Hermes API which provides sub-second institutional Oracle data
    matching Polymarket's resolution latency.
    """
    try:
        # Pyth Network Hermes v2 API Endpoint for BTC/USD (Feed ID: e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43)
        url = 'https://hermes.pyth.network/v2/updates/price/latest?ids[]=e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43'
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        
        data = response.json()
        parsed = data.get('parsed', [])[0]
        
        # Pyth returns components 'price' string and 'expo' integer.
        price_str = parsed['price']['price']
        expo = parsed['price']['expo']
        
        actual_price = float(price_str) * (10 ** expo)
        return round(actual_price, 2)

    except Exception as e:
        logger.warning(f"Error fetching Pyth Oracle stream: {e}. Falling back to Binance.")
        return get_binance_btc_price()

if __name__ == "__main__":
    # Test the Oracle
    price = get_chainlink_btc_price()
    print(f"Current Chainlink BTC/USD Oracle Price: ${price:.2f}")
