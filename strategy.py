"""BTC trading strategy with volatility-adjusted thresholds and Decimal precision.

Fixes applied from audit:
- C2: Uses config.DRY_RUN (module attr) not imported constant
- C3: live_orders is dict[str, dict[str, str]] for multi-order tracking
- C4: Global exposure limit enforced before entry
- H3: process_pending_orders called for ALL tokens with pending orders
- H4: Trend reversal continue only fires after stop-loss executes
- M2: EMA uses manual recurrence, no DataFrame allocation
"""
import logging
import time
import statistics
import config
from typing import Optional
from decimal import Decimal, ROUND_DOWN

from config import (
    SHORT_EMA_PERIOD, LONG_EMA_PERIOD,
    TRADE_SIZE_USD, MAX_POSITION_USD,
    VOLATILITY_K,
)

logger = logging.getLogger(__name__)

D = Decimal
ZERO = D("0")
TICK = D("0.001")


class BTCStrategy:
    def __init__(self, portfolio):
        self.portfolio = portfolio
        self.price_history: list[float] = []
        self.last_trend = "NEUTRAL"
        self.last_sell_prices: dict[str, Decimal] = {}
        # C3 fix: token_id -> {side: order_id} to track multiple orders per token
        self.live_orders: dict[str, dict[str, str]] = {}
        self._ema_cache: dict[int, float] = {}  # period -> last EMA value

    # ── EMA (M2 fix: manual recurrence, no DataFrame) ────────────────────

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

    # ── volatility ───────────────────────────────────────────────────────

    def _get_volatility(self) -> float:
        """Rolling stddev of last 30 oracle prices (USD terms)."""
        if len(self.price_history) < 5:
            return 0.0
        return statistics.stdev(self.price_history[-30:])

    def _entry_threshold(self) -> float:
        """Dynamic entry threshold: max(2.0, k * volatility)."""
        vol = self._get_volatility()
        return max(2.0, VOLATILITY_K * vol)

    # ── trend ────────────────────────────────────────────────────────────

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

    # ── maker price ──────────────────────────────────────────────────────

    def calculate_safe_maker_price(self, best_bid: float, best_ask: float, tick_size=0.01) -> Optional[Decimal]:
        spread = best_ask - best_bid

        if spread <= tick_size:
            limit_price = D(str(best_bid)).quantize(TICK, rounding=ROUND_DOWN)
        else:
            limit_price = (D(str(best_bid)) + TICK).quantize(TICK, rounding=ROUND_DOWN)

        if limit_price > D("0.95") or limit_price < D("0.05"):
            logger.warning(f"Market skewed ({limit_price}). Rejecting on 5m risk/reward.")
            return None

        return limit_price

    # ── live order helpers (C3 fix) ──────────────────────────────────────

    def _track_order(self, token_id: str, side: str, order_id: str):
        if token_id not in self.live_orders:
            self.live_orders[token_id] = {}
        self.live_orders[token_id][side] = order_id

    def _get_order_id(self, token_id: str, side: str = None) -> Optional[str]:
        orders = self.live_orders.get(token_id, {})
        if side:
            return orders.get(side)
        # Return any order for this token
        return next(iter(orders.values()), None) if orders else None

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

    # ── main brain ───────────────────────────────────────────────────────

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
        is_dry = config.DRY_RUN  # C2 fix: read module attribute, not import-time constant

        best_bid = orderbook_res.get("bid", 0.0)
        best_ask = orderbook_res.get("ask", 1.0)

        if best_bid == 0.0 and best_ask == 1.0:
            logger.warning("Orderbook data invalid or empty. Skipping execution.")
            return

        # 0. Adverse Selection Simulator — process ALL pending tokens (H3 fix)
        if is_dry:
            pending_tokens = {o.token_id for o in self.portfolio.pending_orders}
            for tok in pending_tokens:
                if tok == target_token:
                    self.portfolio.process_pending_orders(tok, best_bid, best_ask)
                else:
                    tok_book = await pm_client.fetch_orderbook(tok)
                    self.portfolio.process_pending_orders(tok, tok_book.get("bid", 0.0), tok_book.get("ask", 1.0))

        limit_price = self.calculate_safe_maker_price(best_bid, best_ask)
        if not limit_price:
            return

        existing_positions = self.portfolio.get_positions_for_market(active_market["condition_id"])
        pending_orders = self.portfolio.pending_orders

        # ── Order Manager (Chase the Price) ──────────────────────────────
        for order in list(pending_orders):
            if order.token_id != target_token:
                logger.info(f"🔄 Canceling {order.side} maker order: Trend switched.")
                if is_dry:
                    self.portfolio.cancel_pending(order)
                else:
                    await self._cancel_token_orders(pm_client, order.token_id)
                continue

            if time.time() - order.timestamp > 1.5:
                if order.action == "BUY" and best_bid > float(order.limit_price) + 0.005:
                    logger.info(
                        f"💨 Order Manager: Canceling staled BUY at ${order.limit_price}, "
                        f"Bid ran to ${best_bid:.3f}"
                    )
                    if is_dry:
                        self.portfolio.cancel_pending(order)
                    else:
                        await self._cancel_token_orders(pm_client, order.token_id, "BUY")

                elif order.action == "SELL" and best_ask < float(order.limit_price) - 0.005:
                    logger.info(
                        f"💨 Order Manager: Canceling staled SELL at ${order.limit_price}, "
                        f"Ask crashed to ${best_ask:.3f}"
                    )
                    if is_dry:
                        self.portfolio.cancel_pending(order)
                    else:
                        await self._cancel_token_orders(pm_client, order.token_id, "SELL")

        # ── 1. Position Management ───────────────────────────────────────
        for pos in existing_positions:
            held_book = await pm_client.fetch_orderbook(pos.token_id)
            held_bid = held_book.get("bid", 0.0)
            held_ask = held_book.get("ask", 1.0)

            held_bid_d = D(str(held_bid))

            # 1a. Hard 15% Price Crash Stop Loss
            if held_bid_d < pos.entry_price * D("0.85"):
                sell_limit = held_bid_d.quantize(TICK, rounding=ROUND_DOWN)
                logger.info(
                    f"💀 [bold red][HARD STOP LOSS][/bold red] Price Crashed >15%. "
                    f"Bailing out of {pos.side} at TAKER ${sell_limit}.",
                    extra={"markup": True},
                )
                if is_dry:
                    self.portfolio.execute_sell(pos, sell_limit, reason="Hard Stop Loss", is_taker=True, signal_diff=diff)
                else:
                    await self._cancel_token_orders(pm_client, pos.token_id)
                    order_id = await pm_client.place_limit_order(
                        pos.token_id, "SELL", float(sell_limit), float(pos.num_shares), post_only=False,
                    )
                    if order_id:
                        self._track_order(pos.token_id, "SELL", order_id)
                self.last_sell_prices[pos.token_id] = sell_limit
                continue

            # 1b. Trend Reversal Stop Loss (H4 fix: continue only if SL fires)
            if pos.token_id != target_token:
                if pos.entry_price - held_bid_d >= D("0.05"):
                    sell_limit = held_bid_d.quantize(TICK, rounding=ROUND_DOWN)
                    logger.info(
                        f"💀 [bold red][STOP LOSS][/bold red] Trend Reversed. "
                        f"Bailing out of {pos.side} at TAKER ${sell_limit}.",
                        extra={"markup": True},
                    )
                    if is_dry:
                        self.portfolio.execute_sell(pos, sell_limit, reason="Stop Loss", is_taker=True, signal_diff=diff)
                    else:
                        await self._cancel_token_orders(pm_client, pos.token_id)
                        order_id = await pm_client.place_limit_order(
                            pos.token_id, "SELL", float(sell_limit), float(pos.num_shares), post_only=False,
                        )
                        if order_id:
                            self._track_order(pos.token_id, "SELL", order_id)
                    self.last_sell_prices[pos.token_id] = sell_limit
                    continue  # H4 fix: only continue AFTER stop-loss fires
                # H4 fix: if SL didn't fire, fall through to take-profit below

            # 1c. Take Profit (Maker) — now reachable for counter-trend positions
            best_bid_d = D(str(best_bid)) if pos.token_id == target_token else held_bid_d
            if pos.entry_price < best_bid_d:
                if pos.token_id == target_token:
                    raw_sell = best_ask if (best_ask - best_bid) <= 0.01 else (best_ask - 0.001)
                else:
                    raw_sell = held_ask if (held_ask - held_bid) <= 0.01 else (held_ask - 0.001)
                sell_limit = D(str(raw_sell)).quantize(TICK, rounding=ROUND_DOWN)

                fee_offset = D("0.015") if getattr(pos, "is_taker", False) else ZERO
                profit_margin = ((sell_limit - pos.entry_price) / pos.entry_price) - fee_offset

                if profit_margin >= D("0.03"):
                    logger.info(
                        f"💰 [bold green][TAKE PROFIT][/bold green] Up {float(profit_margin * D('100')):.1f}%. "
                        f"Selling {pos.side} at MAKER ${sell_limit}",
                        extra={"markup": True},
                    )
                    if is_dry:
                        self.portfolio.execute_sell(pos, sell_limit, reason="Take Profit", is_taker=False, signal_diff=diff)
                    else:
                        await self._cancel_token_orders(pm_client, pos.token_id, "SELL")
                        order_id = await pm_client.place_limit_order(
                            pos.token_id, "SELL", float(sell_limit), float(pos.num_shares),
                        )
                        if order_id:
                            self._track_order(pos.token_id, "SELL", order_id)
                    self.last_sell_prices[pos.token_id] = sell_limit

        # ── 2. Entry ─────────────────────────────────────────────────────
        has_position = any(p.token_id == target_token for p in existing_positions)
        has_pending = any(o.token_id == target_token for o in pending_orders)

        if not has_position and not has_pending:
            # C4 fix: Global exposure limit enforcement
            total_exposure = sum(p.amount_usd for p in self.portfolio.open_positions)
            pending_exposure = sum(
                o.amount_usd for o in self.portfolio.pending_orders if o.action == "BUY"
            )

            # Smart FOMO Re-Entry
            if target_token in self.last_sell_prices:
                if limit_price > self.last_sell_prices[target_token]:
                    logger.debug(
                        f"Smart Re-entry: blocking buy at ${limit_price} > "
                        f"last sell ${self.last_sell_prices[target_token]}"
                    )
                    return

            # Hysteresis (dynamic threshold)
            entry_thresh = self._entry_threshold()
            if abs(diff) < entry_thresh:
                logger.debug(
                    f"Hysteresis active: Diff {abs(diff):.2f} < threshold {entry_thresh:.2f}"
                )
                return

            # Dynamic Spread Control
            spread = best_ask - best_bid
            if spread > 0.08:
                logger.warning(f"🚧 Spread too wide ({spread:.3f} > 0.08). Refusing entry.")
                return

            # OFI
            bids = orderbook_res.get("bids", [])
            asks = orderbook_res.get("asks", [])
            total_bid_vol = sum(float(b.get("size", 0)) for b in bids)
            total_ask_vol = sum(float(a.get("size", 0)) for a in asks)
            total_vol = total_bid_vol + total_ask_vol

            ofi = total_bid_vol / total_vol if total_vol > 0 else 0.5
            if ofi < 0.30:
                logger.warning(
                    f"🧱 [bold yellow]OFI BLOCK:[/bold yellow] OFI={ofi:.2f}. Vetoing signal.",
                    extra={"markup": True},
                )
                return

            is_taker = False
            post_only = True

            # Taker Hybrid Override
            vol = self._get_volatility()
            taker_threshold = max(4.0, 4.0 * vol) if vol > 0 else 4.0
            if abs(diff) > taker_threshold:
                logger.warning(
                    f"🚀 [bold red]MASSIVE MOMENTUM (Diff: {diff:.2f}) - TAKER OVERRIDE[/bold red]",
                    extra={"markup": True},
                )
                is_taker = True
                post_only = False
                limit_price = D(str(best_ask)).quantize(TICK, rounding=ROUND_DOWN)
                trade_size_usd = min(TRADE_SIZE_USD * 4, MAX_POSITION_USD)
                logger.info(f"🔥 Sniper Mode: SCALING to ${trade_size_usd}")
            else:
                trade_size_usd = TRADE_SIZE_USD

            # C4 fix: Enforce MAX_POSITION_USD
            if total_exposure + pending_exposure + trade_size_usd > MAX_POSITION_USD:
                remaining = MAX_POSITION_USD - total_exposure - pending_exposure
                if remaining < TRADE_SIZE_USD / 2:
                    logger.warning(
                        f"🚧 [bold yellow]EXPOSURE LIMIT:[/bold yellow] ${total_exposure + pending_exposure} / "
                        f"${MAX_POSITION_USD}. Refusing entry.",
                        extra={"markup": True},
                    )
                    return
                trade_size_usd = remaining.quantize(D("0.01"), rounding=ROUND_DOWN)
                logger.info(f"📏 Sizing capped to ${trade_size_usd} (exposure limit)")

            if is_dry:
                self.portfolio.execute_buy(
                    active_market["title"], active_market["condition_id"],
                    target_token, target_side, trade_size_usd, limit_price,
                    is_taker=is_taker, signal_diff=diff,
                )
            else:
                logger.info(
                    f"🚀 [bold magenta][LIVE TRADING][/bold magenta] Limit BUY ${trade_size_usd} | "
                    f"{target_side} @ ${limit_price} (Post-Only: {post_only})",
                    extra={"markup": True},
                )
                await self._cancel_token_orders(pm_client, target_token, "BUY")
                target_size = float((trade_size_usd / limit_price).quantize(D("0.01"), rounding=ROUND_DOWN))
                order_id = await pm_client.place_limit_order(
                    target_token, "BUY", float(limit_price), target_size, post_only=post_only,
                )
                if order_id:
                    self._track_order(target_token, "BUY", order_id)
        else:
            logger.debug(f"🔒 Holding or Pending {target_side}. Waiting...")
