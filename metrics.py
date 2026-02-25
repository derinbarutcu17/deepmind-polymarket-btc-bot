"""Lightweight trade metrics and daily summary reporter."""
import csv
import os
import time
from collections import Counter
from decimal import Decimal


class Metrics:
    def __init__(self):
        self.counters: Counter = Counter()

    def inc(self, key: str, n: int = 1):
        self.counters[key] += n

    def record_trade(self, won: bool):
        self.counters["trades"] += 1
        if won:
            self.counters["wins"] += 1
        else:
            self.counters["losses"] += 1

    def record_error(self):
        self.counters["errors"] += 1

    def snapshot(self) -> dict:
        return dict(self.counters)

    def write_daily_summary(self, reports_dir: str = "reports", trades_csv: str = "trades.csv"):
        """Generate a markdown performance summary from the trades CSV."""
        os.makedirs(reports_dir, exist_ok=True)

        if not os.path.exists(trades_csv):
            return

        wins, losses = [], []
        total_pnl = Decimal("0")

        with open(trades_csv, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pnl = Decimal(row.get("pnl", "0"))
                if pnl > 0:
                    wins.append(pnl)
                elif pnl < 0:
                    losses.append(pnl)
                total_pnl += pnl

        total_trades = len(wins) + len(losses)
        if total_trades == 0:
            return

        win_rate = len(wins) / total_trades
        avg_win = sum(wins) / len(wins) if wins else Decimal("0")
        avg_loss = sum(losses) / len(losses) if losses else Decimal("0")
        expectancy = win_rate * float(avg_win) - (1 - win_rate) * abs(float(avg_loss))

        ts = time.strftime("%Y-%m-%d")
        path = os.path.join(reports_dir, f"perf_summary_{ts}.md")

        with open(path, "w") as f:
            f.write(f"# Performance Summary — {ts}\n\n")
            f.write(f"| Metric | Value |\n|---|---|\n")
            f.write(f"| Total Trades | {total_trades} |\n")
            f.write(f"| Wins | {len(wins)} |\n")
            f.write(f"| Losses | {len(losses)} |\n")
            f.write(f"| Win Rate | {win_rate:.1%} |\n")
            f.write(f"| Avg Win | ${avg_win:.4f} |\n")
            f.write(f"| Avg Loss | ${avg_loss:.4f} |\n")
            f.write(f"| Expectancy | ${expectancy:.4f} |\n")
            f.write(f"| Total PnL | ${total_pnl:.4f} |\n")
            f.write(f"\n**Counters:** {dict(self.counters)}\n")

        return path
