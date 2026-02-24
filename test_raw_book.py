import aiohttp
import asyncio
import json

async def test():
    async with aiohttp.ClientSession() as s:
        async with s.get("https://gamma-api.polymarket.com/events?active=true&closed=false&tag_id=102892&limit=20") as r:
            data = await r.json()
            for event in data:
                m = event.get('markets', [{}])[0]
                tokens = m.get('clobTokenIds')
                if not tokens: tokens = m.get('tokens', [])
                if isinstance(tokens, str): tokens = json.loads(tokens)
                if tokens:
                    tok = tokens[0]
                    if isinstance(tok, dict): tok = tok['token_id']
                    async with s.get(f"https://clob.polymarket.com/book?token_id={tok}") as r2:
                        book = await r2.json()
                        bids = book.get('bids', [])
                        asks = book.get('asks', [])
                        if bids or asks:
                            print(f"Token: {tok}")
                            print(f"Bids (first 2): {bids[:2]}")
                            print(f"Bids (last 2): {bids[-2:]}")
                            print(f"Asks (first 2): {asks[:2]}")
                            print(f"Asks (last 2): {asks[-2:]}")
                            return
asyncio.run(test())
