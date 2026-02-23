import logging
import time
from oracle import get_chainlink_btc_price
from polymarket_client import PMClient
from portfolio import Portfolio
from config import DRY_RUN, TRADE_SIZE_USD, TREND_WINDOW_SECONDS

logger = logging.getLogger(__name__)

class BTCStrategy:
    def __init__(self, pm_client: PMClient, portfolio: Portfolio):
        self.pm_client = pm_client
        self.portfolio = portfolio
        self.last_btc_price = 0.0
        self.last_price_time = 0.0
        
    def get_trend(self, current_price: float) -> str:
        """
        Determines the short-term trend based on the Binance price change over TREND_WINDOW_SECONDS.
        """
        now = time.time()
        
        # If this is the first run or the window hasn't passed, just store it and wait.
        if self.last_btc_price == 0.0 or (now - self.last_price_time) >= TREND_WINDOW_SECONDS:
            old_price = self.last_btc_price
            self.last_btc_price = current_price
            self.last_price_time = now
            
            if old_price == 0.0:
                return "NEUTRAL" # Bootstrapping data
                
            delta = current_price - old_price
            
            # Simple Trend Strategy: Option A
            if delta > 0:
                logger.info(f"Trend Analysis: UP (Price increased by ${delta:.2f} over the last {TREND_WINDOW_SECONDS}s window)")
                return "UP"
            elif delta < 0:
                logger.info(f"Trend Analysis: DOWN (Price decreased by ${abs(delta):.2f} over the last {TREND_WINDOW_SECONDS}s window)")
                return "DOWN"
            
        return "NEUTRAL"
        
    def evaluate(self):
        """
        Main execution layer called by the bot loop.
        """
        # 1. Fetch current Chainlink Oracle price
        current_btc_price = get_chainlink_btc_price()
        if current_btc_price == 0.0:
            logger.warning("Failed to fetch Chainlink BTC price. Skipping evaluation.")
            return
            
        logger.info(f"Oracle BTC Price (Chainlink Data Streams): ${current_btc_price:,.2f}")
        
        # 2. Determine Trend (Option A)
        trend = self.get_trend(current_btc_price)
        if trend == "NEUTRAL":
            logger.info("Trend is neutral or gathering data. No trade action.")
            return
            
        # 3. Fetch Active 5-minute Market
        active_market = self.pm_client.get_active_5m_btc_market()
        if not active_market:
            # Note: 5-minute markets might go down momentarily during settlement/rollover.
            logger.info("No active 5-minute BTC market detected. Waiting for next window...")
            return
            
        logger.info(f"Active Market Found: {active_market['title']}")
        
        # 4. Decide WHICH token to buy based on trend
        # "UP" trend means we buy YES. "DOWN" trend means we buy NO. 
        # By convention in Polymarket UP/DOWN markets, the YES token represents the UP condition.
        target_token = active_market['yes_token'] if trend == "UP" else active_market['no_token']
        target_side = "YES (UP)" if trend == "UP" else "NO (DOWN)"
        
        # 5. Fetch current Polymarket Orderbook for that outcome
        book = self.pm_client.get_market_price(target_token)
        best_bid = book['bid']
        best_ask = book['ask']
        
        logger.info(f"Current Book for {target_side}: Bid={best_bid:.4f}, Ask={best_ask:.4f}")
        
        # We want to avoid Taker Fees by being a Maker. 
        # A Maker BUY order rests on the book. We want to be the best bid.
        if best_bid > 0.0:
            limit_price = round(best_bid + 0.001, 3) # Outbid the best bid by $0.001
            # Cap it so we never cross the spread and accidentally become a Taker
            limit_price = min(limit_price, round(best_ask - 0.001, 3))
        else:
            limit_price = 0.50 # Fallback default if book is empty
            
        logger.info(f"Calculated Maker Limit Price: {limit_price:.3f} (~{limit_price*100:.1f}%)")
        
        # 6. Sell Early Logic (Take Profit & Stop Loss)
        # Check if we already have positions in this market
        existing_positions = self.portfolio.get_positions_for_market(active_market['condition_id'])
        
        for pos in existing_positions:
            # We only evaluate sell conditions if we hold the token we're currently analyzing the book for
            if pos.token_id != target_token:
                # If we hold the OPPOSITE side (e.g., trend reversed), we should fetch the book for the token we DO hold to check if we need to Stop Loss
                held_book = self.pm_client.get_market_price(pos.token_id)
                held_bid = held_book['bid']
                
                # Dynamic Stop Loss on Reverse Trend
                if held_bid > 0.0:
                    sell_limit = round(held_bid - 0.001, 3) 
                    sell_limit = max(sell_limit, 0.01) # Floor
                    
                    # If the opposing trend is strong, or if our position value dropped by 20%
                    if pos.entry_price - held_bid >= 0.10: 
                         logger.info(f"[STOP LOSS] Trend Reversed. Selling {pos.side} at ${sell_limit:.3f} to cut losses.")
                         if DRY_RUN:
                             self.portfolio.execute_sell(pos, sell_limit, reason="Stop Loss (Reversal)")
                continue

            # We hold the SAME token the trend is pushing. Check for Take Profit.
            # `best_bid` is what buyers are willing to pay NOW. 
            if pos.entry_price < best_bid:
                # We are in profit! (e.g. bought at 0.45, bid is now 0.60)
                profit_margin = best_bid - pos.entry_price
                if profit_margin >= 0.10: # Take Profit Threshold: 10 cents per share
                    sell_limit = round(best_bid, 3) # We can hit the bid directly, or place a maker slightly below
                    logger.info(f"[TAKE PROFIT] Position is up by {profit_margin*100:.1f}%. Selling {pos.side} at ${sell_limit:.3f}")
                    if DRY_RUN:
                        self.portfolio.execute_sell(pos, sell_limit, reason="Take Profit")

        # 7. Execution Logic (BUY)
        # Only buy if we don't already have a position in this specific token
        if not any(p.token_id == target_token for p in existing_positions):
            if DRY_RUN:
                self.portfolio.execute_buy(active_market['title'], active_market['condition_id'], target_token, target_side, TRADE_SIZE_USD, limit_price)
            else:
                logger.info(f"[LIVE TRADING] Executing LIMIT BUY of ${TRADE_SIZE_USD} on {target_side} token at ${limit_price:.3f}...")
                # Example implementation: pm_client.place_limit_order(target_token, amount=TRADE_SIZE_USD, price=limit_price, side="BUY")
        else:
            logger.info(f"Already holding {target_side} position in this market. Waiting for TP/SL or Resolution.")
