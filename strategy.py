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
from market import PriceBuffer
from typing import Optional
from decimal import Decimal, ROUND_DOWN, ROUND_UP

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
        # Orderbook cache for Signal Blending
        self.price_buffers: dict[str, PriceBuffer] = {}
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
        if len(self.price_history) > LONG_EMA_PERIOD:
            self.price_history.pop(0)

        if len(self.price_history) < LONG_EMA_PERIOD:
            return "NEUTRAL", 0.0

        short_ema = self._calculate_ema(self.price_history[-SHORT_EMA_PERIOD:], SHORT_EMA_PERIOD)
        long_ema = self._calculate_ema(self.price_history, LONG_EMA_PERIOD)

        # Normalize difference to basis points (1 bps = 0.01%)
        diff = ((short_ema - long_ema) / long_ema) * 10000

        if diff > 1.2:
            current_trend = "UP"
        elif diff < -1.2:
            current_trend = "DOWN"
        else:
            current_trend = "NEUTRAL"

        if current_trend != "NEUTRAL" and current_trend != self.last_trend:
            logger.info(
                f"📈 [bold cyan]MOMENTUM SHIFT:[/bold cyan] {self.last_trend} -> {current_trend} (Diff: {diff:.2f} bps)",
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

        if limit_price > D("0.93") or limit_price < D("0.04"):
            logger.info(f"⛔ GATE 2: Price zone blocked (bid={best_bid:.3f}, ask={best_ask:.3f}) — token OTM or deeply ITM")
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
            return

        # Adverse Selection Simulator (Dry Run Only)
        if is_dry:
            pending_tokens = {o.token_id for o in self.portfolio.pending_orders}
            for tok in pending_tokens:
                if tok == target_token:
                    self.portfolio.process_pending_orders(tok, best_bid, best_ask)

        limit_price = self.calculate_safe_maker_price(best_bid, best_ask)
        if not limit_price:
            return

        existing_positions = self.portfolio.get_positions_for_market(active_market["condition_id"])
        pending_orders = self.portfolio.pending_orders

        # ── Order Manager (No Chasing State Machine) ─────────────────────
        for order in list(pending_orders):
            if order.token_id != target_token and order.action == "BUY":
                logger.info(f"🔄 Canceling {order.action} {order.side} maker order: Trend switched.")
                if is_dry:
                    self.portfolio.cancel_pending(order)
                else:
                    await self._cancel_token_orders(pm_client, order.token_id, "BUY")
                continue

            # PHASE 1: NO CHASING. HARD 5 SECOND CANCEL ON BUYS.
            if order.action == "BUY" and time.time() - order.timestamp > 5.0:
                logger.info(f"⏳ Phase 1 State Shift: Limit BUY older than 5s (${order.limit_price}). Canceling to return to HUNTING.")
                if is_dry:
                    self.portfolio.cancel_pending(order)
                else:
                    await self._cancel_token_orders(pm_client, order.token_id, "BUY")

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

            # Minimum hold time: don't fire any stop for 3s after fill,
            # to avoid immediate spread jitter after execution.
            hold_secs = time.time() - pos.entry_time if hasattr(pos, 'entry_time') else 999
            held_mid_d = (held_bid_d + D(str(held_ask))) / D("2")

            # 1a. Hard $0.10 Price Crash Stop Loss (Phase 3 Delta Hedge)
            price_drop = pos.entry_price - held_mid_d
            if hold_secs > 5 and price_drop >= D("0.10"):
                opposite_token = active_market['yes_token'] if pos.token_id == active_market['no_token'] else active_market['no_token']
                opp_book = await pm_client.fetch_orderbook(opposite_token)
                
                # FIX: Target the Ask, not the Bid, to ensure we cross the spread on a reversal.
                opp_ask = opp_book.get("ask", 1.0)
                
                # Add 1 cent to the Ask to guarantee a Taker sweep even if the book shifts by milliseconds
                hedge_limit = (D(str(opp_ask)) + TICK).quantize(TICK, rounding=ROUND_DOWN)
                effective_exit_price = (D("1.0") - hedge_limit).quantize(TICK, rounding=ROUND_DOWN)

                logger.info(
                    f"💀 [bold red]HARD STOP LOSS[/bold red] Price dropped 10+ cents. Executing Delta Hedge! Taker Buying Opposite ID at ${hedge_limit} (Effective Exit: ${effective_exit_price}).",
                    extra={"markup": True},
                )
                if is_dry:
                    self.portfolio.execute_sell(pos, effective_exit_price, reason="Delta Hedge (Hard Stop)", is_taker=True)
                else:
                    async with pos_lock:
                        await self._cancel_token_orders(pm_client, pos.token_id)
                        await pm_client.place_limit_order(
                            opposite_token,  
                            "BUY",
                            str(hedge_limit),
                            str(pos.num_shares.quantize(SIZE_TICK, rounding=ROUND_DOWN)),
                            post_only=False, # FIX: Must be False to act as a Taker
                        )
                        # FIX: Remove the original position from local state so we don't infinite loop
                        if pos in self.portfolio.open_positions:
                            self.portfolio.open_positions.remove(pos)
                            
                self.last_sell_prices[pos.token_id] = hedge_limit
                self.stop_cooldowns[pos.condition_id] = time.time()
                continue

            # 1b. Momentum Reversal Exit — Oracle trend strongly shifted against us
            # We don't need hold_secs > 3 because diff comes from Pyth, not the orderbook spread.
            if pos.token_id != target_token:
                # If diff moves against us by at least 1.5 bps (solid reversal)
                if abs(diff) >= 1.5:
                    opposite_token = active_market['yes_token'] if pos.token_id == active_market['no_token'] else active_market['no_token']
                    opp_book = await pm_client.fetch_orderbook(opposite_token)
                    
                    # FIX: Target the Ask, not the Bid, to ensure we cross the spread on a reversal.
                    opp_ask = opp_book.get("ask", 1.0)
                    
                    # Add 1 cent to the Ask to guarantee a Taker sweep even if the book shifts by milliseconds
                    hedge_limit = (D(str(opp_ask)) + TICK).quantize(TICK, rounding=ROUND_DOWN)
                    effective_exit_price = (D("1.0") - hedge_limit).quantize(TICK, rounding=ROUND_DOWN)

                    logger.info(
                        f"💀 [bold red]MOMENTUM REVERSAL[/bold red] Trend significantly shifted (diff={diff:.2f} bps). Delta Hedging Taker Buy on Opposite Token at ${hedge_limit}.",
                        extra={"markup": True},
                    )
                    if is_dry:
                        self.portfolio.execute_sell(pos, effective_exit_price, reason="Delta Hedge (Reversal)", is_taker=True)
                    else:
                        async with pos_lock:
                            await self._cancel_token_orders(pm_client, pos.token_id)
                            await pm_client.place_limit_order(
                                opposite_token,
                                "BUY",
                                str(hedge_limit),
                                str(pos.num_shares.quantize(SIZE_TICK, rounding=ROUND_DOWN)),
                                post_only=False, # FIX: Must be False to act as a Taker
                            )
                            # FIX: Remove the original position from local state so we don't infinite loop
                            if pos in self.portfolio.open_positions:
                                self.portfolio.open_positions.remove(pos)
                                
                    self.last_sell_prices[pos.token_id] = hedge_limit
                    self.stop_cooldowns[pos.condition_id] = time.time()
                    continue

                # 1b-2. Stale Trade Scratch Exit — Trade has stagnated and momentum died
                elif hold_secs >= 45 and abs(diff) < 1.0:
                    # Clear it out at the bid to escape the dead money
                    sell_limit = max(D("0.01"), held_bid_d - D("0.01")).quantize(TICK, rounding=ROUND_DOWN)
                    logger.info(
                        f"⏳ [bold yellow]STALE TRADE SCRATCH[/bold yellow] Held >45s & weak momentum (diff={diff:.2f} bps). Extricating at ${sell_limit}.",
                        extra={"markup": True},
                    )
                    if is_dry:
                        self.portfolio.execute_sell(pos, sell_limit, reason="Stale Scratch", is_taker=True)
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
                            if pos in self.portfolio.open_positions:
                                self.portfolio.open_positions.remove(pos)
                    self.last_sell_prices[pos.token_id] = sell_limit
                    self.stop_cooldowns[pos.condition_id] = time.time()
                    continue

            # 1c. Queue Take Profit at 10% fixed ratio
            sell_limit = min(D("0.99"), (pos.entry_price * D("1.10")).quantize(TICK, rounding=ROUND_UP))
            already_has_tp = any(
                o.action == "SELL" and o.token_id == pos.token_id
                for o in pending_orders
            )
            
            if not already_has_tp:
                logger.info(
                    f"💰 [bold green]QUEUE TAKE PROFIT[/bold green] (+10%). Limit Sell Maker: ${sell_limit}",
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
                            post_only=True
                        )
                        if order_id:
                            self._track_order(pos.token_id, "SELL", order_id)
                self.stop_cooldowns[pos.condition_id] = time.time() - 20

        # ── 2. Entry ─────────────────────────────────────────────────────
        has_pos = any(p.token_id == target_token for p in existing_positions)
        has_pending = any(o.token_id == target_token for o in pending_orders)

        if target_token not in self.price_buffers:
            self.price_buffers[target_token] = PriceBuffer(maxlen=10)
        self.price_buffers[target_token].add_tick(best_bid, best_ask)

        if not has_pos and not has_pending:
            cooldown_ts = self.stop_cooldowns.get(active_market["condition_id"], 0)
            remaining = 30 - (time.time() - cooldown_ts)
            if remaining > 0:
                logger.info(f"⛔ GATE 3: Cooldown active — {remaining:.0f}s remaining")
                return

            # PHASE 4: Golden Zone ($0.40 - $0.60) exclusively
            if limit_price > D("0.60") or limit_price < D("0.40"):
                logger.info(f"⛔ GATE 4: Outside Golden Zone ($0.40-$0.60) — Price ${limit_price} rejected.")
                return

            # GATE 4b: Time remaining guard — don't enter dying markets
            closes_in = active_market.get("closes_in", 300)
            if closes_in < 90:
                logger.info(f"⛔ GATE 4b: Market expires in {closes_in}s — too late to enter")
                return

            # GATE 5: Don't re-enter same token above stop-out price (stops only)
            if target_token in self.last_sell_prices:
                if limit_price > self.last_sell_prices[target_token]:
                    logger.info(f"⛔ GATE 5: Re-entry blocked — ${limit_price} > last stop ${self.last_sell_prices[target_token]}")
                    return

            if abs(diff) < 1.0:
                logger.info(f"⛔ GATE 6: Momentum too weak — diff={diff:.2f} bps (need >= 1.0 bps)")
                return

            # PHASE 2: Signal Blending (Oracle + Orderbook)
            if not self.price_buffers[target_token].is_micro_pullback(float(limit_price)):
                logger.info(f"⛔ GATE 8: Waiting for Orderbook micro-pullback (Chasing suppressed).")
                return

            spread = best_ask - best_bid
            if spread > 0.05:
                logger.info(f"⛔ GATE 7: Spread too wide — {spread:.3f}")
                return

            bids = orderbook_res.get("bids", [])
            asks = orderbook_res.get("asks", [])
            total_bid_vol = sum(float(b.get("size", 0)) for b in bids)
            total_ask_vol = sum(float(a.get("size", 0)) for a in asks)
            total_vol = total_bid_vol + total_ask_vol

            ofi = total_bid_vol / total_vol if total_vol > 0 else 0.5
            if ofi < 0.15:
                return

            is_taker = False
            post_only = True
            # TAKER SNIPER DISABLED: was buying at market price during extreme
            # moves, consistently entering at the worst price and losing 20-30%.
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
