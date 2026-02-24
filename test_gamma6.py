import asyncio, aiohttp, json
from datetime import datetime, timezone
async def test():
    url = "https://gamma-api.polymarket.com/events?active=true&closed=false&tag_id=102892&order=endDate&ascending=false&limit=100"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=5) as resp:
            data = await resp.json()
            valid_markets = []
            for event in data:
                title = event.get('title', '').lower()
                if 'bitcoin' in title or 'btc' in title:
                    m = event.get('markets', [{}])[0]
                    end_str = m.get('endDate')
                    if end_str:
                        end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                        now = datetime.now(timezone.utc)
                        seconds = (end_dt - now).total_seconds()
                        if 0 < seconds < 900: # strictly next 15 mins
                            valid_markets.append((seconds, m, title))
            
            if valid_markets:
                valid_markets.sort(key=lambda x: x[0])
                print(valid_markets[0][2])
            else:
                print("No market found")
asyncio.run(test())
