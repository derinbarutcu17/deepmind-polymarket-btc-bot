import asyncio
from polymarket_client import AsyncPMClient

async def test():
    pm = AsyncPMClient()
    # We will fetch a past market slug, from earlier today. e.g. 5:00 PM ET is 1708898400 or something. 
    # Actually, we can just search for ANY recent closed market from Gamma API.
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get("https://gamma-api.polymarket.com/events?limit=1&closed=true") as resp:
            data = await resp.json()
            slug = data[0]['slug']
            print(f"Testing resolution on: {slug}")
            winner = await pm.check_resolution(slug)
            print(f"WINNER TOKEN ID: {winner}")

asyncio.run(test())
