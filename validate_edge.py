"""Statistical edge validation with bootstrap confidence intervals."""
import argparse
import csv
import json
import os
import random
from decimal import Decimal

D = Decimal


def load_trades(path: str) -> list[dict]:
    trades = []
    with open(path, "r") as f:
        for row in csv.DictReader(f):
            pnl = D(row.get("pnl", "0"))
            if pnl != 0:
                trades.append({"pnl": float(pnl), "action": row.get("action", "")})
    return trades


def compute_metrics(trades: list[dict]) -> dict:
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    n = len(pnls)
    w = len(wins) / n if n > 0 else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    expectancy = w * avg_win - (1 - w) * avg_loss

    # Max drawdown
    equity = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)

    # Kelly
    if avg_loss > 0:
        kelly = w - (1 - w) / (avg_win / avg_loss) if avg_win > 0 else 0
    else:
        kelly = w

    return {
        "n": n,
        "win_rate": w,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "total_pnl": sum(pnls),
        "max_drawdown": max_dd,
        "kelly_fraction": kelly,
    }


def bootstrap_ci(trades: list[dict], n_resamples: int = 10000, ci: float = 0.95) -> dict:
    """Bootstrap 95% CI for expectancy."""
    random.seed(42)
    expectations = []

    for _ in range(n_resamples):
        sample = random.choices(trades, k=len(trades))
        m = compute_metrics(sample)
        expectations.append(m["expectancy"])

    expectations.sort()
    lo_idx = int((1 - ci) / 2 * n_resamples)
    hi_idx = int((1 + ci) / 2 * n_resamples)

    return {
        "mean": sum(expectations) / len(expectations),
        "ci_lower": expectations[lo_idx],
        "ci_upper": expectations[hi_idx],
        "p_positive": sum(1 for e in expectations if e > 0) / n_resamples,
    }


def generate_report(metrics: dict, bootstrap: dict, output_dir: str, acceptance: dict) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "perf_summary.md")

    passed = (
        bootstrap["ci_lower"] > 0
        and metrics["max_drawdown"] <= acceptance.get("max_drawdown", 15.0)
    )

    with open(path, "w") as f:
        f.write("# Statistical Edge Validation Report\n\n")

        f.write("## Core Metrics\n\n")
        f.write("| Metric | Value |\n|---|---|\n")
        for k, v in metrics.items():
            f.write(f"| {k} | {v:.4f} |\n")

        f.write("\n## Bootstrap Analysis (10k resamples, 95% CI)\n\n")
        f.write("| Metric | Value |\n|---|---|\n")
        for k, v in bootstrap.items():
            f.write(f"| {k} | {v:.4f} |\n")

        f.write(f"\n## Verdict: {'✅ PASS' if passed else '❌ FAIL'}\n\n")

        if passed:
            f.write("The system demonstrates statistically significant positive expectancy.\n")
            f.write("Recommended to proceed with `scaling_plan.md`.\n")
        else:
            f.write("The system does NOT meet acceptance criteria.\n")
            f.write("See `action_items.md` for remediation.\n")

    # Generate follow-up artifact
    if passed:
        _write_scaling_plan(output_dir, metrics)
    else:
        _write_action_items(output_dir, metrics, bootstrap)

    return path


def _write_scaling_plan(output_dir, metrics):
    path = os.path.join(output_dir, "scaling_plan.md")
    kelly = max(0, min(metrics["kelly_fraction"], 0.25))  # Cap at 25%
    with open(path, "w") as f:
        f.write("# Scaling Plan\n\n")
        f.write(f"- Kelly fraction: {kelly:.2%}\n")
        f.write(f"- Recommended sizing: {kelly * 100:.1f}% of bankroll per trade\n")
        f.write("- Phase 1: Run at 50% Kelly for 200 trades\n")
        f.write("- Phase 2: If expectancy holds, move to 75% Kelly\n")
        f.write("- Phase 3: Full Kelly only after 500+ live trades\n")
        f.write("- Monitoring: Check metrics daily, halt if 95% CI dips below 0\n")


def _write_action_items(output_dir, metrics, bootstrap):
    path = os.path.join(output_dir, "action_items.md")
    with open(path, "w") as f:
        f.write("# Action Items — Edge Not Validated\n\n")
        f.write(f"- Expectancy: ${metrics['expectancy']:.4f}\n")
        f.write(f"- 95% CI Lower: ${bootstrap['ci_lower']:.4f}\n")
        f.write(f"- Max Drawdown: ${metrics['max_drawdown']:.4f}\n\n")
        f.write("## Prioritized Fixes\n\n")

        if metrics["win_rate"] < 0.45:
            f.write("1. **Improve signal quality**: Tighten EMA periods, add momentum confirmation\n")
        if metrics["avg_loss"] > metrics["avg_win"]:
            f.write("2. **Cut losses faster**: Reduce stop-loss threshold from 15% to 10%\n")
        if metrics["expectancy"] < 0:
            f.write("3. **Reduce trading frequency**: Increase hysteresis threshold\n")
            f.write("4. **Widen maker price**: Improve queue priority for better fills\n")
        f.write("5. **Collect more data**: Need N >= 200 trades for reliable statistics\n")


def main():
    parser = argparse.ArgumentParser(description="Validate edge from trades.csv")
    parser.add_argument("--input", default="trades.csv")
    parser.add_argument("--output", default="reports")
    parser.add_argument("--max-drawdown", type=float, default=15.0)
    args = parser.parse_args()

    trades = load_trades(args.input)

    if len(trades) < 10:
        print(f"Only {len(trades)} trades found. Need at least 10 for analysis.")
        print("Run the bot longer or use run_local_backtest.py first.")
        return

    metrics = compute_metrics(trades)
    bootstrap = bootstrap_ci(trades)
    report_path = generate_report(
        metrics, bootstrap, args.output, {"max_drawdown": args.max_drawdown}
    )
    print(f"Report written to {report_path}")
    print(f"Expectancy: ${metrics['expectancy']:.4f}")
    print(f"95% CI: [{bootstrap['ci_lower']:.4f}, {bootstrap['ci_upper']:.4f}]")


if __name__ == "__main__":
    main()
