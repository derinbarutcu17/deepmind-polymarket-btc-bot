import asyncio, aiohttp, json
from datetime import datetime, timezone

async def test():
    import time
    curr_ts = int(time.time())
    current_base = (curr_ts // 300) * 300
    
    slugs = []
    # Check current window and next 2 windows
    for ts in [current_base, current_base + 300, current_base + 600]:
        slugs.append(f"btc-updown-5m-{ts}")
        
    for slug in slugs:
        url = f"https://gamma-api.polymarket.com/events?slug={slug}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                data = await resp.json()
                if data:
                    event = data[0]
                    title = event.get('title')
                    m = event.get('markets', [{}])[0]
                    end_str = m.get('endDate')
                    if end_str:
                        end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                        now = datetime.now(timezone.utc)
                        seconds = (end_dt - now).total_seconds()
                        if seconds > 0:
                            print(f"FOUND ACTIVE: {slug} | Title: {title} | Closes in: {seconds}s")
asyncio.run(test())
