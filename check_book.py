import requests
from config import POLYMARKET_HOST
import time

def test():
    anchor_ts = 1771796700
    curr_ts = int(time.time())
    current_base = anchor_ts + ((curr_ts - anchor_ts) // 300) * 300

    slug = f"btc-updown-5m-{current_base + 300}"
    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    resp = requests.get(url)
    m = resp.json()[0]['markets'][0]
    
    import json
    tokens = m.get('clobTokenIds')
    if isinstance(tokens, str): tokens = json.loads(tokens)
    
    print(f"Checking Market: {m['question']}")
    for i, token in enumerate(tokens):
        side = "YES" if i == 0 else "NO"
        book_url = f"{POLYMARKET_HOST}/book?token_id={token}"
        book = requests.get(book_url).json()
        bids = book.get('bids', [])
        asks = book.get('asks', [])
        
        # Sort bids descending (highest first)
        bids_sorted = sorted([float(b['price']) for b in bids], reverse=True)
        # Sort asks ascending (lowest first)
        asks_sorted = sorted([float(a['price']) for a in asks])
        
        best_bid = bids_sorted[0] if bids_sorted else 0.0
        best_ask = asks_sorted[0] if asks_sorted else 1.0
        
        print(f"{side} Token {token}:")
        print(f"  Raw Bids: {[b['price'] for b in bids]}")
        print(f"  Raw Asks: {[a['price'] for a in asks]}")
        print(f"  Best Bid: {best_bid}")
        print(f"  Best Ask: {best_ask}")

if __name__ == '__main__':
    test()
