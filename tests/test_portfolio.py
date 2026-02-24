"""Unit tests for Portfolio accounting with Decimal precision."""
import os
import csv
from decimal import Decimal

D = Decimal


class TestPortfolioBuy:
    def test_taker_buy_deducts_fee(self, portfolio):
        """Taker buy should deduct amount + 1.5% fee from balance."""
        ok = portfolio.execute_buy("Test", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=True)
        assert ok is True
        expected_balance = D("100") - D("10") - D("10") * D("0.015")
        assert portfolio.balance == expected_balance
        assert len(portfolio.open_positions) == 1
        assert portfolio.open_positions[0].num_shares == D("10") / D("0.50")

    def test_maker_buy_creates_pending(self, portfolio):
        """Maker buy should create a pending order, not an instant position."""
        ok = portfolio.execute_buy("Test", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=False)
        assert ok is True
        assert len(portfolio.pending_orders) == 1
        assert len(portfolio.open_positions) == 0
        # Balance should have been reserved (deducted)
        assert portfolio.balance == D("90")

    def test_insufficient_balance(self, portfolio):
        """Buy should fail when balance is too low."""
        ok = portfolio.execute_buy("Test", "cond1", "tok1", "YES", 200.0, 0.50)
        assert ok is False
        assert portfolio.balance == D("100")


class TestPortfolioSell:
    def test_taker_sell_calculates_pnl(self, portfolio):
        """Taker sell should calculate revenue, fee, and PnL correctly."""
        portfolio.execute_buy("Test", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=True)
        pos = portfolio.open_positions[0]

        ok = portfolio.execute_sell(pos, 0.60, reason="Take Profit", is_taker=True)
        assert ok is True
        assert len(portfolio.open_positions) == 0

        # Revenue = 20 shares * 0.60 = $12.00
        # Fee = 12.00 * 0.015 = $0.18
        # Net = 12.00 - 0.18 = $11.82
        # Starting balance after buy = 100 - 10 - 0.15 = 89.85
        # Final balance = 89.85 + 11.82 = 101.67
        expected = D("100") - D("10") - D("0.150") + D("20") * D("0.60") - D("20") * D("0.60") * D("0.015")
        assert portfolio.balance == expected

    def test_sell_nonexistent_position(self, portfolio):
        """Selling a position not in portfolio should return False."""
        from portfolio import Position
        fake_pos = Position("Test", "cond1", "tok1", "YES", 10, 0.5)
        assert portfolio.execute_sell(fake_pos, 0.60) is False


class TestPortfolioResolution:
    def test_winner_pays_one_dollar(self, portfolio):
        """Winning position should pay $1.00 per share."""
        portfolio.execute_buy("Test", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=True)
        assert len(portfolio.open_positions) == 1

        portfolio.resolve_market("cond1", "tok1")  # WIN
        assert len(portfolio.open_positions) == 0

        # Revenue = 20 shares * $1.00 = $20.00
        # Balance = (100 - 10 - 0.15) + 20.00 = 109.85
        initial_after_buy = D("100") - D("10") - D("0.150")
        expected = initial_after_buy + D("20") * D("1")
        assert portfolio.balance == expected

    def test_loser_pays_nothing(self, portfolio):
        """Losing position should expire worthless."""
        portfolio.execute_buy("Test", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=True)
        portfolio.resolve_market("cond1", "tok_other")  # LOSS
        assert len(portfolio.open_positions) == 0
        # Balance unchanged from after-buy
        expected = D("100") - D("10") - D("0.150")
        assert portfolio.balance == expected


class TestMTMEquity:
    def test_equity_with_mark_prices(self, portfolio):
        """MTM equity should use provided mark prices."""
        portfolio.execute_buy("Test", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=True)
        pos = portfolio.open_positions[0]

        mark_prices = {"tok1": D("0.70")}
        equity = portfolio.get_total_equity(mark_prices)

        # Cash = 100 - 10 - 0.15 = 89.85
        # Mark value = 20 shares * 0.70 = 14.00
        # Equity = 89.85 + 14.00 = 103.85
        expected = (D("100") - D("10") - D("0.150")) + D("20") * D("0.70")
        assert equity == expected

    def test_equity_fallback_to_cost_basis(self, portfolio):
        """Without mark prices, use entry_price as fallback."""
        portfolio.execute_buy("Test", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=True)
        equity = portfolio.get_total_equity()

        # Cash = 89.85, Mark = 20 * 0.50 = 10.00, Equity = 99.85
        expected = (D("100") - D("10") - D("0.150")) + D("20") * D("0.50")
        assert equity == expected


class TestPendingFills:
    def test_maker_buy_fill(self, portfolio):
        """Pending buy fills when ask drops to limit price."""
        portfolio.execute_buy("Test", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=False)
        assert len(portfolio.pending_orders) == 1

        # Ask at 0.50 → should fill
        portfolio.process_pending_orders("tok1", 0.48, 0.50)
        assert len(portfolio.pending_orders) == 0
        assert len(portfolio.open_positions) == 1

    def test_maker_buy_no_fill(self, portfolio):
        """Pending buy does NOT fill when ask is above limit."""
        portfolio.execute_buy("Test", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=False)
        portfolio.process_pending_orders("tok1", 0.48, 0.55)
        assert len(portfolio.pending_orders) == 1
        assert len(portfolio.open_positions) == 0


class TestTradeCSV:
    def test_csv_written_on_taker_buy(self, portfolio):
        """Trade CSV should have a row after a taker buy."""
        import portfolio as pm
        portfolio.execute_buy("Test", "cond1", "tok1", "YES", 10.0, 0.50, is_taker=True)

        with open(pm.TRADES_CSV, "r") as f:
            reader = list(csv.reader(f))
        # Header + 1 data row
        assert len(reader) == 2
        assert reader[1][5] == "BUY"  # action column
