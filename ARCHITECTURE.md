# Polymarket BTC Bot: Technical Architecture

This document breaks down the core architecture and execution logic of the Polymarket High-Frequency 5-Minute BTC bot.

## Core Modules

### 1. `main.py` (The Heartbeat)
This is the primary orchestrator that runs the entire program.
*   **Initialization**: It sets up the `PMClient` (Polymarket Connection), `Portfolio` (Dry-Run Ledger), and `BTCStrategy` (Brain).
*   **The 2-Second Loop**: It runs an infinite `while True:` loop that pauses for exactly 2 seconds between ticks. This interval ensures the bot is aggressively monitoring the orderbook for early exits without triggering Polymarket API rate limits.
*   **Auto-Resolver Hook**: Before triggering the strategy, it queries the `portfolio` to check if any older markets have mathematically closed on Polymarket, cleaning out "expired" fake shares and assigning a 100% loss or $1.00 win per share.

### 2. `oracle.py` (The Eyes)
Polymarket's 5-minute BTC markets resolve strictly based on the **Chainlink BTC/USD Data Stream**.
*   **The Pyth Network Bypass**: Chainlink Data Streams require enterprise websocket keys. To achieve the exact same sub-second institutional streaming accuracy legally and freely, the Oracle calls `hermes.pyth.network`.
*   The API returns an 8-to-18 decimal price float dynamically (`expo` mathematics), acting as the absolute source of truth for the bot's trend evaluation, perfectly matching the latency Polymarket relies upon.

### 3. `polymarket_client.py` (The Hands)
This file houses the `PMClient` class, wrapping the official `py_clob_client.client.ClobClient`.
*   **Predictive Slug Discovery (`get_active_5m_btc_market`)**: Rather than paginating thousands of slow API events to find the current 5-minute window, the bot takes the current UNIX timestamp, snaps it to the nearest 300-second (5-minute) boundary, and constructs the EXACT URL slug (e.g., `btc-up-down-5m-1771797600`). It verifies the ping and instantly locks onto the active market.
*   **Orderbook Parsing (`get_market_price`)**: Polymarket sorts its Bids/Asks inversely (Ascending/Descending respectively). The bot explicitly requests the `[-1]` index of both arrays, targeting the absolute tightest liquidity on the orderbook for minimum slippage.

### 4. `strategy.py` (The Brain)
This handles the logic of what to do with the Oracle information and Polymarket orderbook.

#### The Buy Logic (Trend Following)
1.  **Baseline Check**: Wait `TREND_WINDOW_SECONDS` (default 60s). Store the BTC price at the start, and track the price at the end.
2.  **Trade Trigger**: If the price increased > $0, it attempts a BUY on the YES token. If the price decreased < $0, it prepares a BUY on the NO token.
3.  **Maker Sizing (Avoiding Fees)**: Polymarket charges high Taker fees (up to 3%). To avoid this, the bot places a **Maker Limit Order**. It queries the `best_bid` (e.g., 0.45) and manually overrides its limit to `$0.451`. This guarantees the order hits the book passively, avoiding all Taker fees.

#### The Sell Logic (Active Exit Management)
While waiting for the 5-minute clock to hit zero, the brain re-evaluates held positions every 2 seconds:
*   **Take Profit**: If we own YES shares at a cost basis of $0.45, and the `best_bid` aggressively spikes to $0.80, the bot stops waiting. It executes an early `SELL LIMIT` order into the liquidity, immediately locking in the $0.35/share profit before the trend might reverse. (Requires minimum 10% un-levered PNL to trigger).
*   **Stop Loss**: If we bought YES shares, but the trend reverses hard (the opposing NO token bid is pushing high), the bot re-prices its YES shares. If the value drops by >10 cents below our cost basis, it executes an early `SELL LIMIT` (e.g., at $0.25) to salvage remaining cash, rather than allowing the position to expire worthless at $0.00.

### 5. `portfolio.py` (The Ledger)
A purely dry-run simulation engine tracking the fake 100 USDC starting balance.
*   **State Machine**: It maintains an inventory of purchased tokens, tracking amount spent, shares owned, and entry cost bases.
*   **Receipts**: It handles printing the vivid + Profit & - Loss percentage logs to the terminal when `strategy.py` commands an early Take Profit/Stop Loss exit.
*   **True PNL Mapping**: It interfaces with the Auto-Resolver in `main.py`—when Polymarket signals the condition is `resolved/closed`, this class tallies 1 win token to exactly $1.00 USD, tracking ROI mathematically.
