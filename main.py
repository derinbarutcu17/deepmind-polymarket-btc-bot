import time
import logging
import requests
from polymarket_client import PMClient
from strategy import BTCStrategy
from portfolio import Portfolio
from config import DRY_RUN

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s - %(message)s'
)
logger = logging.getLogger("Main")

def main():
    logger.info("--- Starting Polymarket BTC Trading Bot ---")
    logger.info(f"Operational Mode: {'DRY RUN' if DRY_RUN else 'LIVE TRADING'}")
    
    # Initialize components
    try:
        portfolio = Portfolio()
        pm_client = PMClient()
        strategy = BTCStrategy(pm_client, portfolio)
    except Exception as e:
        logger.error(f"Failed to initialize bot components: {e}")
        return

    # Main event loop - High frequency for 5-minute markets
    loop_interval_seconds = 2 
    
    try:
        while True:
            try:
                # 1. Check for expired markets to resolve ROI
                if DRY_RUN and portfolio.open_positions:
                    for pos in list(portfolio.open_positions):
                        try:
                           # Check Gamma API if the market condition has resolved
                           url = f"https://gamma-api.polymarket.com/events?condition_id={pos.condition_id}"
                           resp = requests.get(url, timeout=5)
                           if resp.status_code == 200 and resp.json():
                               event = resp.json()[0]
                               market = event.get('markets', [{}])[0]
                               
                               if market.get('closed') and market.get('status') == 'resolved':
                                   # We assume 'winner_token_id' exists or we can infer from 'yes_price' == 1.00
                                   # Or we just check the group_item_index
                                   winning_token = market.get('groupItemTitle') # Often holds the winning string if not clobTokenIds
                                   # Actually, best robust way: check tokens array for winner=True
                                   tokens = market.get('tokens', [])
                                   winner_id = None
                                   for t in tokens:
                                       if t.get('winner') == True:
                                           winner_id = t.get('token_id')
                                           break
                                           
                                   if winner_id:
                                       portfolio.resolve_market(pos.condition_id, winner_id)
                        except Exception as e:
                            logger.error(f"Error resolving portfolio market {pos.market_title}: {e}")

                # 2. Run strategy evaluation
                strategy.evaluate()
                
            except Exception as e:
                logger.error(f"Error during strategy evaluation loop: {e}")
            
            # 3. Wait
            time.sleep(loop_interval_seconds)
            
    except KeyboardInterrupt:
        logger.info("Bot shutting down gracefully due to Keyboard Interrupt.")

if __name__ == "__main__":
    main()
