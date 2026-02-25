"""Local backtester — replays historical oracle snapshots through the strategy."""
import argparse
import csv
import json
import os
import sys
from decimal import Decimal

# Setup env before imports
os.environ.setdefault("DRY_RUN", "True")
os.environ.setdefault("POLYMARKET_API_KEY", "backtest")
os.environ.setdefault("POLYMARKET_API_SECRET", "backtest")
os.environ.setdefault("POLYMARKET_API_PASSPHRASE", "backtest")

from portfolio import Portfolio, TRADES_CSV
import portfolio as pm


def run_backtest(snapshots: list[dict], output_dir: str = "reports"):
    """Replay oracle snapshots through a simplified strategy simulation.
    
    Each snapshot: {"timestamp": float, "price": float, "best_bid": float, "best_ask": float}
    """
    os.makedirs(output_dir, exist_ok=True)

    # Use isolated trades CSV
    pm.TRADES_CSV = os.path.join(output_dir, "backtest_trades.csv")
    portfolio = Portfolio(initial_balance=100.0)

    from strategy import BTCStrategy
    strategy = BTCStrategy(portfolio)

    equity_curve = []
    D = Decimal

    for snap in snapshots:
        price = snap["price"]
        best_bid = snap.get("best_bid", 0.50)
        best_ask = snap.get("best_ask", 0.52)

        trend, diff = strategy.get_trend(price)

        # Simulate fills
        portfolio.process_pending_orders("sim_yes", best_bid, best_ask)
        portfolio.process_pending_orders("sim_no", best_bid, best_ask)

        if trend != "NEUTRAL" and abs(diff) >= strategy._entry_threshold():
            target_token = "sim_yes" if trend == "UP" else "sim_no"
            target_side = "YES (UP)" if trend == "UP" else "NO (DOWN)"

            # Check if we have a position
            has_pos = any(p.token_id == target_token for p in portfolio.open_positions)
            has_pending = any(o.token_id == target_token for o in portfolio.pending_orders)

            if not has_pos and not has_pending:
                limit_price = strategy.calculate_safe_maker_price(best_bid, best_ask)
                if limit_price:
                    portfolio.execute_buy(
                        "Backtest Market", "bt_cond", target_token, target_side,
                        5.0, float(limit_price), is_taker=False, signal_diff=diff,
                    )

        # Take profit / stop loss for open positions
        for pos in list(portfolio.open_positions):
            bid_d = D(str(best_bid))
            if bid_d < pos.entry_price * D("0.85"):
                portfolio.execute_sell(pos, best_bid, reason="Hard Stop Loss", is_taker=True, signal_diff=diff)
            elif bid_d > pos.entry_price * D("1.03"):
                portfolio.execute_sell(pos, best_bid, reason="Take Profit", is_taker=True, signal_diff=diff)

        mark = {"sim_yes": D(str(best_bid)), "sim_no": D(str(best_bid))}
        equity = float(portfolio.get_total_equity(mark))
        equity_curve.append({"timestamp": snap.get("timestamp", 0), "equity": equity})

    # Write equity curve
    eq_path = os.path.join(output_dir, "equity_curve.csv")
    with open(eq_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp", "equity"])
        w.writeheader()
        w.writerows(equity_curve)

    # Generate PNG if matplotlib available
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        ts = [e["timestamp"] for e in equity_curve]
        eq = [e["equity"] for e in equity_curve]
        plt.figure(figsize=(12, 5))
        plt.plot(ts, eq, linewidth=1)
        plt.title("Equity Curve — Backtest")
        plt.xlabel("Time")
        plt.ylabel("Equity ($)")
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(os.path.join(output_dir, "equity_curve.png"), dpi=150)
        plt.close()
    except ImportError:
        print("matplotlib not installed — skipping equity curve PNG")

    final_equity = equity_curve[-1]["equity"] if equity_curve else 100.0
    print(f"Backtest complete. Final equity: ${final_equity:.2f}")
    print(f"Trades CSV: {pm.TRADES_CSV}")
    print(f"Equity curve: {eq_path}")

    return equity_curve


def generate_sample_data(n_minutes: int = 10080) -> list[dict]:
    """Generate synthetic 1-minute BTC price data for testing (~7 days)."""
    import random
    random.seed(42)
    
    snapshots = []
    price = 64000.0
    t = 1700000000.0

    for i in range(n_minutes):
        price += random.gauss(0, 15)  # ~$15 per-minute vol
        bid = round(0.50 + random.gauss(0, 0.02), 3)
        ask = round(bid + random.uniform(0.005, 0.03), 3)
        bid = max(0.05, min(0.95, bid))
        ask = max(bid + 0.005, min(0.95, ask))
        
        snapshots.append({
            "timestamp": t + i * 60,
            "price": round(price, 2),
            "best_bid": bid,
            "best_ask": ask,
        })
    
    return snapshots


def main():
    parser = argparse.ArgumentParser(description="Local backtest runner")
    parser.add_argument("--input", default=None, help="JSON file with oracle snapshots")
    parser.add_argument("--output", default="reports", help="Output directory")
    parser.add_argument("--generate", action="store_true", help="Generate synthetic data")
    parser.add_argument("--days", type=int, default=7, help="Days of synthetic data")
    args = parser.parse_args()

    if args.input and os.path.exists(args.input):
        with open(args.input) as f:
            snapshots = json.load(f)
    elif args.generate or not args.input:
        print(f"Generating {args.days} days of synthetic data...")
        snapshots = generate_sample_data(args.days * 1440)
    else:
        print(f"Input file not found: {args.input}")
        return

    run_backtest(snapshots, args.output)


if __name__ == "__main__":
    main()
