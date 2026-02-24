import logging
import asyncio
import aiohttp
from datetime import datetime, timezone
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from typing import Optional
from config import (
    POLYMARKET_API_KEY,
    POLYMARKET_API_SECRET,
    POLYMARKET_API_PASSPHRASE,
    POLYMARKET_HOST
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

class AsyncPMClient:
    def __init__(self):
        try:
            creds = ApiCreds(
                api_key=POLYMARKET_API_KEY,
                api_secret=POLYMARKET_API_SECRET,
                api_passphrase=POLYMARKET_API_PASSPHRASE
            )
            # Sync client for strictly necessary synchronous tasks like signing
            self.sync_client = ClobClient(
                host=POLYMARKET_HOST,
                key="", 
                chain_id=137,
                creds=creds
            )
            self.sync_client.set_api_creds(creds)
            logger.info("Polymarket ClobClient initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize Polymarket client: {e}")
            raise
        
        self._session = aiohttp.ClientSession()

    async def close(self):
        await self._session.close()

    async def fetch_orderbook(self, token_id: str) -> dict:
        """
        Asynchronously fetches the orderbook for a given token ID.
        Returns {'bid': 0.0, 'ask': 1.0} as a fallback if empty.
        """
        try:
            url = f"{POLYMARKET_HOST}/book?token_id={token_id}"
            async with self._session.get(url, timeout=2) as response:
                if response.status == 200:
                    book = await response.json()
                    bids = book.get('bids', [])
                    asks = book.get('asks', [])
                    
                    if bids:
                        bids = sorted(bids, key=lambda x: float(x['price']), reverse=True)
                        best_bid = float(bids[0]['price'])
                    else:
                        best_bid = 0.0
                        
                    if asks:
                        asks = sorted(asks, key=lambda x: float(x['price']))
                        best_ask = float(asks[0]['price'])
                    else:
                        best_ask = 1.0
                        
                    return {'bid': best_bid, 'ask': best_ask, 'bids': bids[:5], 'asks': asks[:5]}
                else:
                    return {'bid': 0.0, 'ask': 1.0, 'bids': [], 'asks': []}
        except Exception as e:
            logger.error(f"Error fetching orderbook for token {token_id}: {e}")
            return {'bid': 0.0, 'ask': 1.0, 'bids': [], 'asks': []}

    async def get_active_market(self):
        """
        Dynamically fetches the currently active '5-minute BTC Up/Down' market from the Gamma API.
        Uses predictive Slug calculation based on current UNIX timestamp blocks.
        """
        import time, json
        
        # Polymarket 5-minute slugs are structured as "btc-updown-5m-{UNIX_TS}" 
        # where UNIX_TS is exactly divisible by 300.
        curr_ts = int(time.time())
        current_base = (curr_ts // 300) * 300
        
        # Check current window and next 2 just in case
        for ts in [current_base, current_base + 300, current_base + 600, current_base - 300]:
            slug = f"btc-updown-5m-{ts}"
            try:
                url = f"https://gamma-api.polymarket.com/events?slug={slug}"
                async with self._session.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data:
                            event = data[0]
                            m = event.get('markets', [{}])[0]
                            end_str = m.get('endDate')
                            if end_str:
                                end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                                seconds_until_close = (end_dt - datetime.now(timezone.utc)).total_seconds()
                                
                                # If it hasn't expired yet and closes within the next 8 minutes
                                if 0 < seconds_until_close < 480:
                                    tokens = m.get('clobTokenIds')
                                    if not tokens:
                                        tokens = m.get('tokens', [])
                                        
                                    if isinstance(tokens, str):
                                        tokens = json.loads(tokens)
                                        
                                    token_ids = [t['token_id'] if isinstance(t, dict) else t for t in tokens]
                                    
                                    if len(token_ids) >= 2:
                                        logger.info(f"🎯 Discovered Active Market: [bold yellow]{m.get('question')}[/bold yellow] (Closes in {int(seconds_until_close)}s)", extra={"markup": True})
                                        return {
                                            'title': m.get('question'),
                                            'condition_id': m.get('conditionId'),
                                            'yes_token': token_ids[0],
                                            'no_token': token_ids[1],
                                            'expires_at': end_dt.timestamp(),
                                            'slug': slug
                                        }
            except Exception as e:
                logger.debug(f"Slug fallback error on {slug}: {e}")

        logger.warning("No active 5-minute BTC market found closing soon.")
        return None

    def cancel_all_orders(self):
        """
        Cancels all open orders. Called to prevent 'ghost checks'.
        """
        import time
        if hasattr(self, '_last_cancel_time') and time.time() - self._last_cancel_time < 2.0:
            return  # Rate limit global cancels to max 1 per 2 seconds
        self._last_cancel_time = time.time()
        
        try:
            logger.warning("⚠️ [GLOBAL CANCEL] Nuking all resting Polymarket orders. Ensure bot runs on an isolated API sub-account!")
            res = self.sync_client.cancel_all()
            if res.get('success'):
                logger.debug("🗑️ Cleared open orders.")
            else:
                pass 
        except Exception as e:
            logger.error(f"Failed to cancel orders: {e}")

    async def place_limit_order(self, token_id: str, side: str, price: float, size: float, post_only: bool = True) -> Optional[str]:
        """
        Submits a POST-ONLY Maker limit order safely outside the async event loop.
        If the order threatens to cross the spread and cost Taker fees, Polymarket instantly rejects it.
        Returns the OrderID string if successful, otherwise None.
        """
        def _build_and_post():
            order_args = OrderArgs(
                price=price,
                size=size,
                side=side,
                token_id=token_id
            )
            signed_order = self.sync_client.create_order(order_args)
            return self.sync_client.post_order(signed_order, OrderType.GTC, post_only=post_only)

        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(None, _build_and_post)
            
            if res and res.get('success'):
                logger.info(f"✅ [bold green]Live Order Placed![/bold green] Maker {side} for {size:.2f} shares @ ${price:.3f}", extra={"markup": True})
                return res.get('orderID')
            else:
                logger.error(f"❌ Order Failed/Rejected (Crossed Spread?): {res.get('errorMsg', res)}")
                return None
        except Exception as e:
            logger.error(f"💥 Live Order API Exception: {e}")
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """
        Targeted cancellation of a specific order ID.
        """
        def _cancel():
            return self.sync_client.cancel(order_id)
            
        try:
            try: loop = asyncio.get_running_loop()
            except RuntimeError: loop = asyncio.get_event_loop()
            
            res = await loop.run_in_executor(None, _cancel)
            if res and res == "sucesses" or (isinstance(res, dict) and res.get('success', True)):
                logger.debug(f"🗑️ Cancelled order ID: {order_id[:8]}...")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def check_resolution(self, slug: str) -> Optional[str]:
        """
        Pings the Gamma API for a closed market and returns the winning token ID.
        Returns None if not resolved yet.
        """
        try:
            url = f"https://gamma-api.polymarket.com/events?slug={slug}&closed=true"
            async with self._session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        m = data[0].get('markets', [{}])[0]
                        if m.get('closed'):
                            prices = m.get('outcomePrices', [])
                            tokens = m.get('clobTokenIds', [])
                            import json
                            if isinstance(tokens, str): tokens = json.loads(tokens)
                            if isinstance(prices, str): prices = json.loads(prices)
                            token_ids = [t['token_id'] if isinstance(t, dict) else t for t in tokens]
                            
                            for idx, p in enumerate(prices):
                                if float(p) >= 0.99 or p == "1":
                                    return token_ids[idx]
        except Exception as e:
            logger.debug(f"Error checking resolution for {slug}: {e}")
            
        return None
