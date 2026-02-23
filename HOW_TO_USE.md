# How to Use the Polymarket BTC Bot

This guide will walk you through exactly how to set up, test, and run the automated trading bot. It is designed to be as simple as possible.

## 1. Prerequisites (What you need)
*   **Python**: Installed on your computer (Mac/Windows/Linux).
*   **A Polymarket Account**: With API Keys generated from your account settings.
*   **Funds**: USDC on the Polygon network (to trade for real money).

## 2. Setup Your Keys (The `.env` file)
The bot uses a secret file called `.env` to store your Polymarket keys. **Never share this file with anyone.**

1. Open the project folder `polymarket-btc-bot`.
2. Find the `.env` file and open it in a text editor (like VS Code or Notepad).
3. Paste your keys exactly like this:
   ```env
   POLYMARKET_API_KEY="YOUR_API_KEY_HERE"
   POLYMARKET_API_SECRET="YOUR_API_SECRET_HERE"
   POLYMARKET_API_PASSPHRASE="YOUR_PASSPHRASE_HERE"
   ```

## 3. Configure the Bot (Settings)
In the same `.env` file, you can control how the bot behaves:

*   `DRY_RUN`: 
    *   Set to `"True"`: The bot processes real market data but uses fake $100 money. It will print simulated profits and losses. **(Highly Recommended First Step)**.
    *   Set to `"False"`: The bot will use your REAL Polymarket USDC to execute trades.
*   `TRADE_SIZE_USD`: How much USDC the bot bets on a single market (e.g., `"10.0"`).
*   `TREND_WINDOW_SECONDS`: How long the bot watches the Bitcoin price before deciding if it's going UP or DOWN (e.g., `"60"` seconds).

## 4. Run the Bot (Testing)
1. Open your terminal (Mac/Linux) or Command Prompt (Windows).
2. Navigate to your bot folder: `cd /Users/derin/Desktop/CODING/polymarket-btc-bot`
3. Activate the python environment: `source venv/bin/activate`
4. Start the bot: `python main.py`

You will immediately see the bot printing logs every 2 seconds. It will find the active 5-Minute Bitcoin market, check the Chainlink price, wait your defined window (e.g. 60s), and then execute a simulated **Buy**.

## 5. Live Trading (Real Money)
Once you have watched the bot trade in `DRY_RUN="True"` mode for an hour, and you are happy with the logic, stopping points, and orderbook pricing:

1. Stop the bot by pressing `Ctrl + C` in the terminal.
2. Open the `.env` file.
3. Change `DRY_RUN="True"` to `DRY_RUN="False"`.
4. Run `python main.py` again.

The bot is now fully autonomous. It will place Maker Limit orders (0 fees) directly to your Polymarket account, scan the orderbook for early profit-taking, and automatically exit losing trades before the 5-minute clock runs out.
