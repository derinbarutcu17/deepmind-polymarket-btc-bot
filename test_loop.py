import asyncio
from polymarket_client import AsyncPMClient
from strategy import BTCStrategy
from portfolio import Portfolio
async def test():
    pm = AsyncPMClient()
    market = await pm.get_active_market()
    if not market: return
    print(f"Tracking: {market['title']}")
    for i in range(5):
        book = await pm.fetch_orderbook(market['yes_token'])
        print(f"YES Book: {book}")
        await asyncio.sleep(1)
asyncio.run(test())
