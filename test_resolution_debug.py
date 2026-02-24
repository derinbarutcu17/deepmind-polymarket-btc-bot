import asyncio
import aiohttp
from polymarket_client import AsyncPMClient
import json

async def test():
    slug = "nba-will-the-mavericks-beat-the-grizzlies-by-more-than-5pt5-points-in-their-december-4-matchup"
    url = f"https://gamma-api.polymarket.com/events?slug={slug}&closed=true"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=5) as resp:
            data = await resp.json()
            m = data[0].get('markets', [{}])[0]
            prices = m.get('outcomePrices', [])
            tokens = m.get('clobTokenIds', [])
            print(f"Prices type: {type(prices)}, value: {prices}")
            print(f"Tokens type: {type(tokens)}, value: {tokens}")
            if isinstance(tokens, str): tokens = json.loads(tokens)
            print(f"Tokens after json load: {type(tokens)}, value: {tokens}")
            token_ids = [t['token_id'] if isinstance(t, dict) else t for t in tokens]
            print(f"Token IDs: {token_ids}")
            
            for idx, p in enumerate(prices):
                print(f"Idx: {idx}, Price: {p}, float: {float(p)}, >= 0.99: {float(p) >= 0.99}")
                if float(p) >= 0.99 or p == "1":
                    print(f"RETURN: {token_ids[idx]}")
                    return

asyncio.run(test())
