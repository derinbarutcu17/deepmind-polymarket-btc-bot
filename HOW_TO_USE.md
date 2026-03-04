# How to Use the Polymarket BTC Bot

This guide covers setup, configuration, and operation of the bot. Read it fully before running with real money.

---

## 1. Prerequisites

- **Python 3.11+** installed on your machine.
- **A Polymarket account** with API Keys generated from your account settings.
- **USDC on Polygon** (for live trading).

---

## 2. Install Dependencies

```bash
cd /Users/derin/Desktop/CODING/polymarket-btc-bot
python -m venv venv
source venv/bin/activate          # Mac/Linux
# venv\Scripts\activate           # Windows
pip install -r requirements.txt
```

---

## 3. Configure Your `.env` File

Copy or open the `.env` file in the project root. **Never share this file — it contains your private keys.**

### Required — API Keys

```env
POLYMARKET_API_KEY="YOUR_API_KEY_HERE"
POLYMARKET_API_SECRET="YOUR_API_SECRET_HERE"
POLYMARKET_API_PASSPHRASE="YOUR_PASSPHRASE_HERE"
```

### Optional — Tunable Parameters (with defaults)

```env
# Trade sizing
TRADE_SIZE_USD="5.0"        # USDC per individual trade
MAX_POSITION_USD="20.0"     # Maximum total open exposure across all positions

# EMA trend engine
SHORT_EMA_PERIOD="120"      # Short EMA window (ticks, ~1 min at 0.5s tick)
LONG_EMA_PERIOD="360"       # Long EMA window (ticks, ~3 min at 0.5s tick)

# Risk
CIRCUIT_BREAKER_USD="15.0"  # Halt the bot if total equity drops this many dollars
```

> **Note:** `TREND_WINDOW_SECONDS` has been removed. Trend detection now uses a dual-EMA engine (`SHORT_EMA_PERIOD` vs `LONG_EMA_PERIOD`), not a fixed time window.

---

## 4. Run the Bot

The bot is launched with a `--mode` flag. **The `.env` `DRY_RUN` setting is no longer used** — mode is set entirely via the command line.

### Dry Run (simulated trades, fake money — start here)

```bash
source venv/bin/activate
python main.py --mode dry-run
```

Trades are simulated with a fake $100 portfolio. The bot prints real P&L calculations but never touches your account.

### Staging (paper trading with real market data, no orders placed)

```bash
python main.py --mode staging
```

Identical to dry-run. Use this to verify the live API connection is healthy before switching to live.

### Live Trading (real money)

```bash
python main.py --mode live
```

The bot places real Maker limit orders to your Polymarket account.

---

## 5. How the Strategy Works

The bot targets **15-minute Bitcoin price resolution markets** on Polymarket.

### Entry — All gates must pass

| Gate | Condition |
|------|-----------|
| G2 | Token price must be between $0.04 and $0.93 |
| G3 | 30-second cooldown after any stop-out |
| G4 | **Golden Zone**: price must be $0.40–$0.55 |
| G4b | Market must have >90 seconds remaining |
| G5 | Re-entry blocked if price is above last stop-out price |
| G6 | EMA diff must be ≥ 0.6 (strong momentum required) |
| G7 | Orderbook spread must be ≤ $0.05 |
| G8 | Requires a micro-pullback (at least 1 tick below 5s peak) |
| OFI | Order-flow imbalance must favour the trade side |

### Exit — Three automated exits

1. **Hard Stop Loss ($0.08 drop):** If the position loses 8 cents from entry (mid-price), the bot immediately executes a delta hedge by taker-buying the opposite token. Fires after a 5-second minimum hold.

2. **Momentum Reversal:** If the EMA diff flips to ≥ 0.8 against the position direction, the bot executes the same delta hedge immediately (no hold-time requirement).

3. **Stale Trade Scratch:** If a position has been held for 45+ seconds with weak momentum (diff < 0.4), the bot sells at the bid minus 1 tick to exit dead money.

### Take Profit

- A Maker limit sell order is queued at **entry price × 1.10** (+10%).
- The TP order remains active until filled or an exit trigger fires.

### No-Chase Rule

Buy orders that are not filled within **5 seconds** are automatically cancelled. The bot returns to hunting rather than chasing price.

---

## 6. Logs and Monitoring

Logs are written automatically to the `logs/` folder:

```
logs/bot.log      # Full debug log, rotates daily, keeps 7 days
logs/error.log    # Errors only, rotates daily, keeps 14 days
```

To tail the live log in a second terminal window:

```bash
tail -f logs/bot.log
```

An hourly performance summary is also written to `logs/` automatically.

---

## 7. Kill Switch (Emergency Stop)

To halt the bot cleanly at any time — without losing pending orders to a hard crash — create a file named `STOP_TRADING` in the project root:

```bash
touch STOP_TRADING
```

The bot detects this file on its next tick, cancels all pending orders, and exits gracefully. Delete the file before restarting.

To stop immediately instead, press `Ctrl + C` in the terminal.

---

## 8. Circuit Breaker

If the portfolio equity drops by more than `CIRCUIT_BREAKER_USD` (default $15), the bot automatically cancels all live orders and halts. This is a hard safety net. Check your logs for the `CIRCUIT BREAKER` message, investigate, and restart manually.

---

## 9. Recommended First-Run Checklist

1. Run `python main.py --mode dry-run` and watch for at least 1 hour.
2. Confirm entries are only happening in the $0.40–$0.55 golden zone.
3. Confirm stop-losses and take-profits are firing correctly in the logs.
4. Run `python main.py --mode staging` to verify the API connection is live.
5. Switch to `python main.py --mode live` only when you are satisfied.
