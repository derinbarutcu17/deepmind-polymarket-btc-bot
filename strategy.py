import logging
import pandas as pd
from typing import Optional
from config import SHORT_EMA_PERIOD, LONG_EMA_PERIOD, DRY_RUN, TRADE_SIZE_USD, MAX_POSITION_USD

logger = logging.getLogger(__name__)

class BTCStrategy:
    def __init__(self, portfolio):
        self.portfolio = portfolio
        self.price_history = []
        self.last_trend = "NEUTRAL"
        self.last_sell_prices = {}
        
    def _calculate_ema(self, prices, periods):
        if len(prices) < periods:
            return sum(prices) / len(prices) if prices else 0
        df = pd.DataFrame(prices, columns=['price'])
        ema = df['price'].ewm(span=periods, adjust=False).mean()
        return ema.iloc[-1]

    def get_trend(self, current_price: float) -> tuple[str, float]:
        """
        Calculates EMA-based momentum using a rolling window of prices.
        """
        self.price_history.append(current_price)
        # Keep window max size close to long EMA period
        if len(self.price_history) > 30:
            self.price_history.pop(0)
            
        if len(self.price_history) < LONG_EMA_PERIOD:
            return "NEUTRAL", 0.0 # Bootstrapping history
            
        short_ema = self._calculate_ema(self.price_history[-SHORT_EMA_PERIOD:], SHORT_EMA_PERIOD)
        long_ema = self._calculate_ema(self.price_history, LONG_EMA_PERIOD)
        
        diff = short_ema - long_ema
        
        # Threshold to avoid flip-flopping on noise
        if diff > 0.5:
            current_trend = "UP"
        elif diff < -0.5:
            current_trend = "DOWN"
        else:
            current_trend = "NEUTRAL"
            
        if current_trend != "NEUTRAL" and current_trend != self.last_trend:
             logger.info(f"📈 [bold cyan]MOMENTUM SHIFT:[/bold cyan] {self.last_trend} -> {current_trend} (Diff: {diff:.2f})", extra={"markup": True})
             
        self.last_trend = current_trend
        return current_trend, diff

    def calculate_safe_maker_price(self, best_bid: float, best_ask: float, tick_size=0.01) -> Optional[float]:
        """
        Calculates a maker price without dropping taking fees by crossing.
        """
        spread = best_ask - best_bid
        
        # If spread is tight, join the queue. Don't cross.
        if spread <= tick_size:
            limit_price = round(best_bid, 3) 
            logger.debug(f"Spread tight ({spread:.3f}). Joining queue at {limit_price}")
        else:
            # Penny jump if there's room
            limit_price = round(best_bid + 0.001, 3)
            logger.debug(f"Spread wide ({spread:.3f}). Front-running at {limit_price}")

        # SAFETY: Never buy > 0.95 or < 0.05 due to terrible risk/reward on 5-minute spans (Relaxed for late trends)
        if limit_price > 0.95 or limit_price < 0.05:
            logger.warning(f"Market skewed ({limit_price:.3f}). Rejecting terrible risk/reward on 5m outcome. Aborting.")
            return None
            
        return limit_price
        
    async def evaluate_and_execute(self, pm_client, active_market: dict, oracle_res: dict, orderbook_res: dict, diff: float, target_token: str, target_side: str):
        """
        Determines position handling based on orderbook logic (Take profit, stop loss, bid limits).
        """
        best_bid = orderbook_res.get('bid', 0.0)
        best_ask = orderbook_res.get('ask', 1.0)
        
        if best_bid == 0.0 and best_ask == 1.0:
            logger.warning("Orderbook data invalid or empty. Skipping execution.")
            return

        # 0. Adverse Selection Simulator Processing
        if DRY_RUN:
            self.portfolio.process_pending_orders(target_token, best_bid, best_ask)

        limit_price = self.calculate_safe_maker_price(best_bid, best_ask)
        if not limit_price: return
        
        existing_positions = self.portfolio.get_positions_for_market(active_market['condition_id'])
        pending_orders = self.portfolio.pending_orders
        
        # Order Manager (Chase the Price)
        import time
        for order in list(pending_orders):
            if order.token_id != target_token:
                logger.info(f"🔄 Canceling {order.side} maker order: Trend switched.")
                if DRY_RUN: self.portfolio.cancel_pending(order)
                else: pm_client.cancel_all_orders()
                continue
                
            # Cancel if price ran away and we've been waiting for > 1.5 seconds
            if time.time() - order.timestamp > 1.5:
                if order.action == "BUY" and best_bid > order.limit_price + 0.005:  # Margin for noise
                    logger.info(f"💨 Order Manager: Canceling staled BUY order at ${order.limit_price:.3f}, Bid ran away to ${best_bid:.3f}")
                    if DRY_RUN: self.portfolio.cancel_pending(order)
                    else: pm_client.cancel_all_orders()
                elif order.action == "SELL" and best_ask < order.limit_price - 0.005:
                    logger.info(f"💨 Order Manager: Canceling staled SELL order at ${order.limit_price:.3f}, Ask crashed to ${best_ask:.3f}")
                    if DRY_RUN: self.portfolio.cancel_pending(order)
                    else: pm_client.cancel_all_orders()
        
        # 1. Position Management
        for pos in existing_positions:
            held_book = await pm_client.fetch_orderbook(pos.token_id)
            held_bid = held_book.get('bid', 0.0)
            held_ask = held_book.get('ask', 1.0)
            
            # 1a. Hard 15% Price Crash Stop Loss (WAVE 3 DIRECTIVE)
            # Dumps lagging bags immediately, ignoring EMA trends if price structurally collapses.
            if held_bid < pos.entry_price * 0.85:
                sell_limit = round(held_bid, 3) # Market Dump
                logger.info(f"💀 [bold red][HARD STOP LOSS][/bold red] Price Crashed > 15%. Bailing out of {pos.side} at TAKER Market ${sell_limit:.3f}.", extra={"markup": True})
                if DRY_RUN: self.portfolio.execute_sell(pos, sell_limit, reason="Hard Stop Loss", is_taker=True)
                else: 
                    pm_client.cancel_all_orders()
                    await pm_client.place_limit_order(pos.token_id, "SELL", sell_limit, pos.num_shares, post_only=False)
                self.last_sell_prices[pos.token_id] = sell_limit
                continue

            if pos.token_id != target_token:
                # Holding opposite side (Trend Reversed) Stop loss at 5 cents drop
                if pos.entry_price - held_bid >= 0.05:
                    # WAVE 2 DIRECTIVE: PROTOCOL "GET ME OUT"
                    sell_limit = round(held_bid, 3) # Market Dump
                    logger.info(f"💀 [bold red][STOP LOSS][/bold red] Trend Reversed. Bailing out of {pos.side} at TAKER Market ${sell_limit:.3f}.", extra={"markup": True})
                    if DRY_RUN: self.portfolio.execute_sell(pos, sell_limit, reason="Stop Loss", is_taker=True)
                    else: 
                        pm_client.cancel_all_orders()
                        await pm_client.place_limit_order(pos.token_id, "SELL", sell_limit, pos.num_shares, post_only=False)
                    self.last_sell_prices[pos.token_id] = sell_limit
                continue
                
            # Holding target side
            if pos.entry_price < best_bid:
                # We want to take profit using a MAKER limit to avoid the 1.5% taker fee pool.
                sell_limit = round(best_ask, 3) if round(best_ask - best_bid, 3) <= 0.01 else round(best_ask - 0.001, 3)
                
                # WAVE 4 DIRECTIVE: FEE-AWARE PROFIT MARGINS
                fee_offset = 0.015 if getattr(pos, 'is_taker', False) else 0.0
                profit_margin = ((sell_limit - pos.entry_price) / pos.entry_price) - fee_offset

                if profit_margin >= 0.03: 
                    logger.info(f"💰 [bold green][TAKE PROFIT][/bold green] Up {profit_margin*100:.1f}%. Selling {pos.side} at MAKER ${sell_limit:.3f}", extra={"markup": True})
                    if DRY_RUN: self.portfolio.execute_sell(pos, sell_limit, reason="Take Profit", is_taker=False)
                    else: 
                        pm_client.cancel_all_orders()
                        await pm_client.place_limit_order(pos.token_id, "SELL", sell_limit, pos.num_shares)
                    self.last_sell_prices[pos.token_id] = sell_limit
        
        # 2. Entry
        if not any(p.token_id == target_token for p in existing_positions) and not any(o.token_id == target_token for o in pending_orders):
            # WAVE 3 DIRECTIVE: SMART FOMO RE-ENTRY
            # Do not chase your own tail. Don't buy back in structurally higher than you just sold.
            if target_token in self.last_sell_prices:
                if limit_price > self.last_sell_prices[target_token]:
                    logger.debug(f"Smart Re-entry active: Preventing redundant buy at ${limit_price:.3f} > last sell ${self.last_sell_prices[target_token]:.3f}")
                    return

            # WAVE 2 DIRECTIVE: HYSTERESIS (Anti-Chop)
            # If we recently dumped the other side, don't flip back instantly unless momentum strongly confirms it.
            if abs(diff) < 1.5:
                logger.debug(f"Hysteresis active: Waiting for stronger momentum confirmation (Diff {abs(diff):.2f} < 1.5) before entry.")
                return

            # WAVE 5 DIRECTIVE: DYNAMIC SPREAD CONTROL
            spread = best_ask - best_bid
            if spread > 0.08:
                logger.warning(f"🚧 Spread dangerously wide ({spread:.3f} > 0.08). Refusing entry to prevent catastrophic slippage.")
                return

            # WAVE 5 DIRECTIVE: ORDER FLOW IMBALANCE (OFI)
            bids = orderbook_res.get('bids', [])
            asks = orderbook_res.get('asks', [])
            total_bid_vol = sum(float(b.get('size', 0)) for b in bids)
            total_ask_vol = sum(float(a.get('size', 0)) for a in asks)
            total_vol = total_bid_vol + total_ask_vol
            
            ofi = total_bid_vol / total_vol if total_vol > 0 else 0.5
            if ofi < 0.30:
                logger.warning(f"🧱 [bold yellow]OFI BLOCK:[/bold yellow] Huge Ask wall detected (OFI: {ofi:.2f}). Vetoing Pyth '{target_side}' signal.", extra={"markup": True})
                return

            is_taker = False
            post_only = True
            
            # The Taker Hybrid Override (Sniper Scope Raised to 4.0)
            if abs(diff) > 4.0:
                logger.warning(f"🚀 [bold red]MASSIVE MOMENTUM DETECTED (Diff: {diff:.2f}) - ENGAGING TAKER OVERRIDE[/bold red]", extra={"markup": True})
                is_taker = True
                post_only = False
                limit_price = round(best_ask, 3) # Cross the spread aggressively
                # WAVE 4 DIRECTIVE: DYNAMIC CONVICTION SIZING
                trade_size_usd = min(TRADE_SIZE_USD * 4, MAX_POSITION_USD)
                logger.info(f"🔥 Sniper Mode Active: SCALING Conviction to ${trade_size_usd:.2f} due to extreme momentum.")
            else:
                trade_size_usd = TRADE_SIZE_USD

            if DRY_RUN:
                self.portfolio.execute_buy(active_market['title'], active_market['condition_id'], target_token, target_side, trade_size_usd, limit_price, is_taker=is_taker)
            else:
                logger.info(f"🚀 [bold magenta][LIVE TRADING][/bold magenta] Limit BUY ${trade_size_usd} | {target_side} @ ${limit_price:.3f} (Post-Only: {post_only})", extra={"markup": True})
                pm_client.cancel_all_orders() # Clear deck
                target_size = round(trade_size_usd / limit_price, 2)
                await pm_client.place_limit_order(target_token, "BUY", limit_price, target_size, post_only=post_only)
        else:
             logger.debug(f"🔒 Holding or Pending {target_side} position. Waiting...")
