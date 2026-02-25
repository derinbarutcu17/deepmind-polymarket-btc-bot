import pytest
from decimal import Decimal
from strategy import BTCStrategy
from portfolio import Portfolio

D = Decimal


@pytest.fixture
def strategy():
    p = Portfolio(initial_balance=D("100.0"))
    return BTCStrategy(p)


class TestTrendDetection:
    def test_neutral_start(self, strategy):
        trend, diff = strategy.get_trend(60000.0)
        assert trend == "NEUTRAL"
        assert diff == 0.0

    def test_up_trend(self, strategy):
        # Pump prices to generate positive diff
        prices = [60000.0 + i * 10 for i in range(20)]
        for p in prices:
            trend, diff = strategy.get_trend(p)
        assert trend == "UP"
        assert diff > 0.5

    def test_down_trend(self, strategy):
        # Crash prices to generate negative diff
        prices = [60000.0 - i * 10 for i in range(20)]
        for p in prices:
            trend, diff = strategy.get_trend(p)
        assert trend == "DOWN"
        assert diff < -0.5


class TestMakerPrice:
    def test_maker_join_bid(self, strategy):
        """When spread is 1 tick, join bid."""
        price = strategy.calculate_safe_maker_price(0.50, 0.51)
        assert price == D("0.50")

    def test_maker_frontrun(self, strategy):
        """When spread is wide, frontrun by 1 tick."""
        price = strategy.calculate_safe_maker_price(0.50, 0.55)
        assert price == D("0.501")

    def test_extreme_skew_rejected(self, strategy):
        """Extreme skew (>0.98 or <0.02) should return None."""
        assert strategy.calculate_safe_maker_price(0.981, 0.99) is None
        assert strategy.calculate_safe_maker_price(0.01, 0.015) is None
        assert strategy.calculate_safe_maker_price(0.50, 0.51) == D("0.50")
