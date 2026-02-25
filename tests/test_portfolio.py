import pytest
from decimal import Decimal
from portfolio import Portfolio, Position, PendingOrder as Order

D = Decimal


@pytest.fixture
def portfolio():
    p = Portfolio(initial_balance=D("100.0"))
    # Clear logs for testing
    import os
    if os.path.exists("trades.csv"):
        os.remove("trades.csv")
    return p


class TestPortfolioAccounting:
    def test_initial_balance(self, portfolio):
        assert portfolio.balance == D("100.0")
        assert portfolio.initial_capacity == D("100.0")

    def test_execute_buy_maker(self, portfolio):
        """Maker buy should not deduct fees immediately."""
        portfolio.execute_buy("Market", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=False)
        assert len(portfolio.pending_orders) == 1
        assert portfolio.balance == D("90.0")  # 100 - 10

    def test_execute_buy_taker(self, portfolio):
        """Taker buy should deduct fees immediately."""
        portfolio.execute_buy("Market", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=True)
        # 10.0 / 0.50 = 20 shares. Fee = 10 * 0.015 = 0.15. Total cost = 10.15
        assert portfolio.balance == D("89.85")
        assert len(portfolio.open_positions) == 1


class TestOrderLifecycle:
    def test_process_fill(self, portfolio):
        """Pending order should become an open position when filled."""
        portfolio.execute_buy("Market", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=False)
        portfolio.process_pending_orders("tok1", 0.49, 0.50)  # best_ask <= limit -> FILL
        assert len(portfolio.pending_orders) == 0
        assert len(portfolio.open_positions) == 1
        assert portfolio.open_positions[0].num_shares == 20

    def test_cancel_pending(self, portfolio):
        portfolio.execute_buy("Market", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=False)
        order = portfolio.pending_orders[0]
        portfolio.cancel_pending(order)
        assert len(portfolio.pending_orders) == 0
        assert portfolio.balance == D("100.0")


class TestEquity:
    def test_total_equity_cost_basis(self, portfolio):
        """Total equity should be balance + cost basis of positions."""
        portfolio.execute_buy("Test", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=True)
        # Balance = 100 - 10.15 = 89.85
        # Cost basis = 10.0
        # Equity = 89.85 + 10.0 = 99.85
        assert portfolio.get_total_equity() == D("99.85")


class TestResolution:
    def test_resolve_win(self, portfolio):
        portfolio.execute_buy("Test", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=True)
        pos = portfolio.open_positions[0]
        # 20 shares should resolve to $20
        portfolio.resolve_market("cond1", "tok1")
        assert len(portfolio.open_positions) == 0
        assert portfolio.balance == D("89.85") + D("20.0")

    def test_resolve_loss(self, portfolio):
        portfolio.execute_buy("Test", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=True)
        portfolio.resolve_market("cond1", "tok2")  # tok1 lost
        assert len(portfolio.open_positions) == 0
        assert portfolio.balance == D("89.85")
