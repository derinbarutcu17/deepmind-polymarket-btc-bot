"""BTC trading strategy focused on high-frequency "Static Sniper" execution using static thresholds.

Key Features:
- Replaces dynamic volatility scaling with fixed, responsive thresholds.
- Streamlined Maker/Taker logic based on EMA momentum (diff).
- Conservative re-entry protection and OFI volume-intensity filtering.

Production upgrades:
- Per-token asyncio.Lock: each execution block (entry, TP, SL) acquires the
  token's lock before any awaited API call, so a lagging network response
  from tick N cannot race with tick N+1 for the same token.
- Decimal-string coercion: place_limit_order receives price and size as
  properly quantized Decimal strings — no float() precision loss.
"""
import asyncio
import logging
import time
import config
from typing import Optional
from decimal import Decimal, ROUND_DOWN

from config import (
    SHORT_EMA_PERIOD, LONG_EMA_PERIOD,
    TRADE_SIZE_USD, MAX_POSITION_USD
)

logger = logging.getLogger(__name__)

D = Decimal
ZERO = D("0")
TICK = D("0.001")
SIZE_TICK = D("0.01")


class BTCStrategy:
    def __init__(self, portfolio):
        self.portfolio = portfolio
        self.price_history: list[float] = []
        self.last_trend = "NEUTRAL"
        self.last_sell_prices: dict[str, Decimal] = {}
        # token_id -> {side: order_id} to track multiple orders per token
        self.live_orders: dict[str, dict[str, str]] = {}
        self._ema_cache: dict[int, float] = {}  # period -> last EMA value
        self.stop_cooldowns: dict[str, float] = {}
        # Per-token execution locks — prevent duplicate orders during network lag
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, token_id: str) -> asyncio.Lock:
        if token_id not in self._locks:
            self._locks[token_id] = asyncio.Lock()
        return self._locks[token_id]

    # ── EMA (Optimized Manual Recurrence) ────────────────────────────────

    def _calculate_ema(self, prices: list[float], period: int) -> float:
        if len(prices) < period:
            return sum(prices) / len(prices) if prices else 0.0
        k = 2.0 / (period + 1)
        cache_key = period
        if cache_key in self._ema_cache and len(prices) > 1:
            prev_ema = self._ema_cache[cache_key]
            ema = prices[-1] * k + prev_ema * (1 - k)
        else:
            ema = prices[0]
            for p in prices[1:]:
                ema = p * k + ema * (1 - k)
        self._ema_cache[cache_key] = ema
        return ema

    # ── Trend Detection (Static Thresholds) ──────────────────────────────

    def get_trend(self, current_price: float) -> tuple[str, float]:
        self.price_history.append(current_price)
        if len(self.price_history) > 60:
            self.price_history.pop(0)

        if len(self.price_history) < LONG_EMA_PERIOD:
            return "NEUTRAL", 0.0

        short_ema = self._calculate_ema(self.price_history[-SHORT_EMA_PERIOD:], SHORT_EMA_PERIOD)
        long_ema = self._calculate_ema(self.price_history, LONG_EMA_PERIOD)

        diff = short_ema - long_ema

        if diff > 0.5:
            current_trend = "UP"
        elif diff < -0.5:
            current_trend = "DOWN"
        else:
            current_trend = "NEUTRAL"

        if current_trend != "NEUTRAL" and current_trend != self.last_trend:
            logger.info(
                f"📈 [bold cyan]MOMENTUM SHIFT:[/bold cyan] {self.last_trend} -> {current_trend} (Diff: {diff:.2f})",
                extra={"markup": True},
            )

        self.last_trend = current_trend
        return current_trend, diff

    # ── Price Calculation ────────────────────────────────────────────────

    def calculate_safe_maker_price(self, best_bid: float, best_ask: float, tick_size=0.01) -> Optional[Decimal]:
        spread = D(str(best_ask)) - D(str(best_bid))
        tick_d = D(str(tick_size))

        if spread <= tick_d:
            limit_price = D(str(best_bid)).quantize(TICK, rounding=ROUND_DOWN)
        else:
            limit_price = (D(str(best_bid)) + TICK).quantize(TICK, rounding=ROUND_DOWN)

        # Block deeply out-of-the-money (worthless) tokens — < $0.05 means near zero chance
        if limit_price < D("0.05"):
            return None

        # Block deeply in-the-money tokens — > $0.92 means less than 8% upside to $1.00,
        # not worth the volatility risk for a short-term trade
        if limit_price > D("0.92"):
            return None

        return limit_price

    # ── Live Order Helpers ───────────────────────────────────────────────

    def _track_order(self, token_id: str, side: str, order_id: str):
        if token_id not in self.live_orders:
            self.live_orders[token_id] = {}
        self.live_orders[token_id][side] = order_id

    async def _cancel_token_orders(self, pm_client, token_id: str, side: str = None):
        """Cancel specific side or all orders for a token."""
        orders = self.live_orders.get(token_id, {})
        if not orders:
            return
        if side:
            oid = orders.pop(side, None)
            if oid:
                await pm_client.cancel_order(oid)
        else:
            for s, oid in list(orders.items()):
                await pm_client.cancel_order(oid)
            self.live_orders.pop(token_id, None)

    # ── Strategy Evaluation ──────────────────────────────────────────────

    async def evaluate_and_execute(
        self,
        pm_client,
        active_market: dict,
        oracle_res: dict,
        orderbook_res: dict,
        diff: float,
        target_token: str,
        target_side: str,
    ):
        is_dry = config.DRY_RUN

        best_bid = orderbook_res.get("bid", 0.0)
        best_ask = orderbook_res.get("ask", 1.0)

        if best_bid == 0.0 and best_ask == 1.0:
            logger.info("⛔ GATE 1: Orderbook empty (bid=0, ask=1) — WS cache not seeded yet or REST failed.")
            return

        # Adverse Selection Simulator (Dry Run Only)
        if is_dry:
            pending_tokens = {o.token_id for o in self.portfolio.pending_orders}
            for tok in pending_tokens:
                if tok == target_token:
                    self.portfolio.process_pending_orders(tok, best_bid, best_ask)

        limit_price = self.calculate_safe_maker_price(best_bid, best_ask)
        if not limit_price:
            logger.info(f"⛔ GATE 2: Price zone blocked (bid={best_bid:.3f}, ask={best_ask:.3f}) — token OTM or deeply ITM")
            return

        existing_positions = self.portfolio.get_positions_for_market(active_market["condition_id"])
        pending_orders = self.portfolio.pending_orders

        # ── Order Manager (Price Chasing) ────────────────────────────────
        for order in list(pending_orders):
            if order.token_id != target_token:
                logger.info(f"🔄 Canceling {order.side} maker order: Trend switched.")
                if is_dry:
                    self.portfolio.cancel_pending(order)
                else:
                    await self._cancel_token_orders(pm_client, order.token_id)
                continue

            if time.time() - order.timestamp > 4.0:
                if order.action == "BUY" and best_bid > float(order.limit_price) + 0.02:
                    logger.info(f"💨 Chase: Canceling staled BUY at ${order.limit_price} (bid moved to {best_bid:.3f})")
                    if is_dry:
                        self.portfolio.cancel_pending(order)
                    else:
                        await self._cancel_token_orders(pm_client, order.token_id, "BUY")
            if time.time() - order.timestamp > 15.0:
                if order.action == "SELL" and best_ask < float(order.limit_price) - 0.005:
                    logger.info(f"💨 Chase: Canceling staled SELL at ${order.limit_price}")
                    if is_dry:
                        self.portfolio.cancel_pending(order)
                    else:
                        await self._cancel_token_orders(pm_client, order.token_id, "SELL")

        # ── 1. Position Management ───────────────────────────────────────
        for pos in existing_positions:
            pos_lock = self._get_lock(pos.token_id)
            if pos_lock.locked():
                logger.debug(f"⏭️  pos {pos.token_id[:8]}… locked — skipping tick")
                continue

            held_book = await pm_client.fetch_orderbook(pos.token_id)
            held_bid = held_book.get("bid", 0.0)
            held_ask = held_book.get("ask", 1.0)
            held_bid_d = D(str(held_bid))

            # 1a. Hard Stop Loss — use MID price to avoid bid-ask spread noise
            # The spread on thin books can be 5-10 cents; using raw bid caused
            # immediate stop-outs on the tick after entry. Mid = (bid+ask)/2.
            held_mid_d = (held_bid_d + D(str(held_ask))) / D("2")
            if held_mid_d < pos.entry_price * D("0.78"):
                sell_limit = held_bid_d.quantize(TICK, rounding=ROUND_DOWN)  # still sell at bid
                logger.info(
                    f"💀 [bold red]HARD STOP LOSS[/bold red] Price Crashed. DUMPING at ${sell_limit}.",
                    extra={"markup": True},
                )
                if is_dry:
                    self.portfolio.execute_sell(pos, sell_limit, reason="Hard Stop", is_taker=True)
                else:
                    async with pos_lock:
                        await self._cancel_token_orders(pm_client, pos.token_id)
                        await pm_client.place_limit_order(
                            pos.token_id,
                            "SELL",
                            str(sell_limit),
                            str(pos.num_shares.quantize(SIZE_TICK, rounding=ROUND_DOWN)),
                            post_only=False,
                        )
                self.last_sell_prices[pos.token_id] = sell_limit  # block re-entry above stop price
                self.stop_cooldowns[pos.condition_id] = time.time()
                continue

            # 1b. Trend Reversal Stop Loss — 25% drop from entry (was 15%, too trigger-happy)
            # Also uses mid price to ignore spread noise.
            if pos.token_id != target_token:
                if pos.entry_price - held_mid_d >= pos.entry_price * D("0.25"):
                    sell_limit = held_bid_d.quantize(TICK, rounding=ROUND_DOWN)
                    logger.info(
                        f"💀 [bold red]STOP LOSS[/bold red] Trend Reversed. DUMPING at ${sell_limit}.",
                        extra={"markup": True},
                    )
                    if is_dry:
                        self.portfolio.execute_sell(pos, sell_limit, reason="Stop Loss", is_taker=True)
                    else:
                        async with pos_lock:
                            await self._cancel_token_orders(pm_client, pos.token_id)
                            await pm_client.place_limit_order(
                                pos.token_id,
                                "SELL",
                                str(sell_limit),
                                str(pos.num_shares.quantize(SIZE_TICK, rounding=ROUND_DOWN)),
                                post_only=False,
                            )
                    self.last_sell_prices[pos.token_id] = sell_limit  # block re-entry above stop price
                    self.stop_cooldowns[pos.condition_id] = time.time()
                    continue

            # 1c. Take Profit
            check_bid_d = D(str(best_bid)) if pos.token_id == target_token else held_bid_d

            if pos.entry_price < check_bid_d:
                target_ask = best_ask if pos.token_id == target_token else held_ask
                target_bid = best_bid if pos.token_id == target_token else held_bid

                raw_sell = target_ask if (target_ask - target_bid) <= 0.01 else (target_ask - 0.001)
                sell_limit = D(str(raw_sell)).quantize(TICK, rounding=ROUND_DOWN)

                fee_offset = D("0.015") if getattr(pos, "is_taker", False) else ZERO
                profit_margin = ((sell_limit - pos.entry_price) / pos.entry_price) - fee_offset

                if profit_margin >= D("0.03"):
                    already_has_tp = any(
                        o.action == "SELL" and o.token_id == pos.token_id
                        for o in pending_orders
                    )
                    if already_has_tp:
                        continue
                    logger.info(
                        f"💰 [bold green]TAKE PROFIT[/bold green] (+{float(profit_margin*100):.1f}%). Limit Sell: ${sell_limit}",
                        extra={"markup": True},
                    )
                    if is_dry:
                        self.portfolio.execute_sell(pos, sell_limit, reason="Take Profit", is_taker=False)
                    else:
                        if pos_lock.locked():
                            logger.debug(f"⏭️  TP skipped: {pos.token_id[:8]}… lock held")
                            continue
                        async with pos_lock:
                            await self._cancel_token_orders(pm_client, pos.token_id, "SELL")
                            order_id = await pm_client.place_limit_order(
                                pos.token_id,
                                "SELL",
                                str(sell_limit),
                                str(pos.num_shares.quantize(SIZE_TICK, rounding=ROUND_DOWN)),
                            )
                            if order_id:
                                self._track_order(pos.token_id, "SELL", order_id)
                    # TP fill: do NOT record last_sell_prices — allow re-entry if trend continues.
                    # The TP cooldown (10s) via stop_cooldowns is enough protection.
                    self.stop_cooldowns[pos.condition_id] = time.time() - 20  # 10s TP cooldown

        # ── 2. Entry ─────────────────────────────────────────────────────
        has_pos = any(p.token_id == target_token for p in existing_positions)
        has_pending = any(o.token_id == target_token for o in pending_orders)

        if not has_pos and not has_pending:
            cooldown_ts = self.stop_cooldowns.get(active_market["condition_id"], 0)
            remaining = 30 - (time.time() - cooldown_ts)
            if remaining > 0:
                logger.info(f"⛔ GATE 3: Cooldown active — {remaining:.0f}s remaining")
                return

            # GATE 4: Minimum upside check — don't enter if max payout < 20%.
            # A token at $0.81 wins $1.00 max = only 23% upside, borderline.
            # A token at $0.85+ means < 18% upside — not worth the entry risk.
            if limit_price > D("0.80"):
                logger.info(f"⛔ GATE 4: Low upside — price ${limit_price} leaves < 20% to $1.00")
                return
            # Also block deeply OTM tokens (< $0.10) — near-zero chance of winning
            if limit_price < D("0.10"):
                logger.info(f"⛔ GATE 4: Deep OTM — price ${limit_price} too cheap, likely losing token")
                return

            # GATE 5 REMOVED: Post-stop re-entry is already handled by the 30s cooldown above.
            # The old last_sell_prices check was blocking profitable follow-on entries after TP fills.
            # last_sell_prices is now only set on STOP LOSS exits to prevent buying back into losers.

            if abs(diff) < 1.0:
                logger.info(f"⛔ GATE 6: Momentum too weak — diff={diff:.2f} (need >=1.0)")
                return

            spread = best_ask - best_bid
            if spread > 0.15:
                logger.info(f"⛔ GATE 7: Spread too wide — {spread:.3f} (limit=0.15, bid={best_bid:.3f}, ask={best_ask:.3f})")
                return

            bids = orderbook_res.get("bids", [])
            asks = orderbook_res.get("asks", [])
            total_bid_vol = sum(float(b.get("size", 0)) for b in bids)
            total_ask_vol = sum(float(a.get("size", 0)) for a in asks)
            total_vol = total_bid_vol + total_ask_vol

            ofi = total_bid_vol / total_vol if total_vol > 0 else 0.5
            if ofi < 0.15:
                logger.info(f"⛔ GATE 8: OFI too low — {ofi:.2f} (bid_vol={total_bid_vol:.1f}, ask_vol={total_ask_vol:.1f})")
                return

            is_taker = False
            post_only = True
            # Taker sniper DISABLED: it was buying extremes (diff>4 = price already moved far)
            # at 4x size and getting hard-stopped within 2 ticks. Pure capital destruction.
            # All entries are now calm maker orders at TRADE_SIZE_USD only.
            trade_size_usd = TRADE_SIZE_USD

            current_exposure = sum(p.amount_usd for p in self.portfolio.open_positions)
            if current_exposure + trade_size_usd > MAX_POSITION_USD:
                return

            if is_dry:
                self.portfolio.execute_buy(
                    active_market["title"], active_market["condition_id"],
                    target_token, target_side, trade_size_usd, limit_price, is_taker=is_taker,
                )
            else:
                entry_lock = self._get_lock(target_token)
                if entry_lock.locked():
                    logger.debug(f"⏭️  Entry skipped: {target_token[:8]}… lock held")
                    return
                async with entry_lock:
                    logger.info(
                        f"🚀 [bold magenta]LIVE BUY[/bold magenta] ${trade_size_usd} {target_side} @ ${limit_price} (Taker: {is_taker})",
                        extra={"markup": True},
                    )
                    await self._cancel_token_orders(pm_client, target_token, "BUY")
                    entry_size = str(
                        (trade_size_usd / limit_price).quantize(SIZE_TICK, rounding=ROUND_DOWN)
                    )
                    order_id = await pm_client.place_limit_order(
                        target_token,
                        "BUY",
                        str(limit_price),
                        entry_size,
                        post_only=post_only,
                    )
                    if order_id:
                        self._track_order(target_token, "BUY", order_id)
