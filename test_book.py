import asyncio, aiohttp, json
async def test():
    token_id = "16047031225078979186830012299676647194874567455750165724382709748055800867597"
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            bids = data.get('bids', [])
            asks = data.get('asks', [])
            print(f"Bids: {bids}")
            print(f"Asks: {asks}")
            if bids:
                print(f"bids[0]: {bids[0]['price']}, bids[-1]: {bids[-1]['price']}")
            if asks:
                print(f"asks[0]: {asks[0]['price']}, asks[-1]: {asks[-1]['price']}")
asyncio.run(test())
