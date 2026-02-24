"""Async Polymarket CLOB client with retry/backoff and audit logging."""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

from config import (
    POLYMARKET_API_KEY,
    POLYMARKET_API_SECRET,
    POLYMARKET_API_PASSPHRASE,
    POLYMARKET_HOST,
    MAX_RETRIES,
)

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=5)
_API_LOG_DIR = "logs/api_responses"


def _ensure_audit_dir():
    os.makedirs(_API_LOG_DIR, exist_ok=True)


def _audit_log(label: str, data):
    """Persist raw API response for audit."""
    try:
        _ensure_audit_dir()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(_API_LOG_DIR, f"{ts}_{label}.json")
        with open(path, "w") as f:
            json.dump(data if isinstance(data, (dict, list)) else str(data), f, indent=2, default=str)
    except Exception:
        pass  # audit failures must never crash the bot


async def _fetch_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    max_retries: int = MAX_RETRIES,
) -> Optional[dict]:
    """GET with exponential backoff. Handles 429/5xx."""
    for attempt in range(max_retries):
        try:
            async with session.get(url, timeout=_TIMEOUT) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status in (429, 500, 502, 503, 504):
                    wait = 0.5 * (2 ** attempt)
                    logger.debug(f"HTTP {resp.status} — retry {attempt + 1}/{max_retries} in {wait:.1f}s")
                    await asyncio.sleep(wait)
                    continue
                logger.debug(f"HTTP {resp.status} from {url[:80]}")
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            wait = 0.5 * (2 ** attempt)
            logger.debug(f"Fetch error ({e}) — retry {attempt + 1}/{max_retries}")
            await asyncio.sleep(wait)
    return None


class AsyncPMClient:
    def __init__(self):
        try:
            creds = ApiCreds(
                api_key=POLYMARKET_API_KEY,
                api_secret=POLYMARKET_API_SECRET,
                api_passphrase=POLYMARKET_API_PASSPHRASE,
            )
            self.sync_client = ClobClient(
                host=POLYMARKET_HOST, key="", chain_id=137, creds=creds,
            )
            self.sync_client.set_api_creds(creds)
            logger.info("Polymarket ClobClient initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize Polymarket client: {e}")
            raise

        self._session = aiohttp.ClientSession()

    async def close(self):
        await self._session.close()

    # ── orderbook ────────────────────────────────────────────────────────

    async def fetch_orderbook(self, token_id: str) -> dict:
        url = f"{POLYMARKET_HOST}/book?token_id={token_id}"
        data = await _fetch_with_retry(self._session, url)

        if data:
            bids = data.get("bids", [])
            asks = data.get("asks", [])

            if bids:
                bids = sorted(bids, key=lambda x: float(x["price"]), reverse=True)
                best_bid = float(bids[0]["price"])
            else:
                best_bid = 0.0

            if asks:
                asks = sorted(asks, key=lambda x: float(x["price"]))
                best_ask = float(asks[0]["price"])
            else:
                best_ask = 1.0

            return {"bid": best_bid, "ask": best_ask, "bids": bids[:5], "asks": asks[:5]}

        return {"bid": 0.0, "ask": 1.0, "bids": [], "asks": []}

    # ── market discovery ─────────────────────────────────────────────────

    async def get_active_market(self):
        curr_ts = int(time.time())
        current_base = (curr_ts // 300) * 300

        for ts in [current_base, current_base + 300, current_base + 600, current_base - 300]:
            slug = f"btc-updown-5m-{ts}"
            url = f"https://gamma-api.polymarket.com/events?slug={slug}"
            data = await _fetch_with_retry(self._session, url)

            if not data:
                continue

            try:
                event = data[0]
                m = event.get("markets", [{}])[0]
                end_str = m.get("endDate")
                if not end_str:
                    continue

                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                seconds_until_close = (end_dt - datetime.now(timezone.utc)).total_seconds()

                if 0 < seconds_until_close < 480:
                    tokens = m.get("clobTokenIds")
                    if not tokens:
                        tokens = m.get("tokens", [])
                    if isinstance(tokens, str):
                        tokens = json.loads(tokens)

                    token_ids = [t["token_id"] if isinstance(t, dict) else t for t in tokens]

                    if len(token_ids) >= 2:
                        logger.info(
                            f"🎯 Discovered Active Market: [bold yellow]{m.get('question')}[/bold yellow] "
                            f"(Closes in {int(seconds_until_close)}s)",
                            extra={"markup": True},
                        )
                        return {
                            "title": m.get("question"),
                            "condition_id": m.get("conditionId"),
                            "yes_token": token_ids[0],
                            "no_token": token_ids[1],
                            "expires_at": end_dt.timestamp(),
                            "slug": slug,
                        }
            except (IndexError, KeyError, TypeError) as e:
                logger.debug(f"Parse error on {slug}: {e}")

        logger.warning("No active 5-minute BTC market found closing soon.")
        return None

    # ── order management ─────────────────────────────────────────────────

    def cancel_all_orders(self):
        """EMERGENCY ONLY. Nukes all resting orders globally."""
        if hasattr(self, "_last_cancel_time") and time.time() - self._last_cancel_time < 2.0:
            return
        self._last_cancel_time = time.time()

        logger.warning(
            "🚨 [EMERGENCY GLOBAL CANCEL] Nuking ALL resting orders. "
            "This should only trigger from circuit breaker or operator action!"
        )
        try:
            res = self.sync_client.cancel_all()
            _audit_log("cancel_all", res)
        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")

    async def place_limit_order(
        self, token_id: str, side: str, price: float, size: float, post_only: bool = True,
    ) -> Optional[str]:
        """Place a limit order. Returns order_id on success, None on failure."""

        def _build_and_post():
            order_args = OrderArgs(price=price, size=size, side=side, token_id=token_id)
            signed_order = self.sync_client.create_order(order_args)
            return self.sync_client.post_order(signed_order, OrderType.GTC, post_only=post_only)

        try:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, _build_and_post)

            _audit_log(f"place_order_{side}", res)

            if res and res.get("success"):
                order_id = res.get("orderID", "")
                logger.info(
                    f"✅ [bold green]Order Placed![/bold green] {side} {size:.2f} @ ${price:.3f} "
                    f"(ID: {order_id[:8]}…)",
                    extra={"markup": True},
                )
                return order_id
            else:
                logger.error(f"❌ Order Rejected: {res.get('errorMsg', res)}")
                return None
        except Exception as e:
            logger.error(f"💥 Order API Exception: {e}")
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Targeted cancellation of a specific order."""

        def _cancel():
            return self.sync_client.cancel(order_id)

        try:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, _cancel)
            _audit_log(f"cancel_{order_id[:8]}", res)

            if res:
                logger.debug(f"🗑️ Cancelled order {order_id[:8]}…")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    # ── resolution ───────────────────────────────────────────────────────

    async def check_resolution(self, slug: str) -> Optional[str]:
        url = f"https://gamma-api.polymarket.com/events?slug={slug}&closed=true"
        data = await _fetch_with_retry(self._session, url)

        if not data or len(data) == 0:
            return None

        try:
            m = data[0].get("markets", [{}])[0]
            if m.get("closed"):
                prices = m.get("outcomePrices", [])
                tokens = m.get("clobTokenIds", [])
                if isinstance(tokens, str):
                    tokens = json.loads(tokens)
                if isinstance(prices, str):
                    prices = json.loads(prices)

                token_ids = [t["token_id"] if isinstance(t, dict) else t for t in tokens]

                for idx, p in enumerate(prices):
                    if float(p) >= 0.99 or p == "1":
                        return token_ids[idx]
        except (IndexError, KeyError, TypeError) as e:
            logger.debug(f"Resolution parse error for {slug}: {e}")

        return None
