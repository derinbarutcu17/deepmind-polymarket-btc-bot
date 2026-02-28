"""Oracle price fetcher — Pyth WebSocket primary feed, Binance REST fallback.

Production upgrade:
- AsyncOracle now opens a persistent WebSocket to Pyth Hermes instead of
  polling the REST endpoint every tick.  fetch_price() returns the in-memory
  cached value (zero latency) and falls back to Binance REST only when the WS
  has been down for more than _STALE_SECONDS.
- trading_paused is True while both feeds are unavailable, allowing main.py
  to gate strategy execution rather than trading on stale data.
- Auto-reconnect: the WS loop retries with a 3-second back-off on any error.
"""
import asyncio
import json
import logging
import time
from typing import Optional

import aiohttp

from config import BINANCE_API_URL, MAX_RETRIES

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=3)
_PYTH_WS_URL = "wss://hermes.pyth.network/ws"
_PYTH_BTC_FEED_ID = "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43"
_STALE_SECONDS = 5.0  # fall back to Binance REST if Pyth WS is this old


async def _fetch_with_retry(
    session: aiohttp.ClientSession, url: str, max_retries: int = MAX_RETRIES
) -> dict | None:
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
        self._price: float = 0.0
        self._source: str = "None"
        self._last_update: float = 0.0       # monotonic timestamp of last good price
        self._ws_task: Optional[asyncio.Task] = None
        self._paused: bool = True             # True until first WS or fallback succeeds

    async def start(self):
        """Launch the Pyth WebSocket feed background task. Call once after __init__."""
        self._ws_task = asyncio.create_task(self._pyth_ws_loop())
        logger.info("Oracle: Pyth WebSocket task started.")

    @property
    def trading_paused(self) -> bool:
        """True while no valid price is available (both feeds down)."""
        return self._paused

    # ── Pyth WebSocket ────────────────────────────────────────────────────

    async def _pyth_ws_loop(self):
        """Persistent connection to Pyth Hermes with automatic reconnection."""
        while True:
            try:
                async with self._session.ws_connect(_PYTH_WS_URL) as ws:
                    await ws.send_json({
                        "type": "subscribe",
                        "ids": [_PYTH_BTC_FEED_ID],
                    })
                    logger.info("Oracle: Pyth WebSocket connected.")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_pyth_msg(json.loads(msg.data))
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning(f"Oracle: Pyth WS closed/error: {msg.data}")
                            break

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"Oracle: Pyth WS exception ({e}). Reconnecting in 3s…")

            self._paused = True
            try:
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                return

    async def _handle_pyth_msg(self, data: dict):
        if data.get("type") != "price_update":
            return
        try:
            price_info = data["price_feed"]["price"]
            price = float(price_info["price"]) * (10 ** price_info["expo"])
            if price > 0:
                self._price = round(price, 2)
                self._source = "Pyth"
                self._last_update = time.monotonic()
                self._paused = False
        except (KeyError, TypeError, ValueError) as e:
            logger.debug(f"Oracle: Pyth WS parse error: {e}")

    # ── Binance REST fallback ─────────────────────────────────────────────

    async def get_binance_btc_price(self) -> dict:
        data = await _fetch_with_retry(self._session, BINANCE_API_URL + "BTCUSDT")
        if data:
            price = float(data.get("price", 0.0))
            return {"price": price, "source": "Binance"}
        return {"price": 0.0, "source": "Binance"}

    # ── Public interface ──────────────────────────────────────────────────

    async def fetch_price(self) -> dict:
        """Return cached Pyth WS price; fall back to Binance REST when stale."""
        age = time.monotonic() - self._last_update
        if self._price > 0 and age < _STALE_SECONDS:
            return {"price": self._price, "source": self._source}

        # WS data is stale or not yet seeded — try Binance REST
        binance = await self.get_binance_btc_price()
        if binance["price"] > 0:
            if age >= _STALE_SECONDS:
                logger.warning("Oracle: Pyth WS stale. Using Binance REST fallback.")
            self._price = binance["price"]
            self._source = "Binance"
            self._last_update = time.monotonic()
            self._paused = False
            return binance

        return {"price": 0.0, "source": "None"}

    async def close(self):
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        await self._session.close()
