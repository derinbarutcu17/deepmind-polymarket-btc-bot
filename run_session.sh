#!/bin/bash

# Default values if not provided
MODE=${1:-dry-run}
TRADE_SIZE=${TRADE_SIZE_USD:-2.0}
MAX_POS=${MAX_POSITION_USD:-6.0}

# Create logs directory if it doesn't exist
mkdir -p logs

# Create a unique filename for this session based on the date/time
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="logs/session_${MODE}_${TIMESTAMP}.log"

echo "------------------------------------------------"
echo "🚀 BTC BOT SESSION RUNNER"
echo "------------------------------------------------"
echo "Mode:       $MODE"
echo "Log File:   $LOG_FILE"
echo "Trade Size: \$$TRADE_SIZE"
echo "Max Pos:    \$$MAX_POS"
echo "------------------------------------------------"
echo "Tip: Press Ctrl+C to stop the bot."
echo ""

# Run the bot
# 2>&1 redirects errors to the same place as normal output
# 'tee' shows it on screen and writes to the file at the same time
TRADE_SIZE_USD="$TRADE_SIZE" MAX_POSITION_USD="$MAX_POS" venv/bin/python main.py --mode "$MODE" 2>&1 | tee "$LOG_FILE"

echo ""
echo "------------------------------------------------"
echo "✅ BOT STOPPED"
echo "Full logs for this session have been saved to:"
echo "[$(pwd)/$LOG_FILE]"
echo "------------------------------------------------"
