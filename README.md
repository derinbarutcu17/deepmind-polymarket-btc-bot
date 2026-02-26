# Polymarket 5-Minute BTC Scalper Bot

A high-frequency "Static Sniper" designed to scalp micro-profits on Polymarket's 5-minute Bitcoin prediction markets. It bypasses expensive enterprise websockets by pulling sub-second price data directly from Pyth Network, tracks momentum via Exponential Moving Averages (EMA), and explicitly targets Maker limit orders to dodge Taker fees.

## Features

* **Sub-second Oracle Streaming:** Uses `hermes.pyth.network` to match Polymarket's sub-second latency for free without requiring enterprise API keys.
* **Zero-Fee Maker Execution:** Defaults to placing Maker Limit Orders one tick above the best bid to passively hit the orderbook and avoid Polymarket's high Taker fees.
* **Aggressive Risk Management:** Enforces a 3% net Take Profit and a hard 20% price crash / 15% trend-reversal Stop Loss.
* **Paper Trading Ledger:** Features a built-in `DRY_RUN` mode with a purely simulated $100 USDC portfolio to test logic before risking real capital.

## Prerequisites

* Python 3.8+
* A Polymarket account with API Keys generated from your account settings.
* USDC on the Polygon network (for live trading).

## Installation

1. Clone the repository and navigate into the project directory.
2. Create a virtual environment and activate it:
```bash
python -m venv venv
source venv/bin/activate

```


3. Install the required dependencies:
```bash
pip install -r requirements.txt

```


*(Core dependencies include `py-clob-client`, `aiohttp`, `rich`, and `pandas`)*

## Configuration

Copy the provided `.env.example` to `.env` to securely store your variables:

```bash
cp .env.example .env

```

Populate your `.env` file with your Polymarket credentials:

```env
POLYMARKET_API_KEY="YOUR_API_KEY_HERE"
POLYMARKET_API_SECRET="YOUR_API_SECRET_HERE"
POLYMARKET_API_PASSPHRASE="YOUR_PASSPHRASE_HERE"
DRY_RUN="True" # Keep True for initial testing
TRADE_SIZE_USD="5.0"

```

## Usage

Start the bot from your terminal:

```bash
python main.py

```

By default, `main.py` runs in `dry-run` mode (paper trading). You will see logs printing every 2 seconds as the bot evaluates the Chainlink price, checks the orderbook, and executes simulated trades.

**To switch to live trading with real USDC:**

1. Stop the bot (`Ctrl + C`).
2. Change `DRY_RUN="True"` to `DRY_RUN="False"` in your `.env` file.
3. Restart the bot. Alternatively, you can use the command flag `python main.py --mode live`.

## Emergency Stop

The bot is equipped with a hardware-level kill switch. Create an empty file named `STOP_TRADING` in the root directory to instantly halt the execution loop, cancel all pending/resting orders, and shut down.
