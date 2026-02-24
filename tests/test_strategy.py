"""Unit tests for BTCStrategy decision boundaries."""
from decimal import Decimal


class TestTrendDetection:
    def test_neutral_during_bootstrap(self, strategy):
        """Should return NEUTRAL until enough price history."""
        trend, diff = strategy.get_trend(60000.0)
        assert trend == "NEUTRAL"
        assert diff == 0.0

    def test_up_trend_detection(self, strategy):
        """Rising prices should trigger UP trend."""
        # Feed enough to bootstrap, then feed rising prices
        for i in range(20):
            strategy.get_trend(60000.0 + i * 0.1)
        
        # Big spike
        for _ in range(10):
            trend, diff = strategy.get_trend(60010.0)
        
        assert trend == "UP"
        assert diff > 0

    def test_down_trend_detection(self, strategy):
        """Falling prices should trigger DOWN trend."""
        for i in range(20):
            strategy.get_trend(60000.0 - i * 0.1)
        
        for _ in range(10):
            trend, diff = strategy.get_trend(59990.0)
        
        assert trend == "DOWN"
        assert diff < 0


class TestVolatilityThreshold:
    def test_low_volatility_uses_minimum(self, strategy):
        """When volatility is tiny, entry threshold should be 2.0 minimum."""
        # Flat prices → near-zero vol
        for _ in range(30):
            strategy.price_history.append(60000.0)
        
        thresh = strategy._entry_threshold()
        assert thresh == 2.0

    def test_high_volatility_scales(self, strategy):
        """When volatility is high, threshold should scale up."""
        import random
        random.seed(42)
        for i in range(30):
            strategy.price_history.append(60000.0 + random.gauss(0, 50))
        
        thresh = strategy._entry_threshold()
        assert thresh > 2.0  # Should scale above minimum


class TestMakerPrice:
    def test_tight_spread_joins_bid(self, strategy):
        """Tight spread should join at best bid."""
        price = strategy.calculate_safe_maker_price(0.50, 0.505)
        assert price == Decimal("0.500")

    def test_wide_spread_penny_jumps(self, strategy):
        """Wide spread should penny-jump the bid."""
        price = strategy.calculate_safe_maker_price(0.30, 0.40)
        assert price == Decimal("0.301")

    def test_extreme_skew_rejected(self, strategy):
        """Extreme skew should return None."""
        assert strategy.calculate_safe_maker_price(0.96, 0.98) is None
        assert strategy.calculate_safe_maker_price(0.02, 0.04) is None
