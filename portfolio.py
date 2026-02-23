import logging
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

class Position:
    def __init__(self, market_title: str, condition_id: str, token_id: str, side: str, amount_usd: float, entry_price: float):
        self.market_title = market_title
        self.condition_id = condition_id
        self.token_id = token_id
        self.side = side  # "YES (UP)" or "NO (DOWN)"
        self.amount_usd = amount_usd # Amount initially spent
        self.num_shares = amount_usd / entry_price if entry_price > 0 else 0
        self.entry_price = entry_price
        
    def __repr__(self):
        return f"Position(market='{self.market_title}', side='{self.side}', shares={self.num_shares:.2f}, avg_price=${self.entry_price:.3f})"

class Portfolio:
    """Manages the fake balance and active simulated positions for DRY RUN mode."""
    def __init__(self, initial_balance: float = 100.0):
        self.balance = initial_balance
        self.initial_capacity = initial_balance
        self.open_positions: List[Position] = []
        
    def execute_buy(self, market_title: str, condition_id: str, token_id: str, side: str, amount_usd: float, limit_price: float) -> bool:
        """Simulates a MAKER LIMIT BUY order."""
        if self.balance < amount_usd:
            logger.warning(f"[PORTFOLIO] Insufficient fake balance (${self.balance:.2f}) to execute ${amount_usd:.2f} buy.")
            return False
            
        self.balance -= amount_usd
        pos = Position(market_title, condition_id, token_id, side, amount_usd, limit_price)
        self.open_positions.append(pos)
        
        logger.info(f"[PORTFOLIO] Executed Buy: {pos.num_shares:.2f} shares of {side} at ${limit_price:.3f}")
        logger.info(f"[PORTFOLIO] Available Cash: ${self.balance:.2f} (Total P&L: {self.get_total_pnl_str()})")
        return True
        
    def execute_sell(self, position: Position, limit_price: float, reason: str = "Manual") -> bool:
        """Simulates a MAKER LIMIT SELL order to dump a position early."""
        if position not in self.open_positions:
            logger.warning("[PORTFOLIO] Attempted to sell a position not in portfolio.")
            return False
            
        revenue = position.num_shares * limit_price
        profit = revenue - position.amount_usd
        self.balance += revenue
        self.open_positions.remove(position)
        
        logger.info(f"[PORTFOLIO] EXECUTED EARLY SELL ({reason}): Sold {position.num_shares:.2f} shares of {position.side} at ${limit_price:.3f}")
        logger.info(f"[PORTFOLIO] Trade P&L: ${profit:+.2f} ({profit/position.amount_usd*100:+.1f}%)")
        logger.info(f"[PORTFOLIO] Available Cash: ${self.balance:.2f} (Total P&L: {self.get_total_pnl_str()})")
        return True
        
    def resolve_market(self, condition_id: str, winning_token_id: str):
        """Processes resolutions for expired markets, paying out $1 per winning share."""
        positions_to_resolve = [p for p in self.open_positions if p.condition_id == condition_id]
        
        if not positions_to_resolve:
            return
            
        logger.info(f"[PORTFOLIO] Resolving {len(positions_to_resolve)} positions for market condition {condition_id[:8]}...")
        
        for pos in positions_to_resolve:
            if pos.token_id == winning_token_id:
                # Win: 1 share = $1.00 payout
                revenue = pos.num_shares * 1.00
                profit = revenue - pos.amount_usd
                self.balance += revenue
                logger.info(f"[PORTFOLIO] WON MARKET ({pos.side}): Payout ${revenue:.2f} (Profit: ${profit:+.2f})")
            else:
                # Loss: 1 share = $0.00
                logger.info(f"[PORTFOLIO] LOST MARKET ({pos.side}): Shares expired worthless. (Loss: -${pos.amount_usd:.2f})")
                
            self.open_positions.remove(pos)
            
        logger.info(f"[PORTFOLIO] Available Cash: ${self.balance:.2f} (Total P&L: {self.get_total_pnl_str()})")
        
    def get_positions_for_market(self, condition_id: str) -> List[Position]:
        return [p for p in self.open_positions if p.condition_id == condition_id]
        
    def get_total_pnl_str(self) -> str:
        # Note: This is realized PnL based on available cash vs starting capital.
        # It does not include unrealized value of open positions.
        pnl = self.balance - self.initial_capacity
        return f"${pnl:+.2f}"
