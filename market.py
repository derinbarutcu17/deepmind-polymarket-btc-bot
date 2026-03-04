from collections import deque
import time

class PriceBuffer:
    def __init__(self, maxlen: int = 10):
        # 10 ticks at 0.5s = 5 seconds of orderbook history
        self.buffer = deque(maxlen=maxlen)

    def add_tick(self, bid: float, ask: float):
        self.buffer.append({'bid': bid, 'ask': ask, 'time': time.time()})

    def is_micro_pullback(self, current_bid: float) -> bool:
        """
        Returns True if the current price is a micro-pullback 
        compared to the recent peak in the 5-second buffer.
        Helps to ensure we buy red ticks into a green trend instead of chasing tops.
        """
        if len(self.buffer) < 4:
            return False  # Need some history
            
        # Highest bid in the recent buffer window
        max_recent_bid = max(item['bid'] for item in self.buffer)
        
        # Pullback defined as at least 1 tick (0.01) below the recent peak
        return current_bid <= max_recent_bid - 0.01

