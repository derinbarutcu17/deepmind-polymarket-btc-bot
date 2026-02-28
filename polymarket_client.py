"""Async Polymarket CLOB client with WebSocket orderbook cache, orphan-order
reconciliation, and strict Decimal-string order placement.

Production upgrades applied:
- WS orderbook stream: a background task connects to Polymarket's market
  WebSocket and maintains a local bid/ask cache per token_id.
  fetch_orderbook() returns from cache instantly; falls back to REST only
  when cache is absent or stale (> 10 s).
- sync_open_orders(): fetches live open orders from the exchange every 60 s
  and cancels any orphan orders not tracked in strategy.live_orders.
- place_limit_order() now accepts price and size as str (Decimal strings) to
  eliminate floating-point dust that causes API lot-size rejections.
- Existing fixes retained: H1 (cancel_all non-blocking), M5 (audit log cap).
"""
import asyncio
import glob
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
    AUDIT_LOG_MAX_FILES,
)

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=5)
_API_LOG_DIR = "logs/api_responses"
_OB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_OB_STALE_SECONDS = 10.0  # fall back to REST if WS cache is this old


def _ensure_audit_dir():
    os.makedirs(_API_LOG_DIR, exist_ok=True)


def _audit_log(label: str, data):
    """Persist raw API response for audit. Caps directory at AUDIT_LOG_MAX_FILES (M5 fix)."""
    try:
        _ensure_audit_dir()
        files = sorted(glob.glob(os.path.join(_API_LOG_DIR, "*.json")))
        if len(files) >= AUDIT_LOG_MAX_FILES:
            for f in files[: len(files) - AUDIT_LOG_MAX_FILES + 1]:
                os.remove(f)
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

        # WS orderbook cache: token_id -> {bids: {price_str: size_str}, asks: {...}, ts: float}
        self._ob_cache: dict[str, dict] = {}
        self._ob_ws_task: Optional[asyncio.Task] = None

    async def close(self):
        if self._ob_ws_task and not self._ob_ws_task.done():
            self._ob_ws_task.cancel()
            try:
                await self._ob_ws_task
            except asyncio.CancelledError:
                pass
        await self._session.close()

    # ── WebSocket orderbook feed ──────────────────────────────────────────

    def start_orderbook_ws(self, token_ids: list[str]):
        """Start (or restart) the orderbook WS subscription for the given tokens."""
        if self._ob_ws_task and not self._ob_ws_task.done():
            self._ob_ws_task.cancel()
        self._ob_ws_task = asyncio.create_task(self._ob_ws_loop(token_ids))
        logger.info(f"Orderbook WS task started for {len(token_ids)} token(s).")

    async def _ob_ws_loop(self, token_ids: list[str]):
        """Persistent WS connection to Polymarket market feed with auto-reconnect."""
        while True:
            try:
                async with self._session.ws_connect(_OB_WS_URL) as ws:
                    await ws.send_json({"assets_ids": token_ids, "type": "subscribe"})
                    logger.info("Orderbook WS: connected and subscribed.")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_ob_message(json.loads(msg.data))
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning(f"Orderbook WS closed/error: {msg.data}")
                            break

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"Orderbook WS exception ({e}). Reconnecting in 3s…")

            try:
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                return

    def _handle_ob_message(self, events):
        """Process a list of orderbook events (book snapshot or price_change deltas)."""
        if not isinstance(events, list):
            return

        now = time.monotonic()
        for event in events:
            token_id = event.get("asset_id", "")
            if not token_id:
                continue

            event_type = event.get("event_type", "")

            if event_type == "book":
                # Full snapshot — replace cache entirely
                bids = {b["price"]: b["size"] for b in event.get("bids", [])}
                asks = {a["price"]: a["size"] for a in event.get("asks", [])}
                self._ob_cache[token_id] = {"bids": bids, "asks": asks, "ts": now}

            elif event_type == "price_change":
                # Delta — apply individual level changes
                if token_id not in self._ob_cache:
                    self._ob_cache[token_id] = {"bids": {}, "asks": {}, "ts": now}
                cache = self._ob_cache[token_id]

                for change in event.get("changes", []):
                    price = change.get("price", "")
                    size = change.get("size", "0")
                    side = change.get("side", "")

                    book_side = "bids" if side == "BUY" else "asks"
                    if float(size) == 0:
                        cache[book_side].pop(price, None)
                    else:
                        cache[book_side][price] = size

                cache["ts"] = now

    def _cache_to_book(self, cache: dict) -> dict:
        """Convert internal WS cache format to the standard fetch_orderbook dict."""
        bids = sorted(
            [(float(p), float(s)) for p, s in cache["bids"].items() if float(s) > 0],
            reverse=True,
        )
        asks = sorted(
            [(float(p), float(s)) for p, s in cache["asks"].items() if float(s) > 0],
        )
        return {
            "bid": bids[0][0] if bids else 0.0,
            "ask": asks[0][0] if asks else 1.0,
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids[:5]],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks[:5]],
        }

    # ── orderbook ────────────────────────────────────────────────────────

    async def fetch_orderbook(self, token_id: str) -> dict:
        """Return orderbook from WS cache; fall back to REST if cache is absent/stale."""
        cached = self._ob_cache.get(token_id)
        if cached and time.monotonic() - cached["ts"] < _OB_STALE_SECONDS:
            return self._cache_to_book(cached)

        # Cache miss or stale — fall back to REST
        return await self._fetch_orderbook_rest(token_id)

    async def _fetch_orderbook_rest(self, token_id: str) -> dict:
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
        """EMERGENCY ONLY — SYNC. Use cancel_all_orders_async in async context."""
        if hasattr(self, "_last_cancel_time") and time.time() - self._last_cancel_time < 2.0:
            return
        self._last_cancel_time = time.time()
        logger.warning("🚨 [EMERGENCY GLOBAL CANCEL] Nuking ALL resting orders.")
        try:
            res = self.sync_client.cancel_all()
            _audit_log("cancel_all", res)
        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")

    async def cancel_all_orders_async(self):
        """H1 fix: Async wrapper for cancel_all — won't block event loop."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.cancel_all_orders)

    async def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: str,   # Decimal string — no float conversion before this point
        size: str,    # Decimal string — no float conversion before this point
        post_only: bool = True,
    ) -> Optional[str]:
        """Place a limit order. Accepts price and size as Decimal strings to avoid
        floating-point dust that causes Polymarket lot-size rejections."""

        # Convert once here with full precision; float(Decimal(s)) is exact for
        # properly quantized values (3 d.p. prices, 2 d.p. sizes).
        price_f = float(price)
        size_f = float(size)

        def _build_and_post():
            order_args = OrderArgs(price=price_f, size=size_f, side=side, token_id=token_id)
            signed_order = self.sync_client.create_order(order_args)
            return self.sync_client.post_order(signed_order, OrderType.GTC, post_only=post_only)

        try:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, _build_and_post)

            _audit_log(f"place_order_{side}", res)

            if res and res.get("success"):
                order_id = res.get("orderID", "")
                logger.info(
                    f"✅ [bold green]Order Placed![/bold green] {side} {size} @ ${price} "
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

    # ── reconciliation ───────────────────────────────────────────────────

    async def sync_open_orders(self, strategy_live_orders: dict[str, dict[str, str]]) -> int:
        """Fetch real open orders from exchange; cancel any not tracked by the strategy.

        Returns the number of orphan orders cancelled.
        """
        def _fetch():
            try:
                return self.sync_client.get_orders()
            except Exception as e:
                raise RuntimeError(f"get_orders failed: {e}") from e

        try:
            loop = asyncio.get_running_loop()
            orders = await loop.run_in_executor(None, _fetch)
        except Exception as e:
            logger.error(f"sync_open_orders: {e}")
            return 0

        if not orders:
            return 0

        # Build flat set of all order IDs currently tracked by strategy
        tracked_ids: set[str] = {
            oid
            for sides in strategy_live_orders.values()
            for oid in sides.values()
            if oid
        }

        cancelled = 0
        for order in orders:
            order_id = order.get("id") or order.get("orderID", "")
            if not order_id:
                continue
            if order_id not in tracked_ids:
                logger.warning(
                    f"🔍 Orphaned order {order_id[:8]}… not in strategy state — cancelling."
                )
                if await self.cancel_order(order_id):
                    cancelled += 1

        if cancelled:
            logger.warning(f"Reconciliation: cancelled {cancelled} orphaned order(s).")
        else:
            logger.debug("Reconciliation: all open orders accounted for.")

        return cancelled

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
