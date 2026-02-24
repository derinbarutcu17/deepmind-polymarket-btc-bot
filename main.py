"""Main entry point with kill switch, MTM circuit breaker, and clean shutdown.

Fixes applied from audit:
- C2: Sets config.DRY_RUN module attribute directly, not os.environ
- H1: cancel_all_orders wrapped in run_in_executor
- H5: Circuit breaker uses pessimistic mark (0.0) on fetch failure
- M6: Metrics module wired and daily summary triggered
"""
import argparse
import asyncio
import logging
import os
import sys
import time
from decimal import Decimal
from logging.handlers import TimedRotatingFileHandler

from rich.logging import RichHandler

import config
from config import CIRCUIT_BREAKER_USD
from oracle import AsyncOracle
from polymarket_client import AsyncPMClient
from portfolio import Portfolio
from strategy import BTCStrategy
from metrics import Metrics

# ── Logging ──────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)

console_handler = RichHandler(rich_tracebacks=True, show_path=False)
console_handler.setLevel(logging.INFO)

file_handler = TimedRotatingFileHandler("logs/bot.log", when="midnight", backupCount=7)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))

error_handler = TimedRotatingFileHandler("logs/error.log", when="midnight", backupCount=14)
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[console_handler, file_handler, error_handler],
)
logger = logging.getLogger("Main")

STOP_FILE = "./STOP_TRADING"


def _check_kill_switch() -> bool:
    return os.path.exists(STOP_FILE)


async def resolver_loop(pm_client, portfolio, resolving_queue):
    while True:
        try:
            for slug, condition_id in list(resolving_queue.items()):
                winner = await pm_client.check_resolution(slug)
                if winner:
                    portfolio.resolve_market(condition_id, winner)
                    del resolving_queue[slug]
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Resolver loop error: {e}")
        await asyncio.sleep(10)


async def main(mode: str = "dry-run"):
    is_dry = mode in ("dry-run", "staging")

    # C2 fix: Set module attribute directly so all imports of config.DRY_RUN pick it up
    config.DRY_RUN = is_dry

    logger.info("--- Starting Async Polymarket BTC Trading Bot ---")
    logger.info(f"Mode: [bold magenta]{mode.upper()}[/bold magenta]", extra={"markup": True})
    logger.info(f"DRY_RUN = {config.DRY_RUN}")

    portfolio = Portfolio()
    metrics = Metrics()  # M6 fix: wire metrics

    try:
        pm_client = AsyncPMClient()
    except Exception as e:
        logger.error(f"Cannot initialize PMClient: {e}")
        return

    oracle = AsyncOracle()
    strategy = BTCStrategy(portfolio)

    resolving_queue: dict[str, str] = {}
    resolver_task = asyncio.create_task(resolver_loop(pm_client, portfolio, resolving_queue))

    # Pre-flight
    active_market = await pm_client.get_active_market()
    if not active_market:
        logger.error("No active market found on startup. Exiting...")
        resolver_task.cancel()
        await pm_client.close()
        await oracle.close()
        return

    logger.info(
        f"🎯 Tracking Market: [bold yellow]{active_market['title']}[/bold yellow]",
        extra={"markup": True},
    )

    tick_interval = 0.5
    last_summary_time = time.time()
    SUMMARY_INTERVAL = 3600  # hourly summary

    try:
        while True:
            # ── Kill switch ──────────────────────────────────────────────
            if _check_kill_switch():
                logger.error(
                    "🛑 [bold red]STOP_TRADING file detected. Halting immediately.[/bold red]",
                    extra={"markup": True},
                )
                portfolio.cancel_all_pending()
                if not is_dry:
                    await pm_client.cancel_all_orders_async()  # H1 fix
                break

            try:
                # ── MTM Circuit Breaker (H5 fix: pessimistic on failure) ──
                mark_prices = {}
                for pos in portfolio.open_positions:
                    try:
                        book = await pm_client.fetch_orderbook(pos.token_id)
                        mark_prices[pos.token_id] = Decimal(str(book.get("bid", 0)))
                    except Exception:
                        # H5 fix: assume worst case, not cost basis
                        mark_prices[pos.token_id] = Decimal("0")
                        logger.warning(f"⚠️ Orderbook fetch failed for {pos.token_id[:8]}. Using mark=0.")

                equity = portfolio.get_total_equity(mark_prices)
                drawdown = equity - portfolio.initial_capacity

                if drawdown <= -CIRCUIT_BREAKER_USD:
                    logger.error(
                        f"🚨 [bold red]CIRCUIT BREAKER[/bold red]: MTM equity drawdown "
                        f"${drawdown} exceeds -${CIRCUIT_BREAKER_USD}. Halting.",
                        extra={"markup": True},
                    )
                    metrics.inc("circuit_breaker_trips")
                    if not is_dry:
                        await pm_client.cancel_all_orders_async()  # H1 fix
                    break

                # ── Oracle ───────────────────────────────────────────────
                oracle_res = await oracle.fetch_price()
                price = oracle_res["price"]

                if price == 0.0:
                    metrics.inc("oracle_failures")
                    await asyncio.sleep(tick_interval)
                    continue

                # ── Market rollover ──────────────────────────────────────
                if time.time() >= active_market.get("expires_at", 0):
                    logger.info("Market expired. Appending to resolution queue...")
                    resolving_queue[active_market["slug"]] = active_market["condition_id"]

                    new_market = await pm_client.get_active_market()
                    if new_market:
                        active_market = new_market
                        strategy.last_sell_prices.clear()
                        logger.info(
                            f"🎯 Now Tracking: [bold yellow]{active_market['title']}[/bold yellow]",
                            extra={"markup": True},
                        )
                    else:
                        await asyncio.sleep(5)
                        continue

                # ── Trend ────────────────────────────────────────────────
                trend, diff = strategy.get_trend(price)

                if trend == "NEUTRAL":
                    await asyncio.sleep(tick_interval)
                    continue

                logger.info(
                    f"🔮 Oracle: [bold cyan]${price:,.2f}[/bold cyan] ({oracle_res['source']}) | "
                    f"Trend: {trend} | Diff: {diff:.2f}",
                    extra={"markup": True},
                )

                target_token = active_market["yes_token"] if trend == "UP" else active_market["no_token"]
                target_side = "YES (UP)" if trend == "UP" else "NO (DOWN)"

                # ── Orderbook ────────────────────────────────────────────
                book = await pm_client.fetch_orderbook(target_token)

                # ── Strategy ─────────────────────────────────────────────
                await strategy.evaluate_and_execute(
                    pm_client, active_market, oracle_res, book, diff, target_token, target_side,
                )

                # ── Periodic summary (M6 fix) ────────────────────────────
                if time.time() - last_summary_time > SUMMARY_INTERVAL:
                    path = metrics.write_daily_summary()
                    if path:
                        logger.info(f"📊 Summary written: {path}")
                    last_summary_time = time.time()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Loop Exception: {e}", exc_info=True)
                metrics.inc("loop_errors")

            await asyncio.sleep(tick_interval)

    finally:
        logger.info("Cleaning up...")
        # Final summary
        path = metrics.write_daily_summary()
        if path:
            logger.info(f"📊 Final summary: {path}")

        resolver_task.cancel()
        try:
            await resolver_task
        except asyncio.CancelledError:
            pass
        await pm_client.close()
        await oracle.close()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket BTC Bot")
    parser.add_argument(
        "--mode",
        choices=["dry-run", "staging", "live"],
        default="dry-run",
        help="Operating mode",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(mode=args.mode))
    except KeyboardInterrupt:
        logger.info("Bot interrupted by user.")
