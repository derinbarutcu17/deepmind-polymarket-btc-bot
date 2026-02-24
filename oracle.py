import asyncio
import aiohttp
import logging
from config import BINANCE_API_URL

logger = logging.getLogger(__name__)

class AsyncOracle:
    async def get_binance_btc_price(self, session: aiohttp.ClientSession) -> dict:
        try:
            async with session.get(BINANCE_API_URL + "BTCUSDT", timeout=2) as response:
                if response.status == 200:
                    data = await response.json()
                    price = float(data.get('price', 0.0))
                    return {'price': price, 'source': 'Binance'}
        except Exception as e:
            logger.debug(f"Binance fetch error: {e}")
        return {'price': 0.0, 'source': 'Binance'}

    async def get_chainlink_btc_price(self, session: aiohttp.ClientSession) -> dict:
        try:
            url = 'https://hermes.pyth.network/v2/updates/price/latest?ids[]=e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43'
            async with session.get(url, timeout=2) as response:
                if response.status == 200:
                    data = await response.json()
                    parsed = data.get('parsed', [])[0]
                    price_str = parsed['price']['price']
                    expo = parsed['price']['expo']
                    actual_price = float(price_str) * (10 ** expo)
                    return {'price': round(actual_price, 2), 'source': 'Pyth'}
        except Exception as e:
            logger.debug(f"Pyth fetch error: {e}")
        return {'price': 0.0, 'source': 'Pyth'}

    async def fetch_price(self) -> dict:
        """
        Fetches price from Pyth and Binance simultaneously using gather.
        Prefers Pyth. Falls back to Binance.
        """
        async with aiohttp.ClientSession() as session:
            pyth_task = self.get_chainlink_btc_price(session)
            binance_task = self.get_binance_btc_price(session)
            
            results = await asyncio.gather(pyth_task, binance_task)
            
            pyth_res = results[0]
            binance_res = results[1]
            
            if pyth_res['price'] > 0:
                return pyth_res
            elif binance_res['price'] > 0:
                logger.warning("Pyth stream down. Using Binance fallback.")
                return binance_res
            
            return {'price': 0.0, 'source': 'None'}
