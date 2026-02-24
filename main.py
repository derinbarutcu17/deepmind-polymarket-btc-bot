import asyncio
import logging
from rich.logging import RichHandler
from polymarket_client import AsyncPMClient
from oracle import AsyncOracle
from strategy import BTCStrategy
from portfolio import Portfolio
from config import DRY_RUN

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False)]
)
logger = logging.getLogger("Main")

async def resolver_loop(pm_client, portfolio, resolving_queue):
    while True:
        try:
            for slug, condition_id in list(resolving_queue.items()):
                winner = await pm_client.check_resolution(slug)
                if winner:
                    portfolio.resolve_market(condition_id, winner)
                    del resolving_queue[slug]
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Resolver loop error: {e}")
        await asyncio.sleep(10)

async def main():
    logger.info("--- Starting Async Polymarket BTC Trading Bot ---")
    logger.info(f"Operational Mode: [bold magenta]{'DRY RUN' if DRY_RUN else 'LIVE TRADING'}[/bold magenta]", extra={"markup": True})
    
    portfolio = Portfolio()
    try:
        pm_client = AsyncPMClient()
    except Exception as e:
        logger.error(f"Cannot initialize the PMClient: {e}")
        return
        
    oracle = AsyncOracle()
    strategy = BTCStrategy(portfolio)
    
    resolving_queue = {} # slug -> condition_id
    resolver_task = asyncio.create_task(resolver_loop(pm_client, portfolio, resolving_queue))
    
    # Pre-flight market check
    active_market = await pm_client.get_active_market()
    if not active_market:
        logger.error("No active market found on startup. Exiting...")
        return
        
    logger.info(f"🎯 Tracking Market: [bold yellow]{active_market['title']}[/bold yellow]", extra={"markup": True})

    tick_interval = 0.5 # Sub-second ticking for HFT

    while True:
        try:
            # 1. Fetch Oracle Price using concurrent tasks inside the Oracle class
            oracle_res = await oracle.fetch_price()
            price = oracle_res['price']
            source = oracle_res['source']
            
            import time
            if price == 0.0:
                await asyncio.sleep(tick_interval)
                continue
                
            # Check market expiration to seamlessly rollover
            if time.time() >= active_market.get('expires_at', 0):
                logger.info("Market expired. Appending to resolution queue...", extra={"markup": True})
                resolving_queue[active_market.get('slug')] = active_market.get('condition_id')
                
                logger.info("Fetching the next 5-minute window...")
                new_market = await pm_client.get_active_market()
                if new_market:
                    active_market = new_market
                    logger.info(f"🎯 Now Tracking: [bold yellow]{active_market['title']}[/bold yellow]", extra={"markup": True})
                # We do not block here if missing, just let it loop and retry later
                else:
                    await asyncio.sleep(5)
                    continue
                
            # 2. Get the trend
            trend, diff = strategy.get_trend(price)
            
            if trend == "NEUTRAL":
                # Only log debug occasionally if needed, skip orderbook phase
                await asyncio.sleep(tick_interval)
                continue
                
            logger.info(f"🔮 Oracle: [bold cyan]${price:,.2f}[/bold cyan] ({source}) | Trend: {trend} | Diff: {diff:.2f}", extra={"markup": True})
                
            target_token = active_market['yes_token'] if trend == "UP" else active_market['no_token']
            target_side = "YES (UP)" if trend == "UP" else "NO (DOWN)"
            
            # 3. Hit the orderbook
            book = await pm_client.fetch_orderbook(target_token)
            
            # 4. Trigger the brain
            await strategy.evaluate_and_execute(pm_client, active_market, oracle_res, book, diff, target_token, target_side)
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Loop Exception: {e}")
            
        await asyncio.sleep(tick_interval)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
         logger.info("Bot shutting down gracefully.")
