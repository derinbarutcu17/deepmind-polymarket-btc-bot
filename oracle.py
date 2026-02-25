"""Oracle price fetcher with retry/backoff for Pyth and Binance."""
import asyncio
import logging

import aiohttp

from config import BINANCE_API_URL, MAX_RETRIES

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=3)


async def _fetch_with_retry(session: aiohttp.ClientSession, url: str, max_retries: int = MAX_RETRIES) -> dict | None:
    """GET with exponential backoff. Returns parsed JSON or None."""
    for attempt in range(max_retries):
        try:
            async with session.get(url, timeout=_TIMEOUT) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status in (429, 500, 502, 503, 504):
                    wait = 0.5 * (2 ** attempt)
                    logger.debug(f"HTTP {resp.status} from {url[:60]}… retry in {wait:.1f}s")
                    await asyncio.sleep(wait)
                    continue
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            wait = 0.5 * (2 ** attempt)
            logger.debug(f"Fetch error ({e}) — retry in {wait:.1f}s")
            await asyncio.sleep(wait)
    return None


class AsyncOracle:
    def __init__(self):
        self._session = aiohttp.ClientSession()

    async def get_binance_btc_price(self) -> dict:
        data = await _fetch_with_retry(self._session, BINANCE_API_URL + "BTCUSDT")
        if data:
            price = float(data.get("price", 0.0))
            return {"price": price, "source": "Binance"}
        return {"price": 0.0, "source": "Binance"}

    async def get_chainlink_btc_price(self) -> dict:
        url = (
            "https://hermes.pyth.network/v2/updates/price/latest"
            "?ids[]=e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"
        )
        data = await _fetch_with_retry(self._session, url)
        if data:
            try:
                parsed = data.get("parsed", [])[0]
                price_str = parsed["price"]["price"]
                expo = parsed["price"]["expo"]
                actual_price = float(price_str) * (10 ** expo)
                return {"price": round(actual_price, 2), "source": "Pyth"}
            except (IndexError, KeyError, TypeError) as e:
                logger.debug(f"Pyth parse error: {e}")
        return {"price": 0.0, "source": "Pyth"}

    async def fetch_price(self) -> dict:
        pyth_task = self.get_chainlink_btc_price()
        binance_task = self.get_binance_btc_price()

        results = await asyncio.gather(pyth_task, binance_task)
        pyth_res, binance_res = results[0], results[1]

        if pyth_res["price"] > 0:
            return pyth_res
        elif binance_res["price"] > 0:
            logger.warning("Pyth stream down. Using Binance fallback.")
            return binance_res

        return {"price": 0.0, "source": "None"}

    async def close(self):
        await self._session.close()
