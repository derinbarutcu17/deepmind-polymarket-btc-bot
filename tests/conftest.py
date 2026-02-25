"""Shared test fixtures."""
import os
import sys
import pytest

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set env vars BEFORE importing any project modules
os.environ["POLYMARKET_API_KEY"] = "test-key"
os.environ["POLYMARKET_API_SECRET"] = "test-secret"
os.environ["POLYMARKET_API_PASSPHRASE"] = "test-passphrase"
os.environ["DRY_RUN"] = "True"


@pytest.fixture
def portfolio():
    from portfolio import Portfolio, TRADES_CSV
    # Use a temp CSV per test
    import tempfile
    original = TRADES_CSV
    import portfolio as pm
    pm.TRADES_CSV = os.path.join(tempfile.mkdtemp(), "test_trades.csv")
    p = Portfolio(initial_balance=100.0)
    yield p
    pm.TRADES_CSV = original


@pytest.fixture
def strategy(portfolio):
    from strategy import BTCStrategy
    return BTCStrategy(portfolio)
