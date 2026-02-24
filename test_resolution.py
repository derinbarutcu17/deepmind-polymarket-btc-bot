import asyncio
import aiohttp
from datetime import datetime
import json

async def test():
    # Let's get an expired market from earlier today
    url = "https://gamma-api.polymarket.com/events?slug=btc-updown-5m-1708891200" # Just an example slug, maybe I should just search for ended=true
    
    # Better: let's query the events API for resolved markets
    async with aiohttp.ClientSession() as session:
        async with session.get("https://gamma-api.polymarket.com/events?limit=5&closed=true") as resp:
            data = await resp.json()
            for event in data:
                if 'markets' in event and len(event['markets']) > 0:
                    m = event['markets'][0]
                    print(f"Title: {m.get('question')}")
                    print(f"Closed: {m.get('closed')}")
                    print(f"Condition ID: {m.get('conditionId')}")
                    
                    tokens = m.get('clobTokenIds', [])
                    if isinstance(tokens, str): tokens = json.loads(tokens)
                    
                    # See if there's a winner field anywhere
                    print(f"Tokens: {tokens}")
                    print(f"Keys in market: {list(m.keys())}")
                    # e.g., 'groupItemTitle', 'outcomes', 'outcomePrices', 'winningToken'
                    for k in ['winningToken', 'winner', 'resolvedOutcome', 'outcomePrices']:
                        if k in m:
                            print(f"{k}: {m[k]}")
                    break

asyncio.run(test())
