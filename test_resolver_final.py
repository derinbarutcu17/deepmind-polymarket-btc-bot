import asyncio
from polymarket_client import AsyncPMClient
import logging
logging.basicConfig(level=logging.INFO)

async def test():
    pm = AsyncPMClient()
    slug = "nba-will-the-mavericks-beat-the-grizzlies-by-more-than-5pt5-points-in-their-december-4-matchup"
    print(f"Testing resolution on: {slug}")
    winner = await pm.check_resolution(slug)
    print(f"WINNER TOKEN ID: {winner}")

asyncio.run(test())
