import logging
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from config import (
    POLYMARKET_API_KEY,
    POLYMARKET_API_SECRET,
    POLYMARKET_API_PASSPHRASE,
    POLYMARKET_HOST
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

class PMClient:
    def __init__(self):
        try:
            creds = ApiCreds(
                api_key=POLYMARKET_API_KEY,
                api_secret=POLYMARKET_API_SECRET,
                api_passphrase=POLYMARKET_API_PASSPHRASE
            )
            # The client usually needs a private key for transaction signing (L1).
            # However, with API keys (L2), we can interact with the matching engine.
            # Empty key string is passed because L1 private key was not provided.
            self.client = ClobClient(
                host=POLYMARKET_HOST,
                key="", 
                chain_id=137,
                creds=creds
            )
            
            # Note: We do not set the auth_type to POLYGON if we only have API keys
            # or we set it to ClobAuthType.HEADER or L2 credentials parsing.
            self.client.set_api_creds(creds)
            logger.info("Polymarket ClobClient initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize Polymarket client: {e}")
            raise

    def get_market_price(self, token_id: str) -> dict:
        """
        Fetches the orderbook for a given token ID and returns the best bid and best ask.
        Returns {'bid': 0.0, 'ask': 1.0, 'mid': 0.5} as a fallback if empty.
        """
        try:
            # Query the orderbook for the token
            book = self.client.get_order_book(token_id)
            
            bids = book.bids
            asks = book.asks
            best_bid = float(bids[-1].price) if bids else 0.0
            best_ask = float(asks[-1].price) if asks else 1.0
            
            mid_price = 0.5
            if best_bid > 0.0 and best_ask < 1.0:
                mid_price = (best_bid + best_ask) / 2
            elif best_bid > 0.0:
                mid_price = best_bid
            elif best_ask < 1.0:
                mid_price = best_ask
                
            return {
                'bid': best_bid,
                'ask': best_ask,
                'mid': mid_price
            }
                
        except Exception as e:
            logger.error(f"Error fetching market price for token {token_id}: {e}")
            return {'bid': 0.0, 'ask': 1.0, 'mid': 0.5}

    def get_active_5m_btc_market(self):
        """
        Dynamically fetches the currently active '5-minute BTC Up/Down' market from the Gamma API.
        Uses a hybrid strategy: Predictive Slug Matching (fastest) + Tag-based Fallback.
        """
        import requests
        import time
        from datetime import datetime, timezone
        
        # 1. Predictive Slug Strategy
        # Reference known anchor: btc-updown-5m-1771796700 (starts Feb 22, 4:45PM ET / 21:45 UTC)
        anchor_ts = 1771796700
        curr_ts = int(time.time())
        # Calculate current and next 300s windows
        current_base = anchor_ts + ((curr_ts - anchor_ts) // 300) * 300
        
        # Check current and next 2 windows
        for ts in [current_base, current_base + 300, current_base + 600]:
            slug = f"btc-updown-5m-{ts}"
            try:
                url = f"https://gamma-api.polymarket.com/events?slug={slug}"
                resp = requests.get(url, timeout=5)
                if resp.status_code == 200:
                    events = resp.json()
                    if events:
                        m = events[0].get('markets', [{}])[0]
                        end_str = m.get('endDate')
                        if end_str:
                            end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                            now_dt = datetime.now(timezone.utc)
                            seconds_until_close = (end_dt - now_dt).total_seconds()
                            # If it hasn't expired yet
                            if seconds_until_close > 0:
                                tokens = m.get('clobTokenIds')
                                if isinstance(tokens, str):
                                    import json
                                    tokens = json.loads(tokens)
                                else:
                                    tokens = m.get('tokens', [])
                                    tokens = [t['token_id'] if isinstance(t, dict) else t for t in tokens]
                                    
                                if len(tokens) >= 2:
                                    logger.info(f"Predictive Disco Success: {m.get('question')} (Closes in {int(seconds_until_close)}s)")
                                    return {
                                        'title': m.get('question'),
                                        'condition_id': m.get('conditionId'),
                                        'yes_token': tokens[0],
                                        'no_token': tokens[1]
                                    }
            except:
                pass

        # 2. Tag-based Fallback (if slugs change)
        try:
            url = "https://gamma-api.polymarket.com/events?active=true&closed=false&tag_id=102892&limit=50"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                for event in data:
                    title = event.get('title', '').lower()
                    if 'bitcoin' in title or 'btc' in title:
                        m = event.get('markets', [{}])[0]
                        end_str = m.get('endDate')
                        if end_str:
                            end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                            if (end_dt - datetime.now(timezone.utc)).total_seconds() > 0:
                                tokens = m.get('tokens', [])
                                tokens = [t['token_id'] if isinstance(t, dict) else t for t in tokens]
                                if len(tokens) >= 2:
                                    logger.info(f"Tag Fallback Success: {m.get('question')}")
                                    return {
                                        'title': m.get('question'),
                                        'condition_id': m.get('conditionId'),
                                        'yes_token': tokens[0],
                                        'no_token': tokens[1]
                                    }
        except:
            pass
            
        logger.warning("No active 5-minute BTC market found.")
        return None

if __name__ == "__main__":
    pm = PMClient()
    active = pm.get_active_5m_btc_market()
    print(f"Active 5-min Market: {active}")
