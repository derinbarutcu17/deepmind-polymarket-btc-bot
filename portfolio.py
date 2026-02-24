"""Portfolio management with Decimal-precision accounting and CSV trade logging."""
import csv
import logging
import os
import time
from decimal import Decimal, getcontext, ROUND_DOWN
from typing import Dict, List, Optional

getcontext().prec = 12

logger = logging.getLogger(__name__)

TRADES_CSV = "trades.csv"
TRADES_HEADER = [
    "timestamp", "market_title", "condition_id", "token_id", "side",
    "action", "entry_price", "exit_price", "shares", "amount_usd",
    "fee", "pnl", "exit_reason", "order_id", "fill_time", "signal_diff",
]

D = Decimal
ZERO = D("0")
ONE = D("1")
TAKER_FEE_RATE = D("0.015")
TICK = D("0.001")


def _ensure_csv():
    """Create CSV with header if it doesn't exist."""
    if not os.path.exists(TRADES_CSV):
        with open(TRADES_CSV, "w", newline="") as f:
            csv.writer(f).writerow(TRADES_HEADER)


class PendingOrder:
    def __init__(
        self,
        action: str,
        market_title: str,
        condition_id: str,
        token_id: str,
        side: str,
        amount_usd: Decimal,
        limit_price: Decimal,
        position=None,
        reason: str = "",
        is_taker: bool = False,
        signal_diff: float = 0.0,
    ):
        self.action = action
        self.market_title = market_title
        self.condition_id = condition_id
        self.token_id = token_id
        self.side = side
        self.amount_usd = D(str(amount_usd))
        self.limit_price = D(str(limit_price))
        self.position = position
        self.reason = reason
        self.is_taker = is_taker
        self.signal_diff = signal_diff
        self.timestamp = time.time()


class Position:
    def __init__(
        self,
        market_title: str,
        condition_id: str,
        token_id: str,
        side: str,
        amount_usd,
        entry_price,
        is_taker: bool = False,
    ):
        self.market_title = market_title
        self.condition_id = condition_id
        self.token_id = token_id
        self.side = side
        self.amount_usd = D(str(amount_usd))
        self.entry_price = D(str(entry_price))
        self.num_shares = (
            (self.amount_usd / self.entry_price).quantize(TICK, rounding=ROUND_DOWN)
            if self.entry_price > ZERO
            else ZERO
        )
        self.is_taker = is_taker

    def __repr__(self):
        return (
            f"Position(market='{self.market_title}', side='{self.side}', "
            f"shares={self.num_shares}, avg_price=${self.entry_price})"
        )


class Portfolio:
    """Manages balance and positions with Decimal precision and CSV audit trail."""

    def __init__(self, initial_balance=100.0):
        _ensure_csv()
        self.balance = D(str(initial_balance))
        self.initial_capacity = D(str(initial_balance))
        self.open_positions: List[Position] = []
        self.pending_orders: List[PendingOrder] = []

    # ── helpers ──────────────────────────────────────────────────────────

    def _log_trade(
        self,
        action: str,
        market_title: str,
        condition_id: str,
        token_id: str,
        side: str,
        entry_price: Decimal,
        exit_price: Decimal,
        shares: Decimal,
        amount_usd: Decimal,
        fee: Decimal,
        pnl: Decimal,
        exit_reason: str = "",
        order_id: str = "",
        fill_time: float = 0.0,
        signal_diff: float = 0.0,
    ):
        row = [
            time.time(),
            market_title,
            condition_id,
            token_id,
            side,
            action,
            str(entry_price),
            str(exit_price),
            str(shares),
            str(amount_usd),
            str(fee),
            str(pnl),
            exit_reason,
            order_id,
            str(fill_time),
            str(signal_diff),
        ]
        try:
            with open(TRADES_CSV, "a", newline="") as f:
                csv.writer(f).writerow(row)
        except Exception as e:
            logger.error(f"Failed to write trade log: {e}")

    # ── MTM ──────────────────────────────────────────────────────────────

    def get_total_equity(self, mark_prices: Optional[Dict[str, Decimal]] = None) -> Decimal:
        """Return balance + mark-to-market value of open positions."""
        equity = self.balance
        for pos in self.open_positions:
            if mark_prices and pos.token_id in mark_prices:
                mark = D(str(mark_prices[pos.token_id]))
            else:
                mark = pos.entry_price  # fallback to cost basis
            equity += pos.num_shares * mark
        return equity

    # ── BUY ──────────────────────────────────────────────────────────────

    def execute_buy(
        self,
        market_title: str,
        condition_id: str,
        token_id: str,
        side: str,
        amount_usd,
        limit_price,
        is_taker: bool = False,
        signal_diff: float = 0.0,
    ) -> bool:
        amount_usd = D(str(amount_usd))
        limit_price = D(str(limit_price))

        if self.balance < amount_usd:
            logger.warning(
                f"⚠️  [PORTFOLIO] Insufficient balance (${self.balance}) "
                f"to execute ${amount_usd} buy.",
                extra={"markup": True},
            )
            return False

        fee = amount_usd * TAKER_FEE_RATE if is_taker else ZERO
        self.balance -= (amount_usd + fee)

        if is_taker:
            pos = Position(market_title, condition_id, token_id, side, amount_usd, limit_price, is_taker=True)
            self.open_positions.append(pos)

            self._log_trade(
                "BUY", market_title, condition_id, token_id, side,
                limit_price, ZERO, pos.num_shares, amount_usd, fee, ZERO,
                signal_diff=signal_diff,
            )

            logger.info(
                f"📥 [bold green][PORTFOLIO] Executed TAKER Buy:[/bold green] "
                f"{pos.num_shares} shares of {side} at ${limit_price}"
                + (f" (Fee: ${fee})" if fee > ZERO else ""),
                extra={"markup": True},
            )
            logger.info(
                f"💵 [PORTFOLIO] Available Cash: [bold]${self.balance}[/bold] "
                f"(Total P&L: {self.get_total_pnl_str()})",
                extra={"markup": True},
            )
            return True
        else:
            order = PendingOrder(
                "BUY", market_title, condition_id, token_id, side,
                amount_usd, limit_price, is_taker=False, signal_diff=signal_diff,
            )
            self.pending_orders.append(order)
            logger.info(
                f"⏳ [bold yellow][PORTFOLIO] Pending MAKER Buy placed:[/bold yellow] "
                f"{amount_usd / limit_price} shares of {side} at ${limit_price} (Awaiting Fill)",
                extra={"markup": True},
            )
            return True

    # ── SELL ─────────────────────────────────────────────────────────────

    def execute_sell(
        self,
        position: Position,
        limit_price,
        reason: str = "Manual",
        is_taker: bool = False,
        signal_diff: float = 0.0,
    ) -> bool:
        limit_price = D(str(limit_price))

        if position not in self.open_positions:
            logger.warning("⚠️  [PORTFOLIO] Attempted to sell a position not in portfolio.", extra={"markup": True})
            return False

        if is_taker:
            revenue = position.num_shares * limit_price
            fee = revenue * TAKER_FEE_RATE
            profit = (revenue - fee) - position.amount_usd

            self.balance += (revenue - fee)
            self.open_positions.remove(position)

            self._log_trade(
                "SELL", position.market_title, position.condition_id,
                position.token_id, position.side,
                position.entry_price, limit_price, position.num_shares,
                position.amount_usd, fee, profit,
                exit_reason=reason, signal_diff=signal_diff,
            )

            pnl_color = "green" if profit >= ZERO else "red"
            logger.info(
                f"📤 [bold cyan][PORTFOLIO] EXECUTED EARLY TAKER SELL ({reason}):[/bold cyan] "
                f"Sold {position.num_shares} shares of {position.side} at ${limit_price}"
                + (f" (Fee: ${fee})" if fee > ZERO else ""),
                extra={"markup": True},
            )
            logger.info(
                f"💸 [PORTFOLIO] Trade P&L: [bold {pnl_color}]${profit} "
                f"({(profit / position.amount_usd * D('100')):+.1f}%)[/bold {pnl_color}]",
                extra={"markup": True},
            )
            logger.info(
                f"💵 [PORTFOLIO] Available Cash: [bold]${self.balance}[/bold] "
                f"(Total P&L: {self.get_total_pnl_str()})",
                extra={"markup": True},
            )
            return True
        else:
            order = PendingOrder(
                "SELL", position.market_title, position.condition_id,
                position.token_id, position.side, position.amount_usd,
                limit_price, position=position, reason=reason,
                is_taker=False, signal_diff=signal_diff,
            )
            self.pending_orders.append(order)
            logger.info(
                f"⏳ [bold yellow][PORTFOLIO] Pending MAKER Sell placed ({reason}):[/bold yellow] "
                f"{position.num_shares} shares of {position.side} at ${limit_price} (Awaiting Fill)",
                extra={"markup": True},
            )
            return True

    # ── cancel ───────────────────────────────────────────────────────────

    def cancel_pending(self, order: PendingOrder):
        if order in self.pending_orders:
            if order.action == "BUY":
                self.balance += order.amount_usd
            self.pending_orders.remove(order)

    def cancel_all_pending(self):
        count = len(self.pending_orders)
        if count == 0:
            return
        for order in list(self.pending_orders):
            self.cancel_pending(order)
        logger.debug(f"🗑️ [PORTFOLIO] Cancelled {count} pending simulator orders.")

    # ── sim fills ────────────────────────────────────────────────────────

    def process_pending_orders(self, token_id: str, current_best_bid, current_best_ask):
        current_best_bid = D(str(current_best_bid))
        current_best_ask = D(str(current_best_ask))

        for order in list(self.pending_orders):
            if order.token_id != token_id:
                continue

            if order.action == "BUY":
                if current_best_ask <= order.limit_price:
                    pos = Position(
                        order.market_title, order.condition_id, order.token_id,
                        order.side, order.amount_usd, order.limit_price, is_taker=False,
                    )
                    self.open_positions.append(pos)

                    self._log_trade(
                        "BUY_FILL", order.market_title, order.condition_id,
                        order.token_id, order.side,
                        order.limit_price, ZERO, pos.num_shares,
                        order.amount_usd, ZERO, ZERO,
                        fill_time=time.time() - order.timestamp,
                        signal_diff=order.signal_diff,
                    )

                    logger.info(
                        f"✅ [bold green][PORTFOLIO] FILLED MAKER Buy:[/bold green] "
                        f"{pos.num_shares} shares of {order.side} at ${order.limit_price}",
                        extra={"markup": True},
                    )
                    self.pending_orders.remove(order)

            elif order.action == "SELL":
                if current_best_bid >= order.limit_price:
                    pos = order.position
                    if pos in self.open_positions:
                        revenue = pos.num_shares * order.limit_price
                        profit = revenue - pos.amount_usd
                        self.balance += revenue
                        self.open_positions.remove(pos)

                        self._log_trade(
                            "SELL_FILL", pos.market_title, pos.condition_id,
                            pos.token_id, pos.side,
                            pos.entry_price, order.limit_price, pos.num_shares,
                            pos.amount_usd, ZERO, profit,
                            exit_reason=order.reason,
                            fill_time=time.time() - order.timestamp,
                            signal_diff=order.signal_diff,
                        )

                        pnl_color = "green" if profit >= ZERO else "red"
                        logger.info(
                            f"✅ [bold cyan][PORTFOLIO] FILLED MAKER SELL ({order.reason}):[/bold cyan] "
                            f"Sold {pos.num_shares} shares of {pos.side} at ${order.limit_price}",
                            extra={"markup": True},
                        )
                        logger.info(
                            f"💸 [PORTFOLIO] Trade P&L: [bold {pnl_color}]${profit}[/bold {pnl_color}]",
                            extra={"markup": True},
                        )
                        logger.info(
                            f"💵 [PORTFOLIO] Available Cash: [bold]${self.balance}[/bold] "
                            f"(Total P&L: {self.get_total_pnl_str()})",
                            extra={"markup": True},
                        )
                    self.pending_orders.remove(order)

    # ── resolution ───────────────────────────────────────────────────────

    def resolve_market(self, condition_id: str, winning_token_id: str):
        positions_to_resolve = [p for p in self.open_positions if p.condition_id == condition_id]

        if not positions_to_resolve:
            return

        logger.info(
            f"🔄 [PORTFOLIO] Resolving {len(positions_to_resolve)} positions for "
            f"market condition {condition_id[:8]}...",
            extra={"markup": True},
        )

        for pos in positions_to_resolve:
            if pos.token_id == winning_token_id:
                revenue = pos.num_shares * ONE
                profit = revenue - pos.amount_usd
                self.balance += revenue
                self._log_trade(
                    "RESOLUTION_WIN", pos.market_title, pos.condition_id,
                    pos.token_id, pos.side,
                    pos.entry_price, ONE, pos.num_shares,
                    pos.amount_usd, ZERO, profit,
                    exit_reason="Market Resolution (WIN)",
                )
                logger.info(
                    f"🏆 [bold green][PORTFOLIO] WON MARKET ({pos.side}):[/bold green] "
                    f"Payout ${revenue} (Profit: [bold]+${profit}[/bold])",
                    extra={"markup": True},
                )
            else:
                self._log_trade(
                    "RESOLUTION_LOSS", pos.market_title, pos.condition_id,
                    pos.token_id, pos.side,
                    pos.entry_price, ZERO, pos.num_shares,
                    pos.amount_usd, ZERO, -pos.amount_usd,
                    exit_reason="Market Resolution (LOSS)",
                )
                logger.info(
                    f"💥 [bold red][PORTFOLIO] LOST MARKET ({pos.side}):[/bold red] "
                    f"Shares expired worthless. (Loss: [bold]-${pos.amount_usd}[/bold])",
                    extra={"markup": True},
                )

            self.open_positions.remove(pos)

        logger.info(
            f"💵 [PORTFOLIO] Available Cash: [bold]${self.balance}[/bold] "
            f"(Total P&L: {self.get_total_pnl_str()})",
            extra={"markup": True},
        )

    # ── queries ──────────────────────────────────────────────────────────

    def get_positions_for_market(self, condition_id: str) -> List[Position]:
        return [p for p in self.open_positions if p.condition_id == condition_id]

    def get_total_pnl_str(self) -> str:
        pnl = self.balance - self.initial_capacity
        return f"${pnl:+.2f}"
