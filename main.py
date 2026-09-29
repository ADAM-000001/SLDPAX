"""
================================================================================
Main Engine: Funded Master Account Python Trading Desk
================================================================================
Connects to MetaTrader 5, validates the account, restores persisted risk state,
then runs the monitoring loop: refresh risk -> reconcile closed deals ->
evaluate employee signals. A single-instance lock prevents two engines from ever
running on the account, and a local web server exposes the browser dashboard.

Failure handling added
----------------------
* MT5 watchdog. The old loop never checked that the terminal was still alive;
  if the terminal closed, `copy_rates_from_pos` and `account_info` quietly
  returned None and the process spun forever doing nothing, looking healthy.
  The loop now detects the loss, reconnects with backoff, and gives up loudly.
* Rotating log file, so `trading_engine.log` could not grow without bound.
* Risk state is flushed on every exit path.
"""

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

import MetaTrader5 as mt5

from config import (
    EVENT_LOG_SIZE,
    LOG_BACKUP_COUNT,
    LOG_FILE,
    LOG_MAX_BYTES,
    LOOP_INTERVAL_SEC,
    MT5_RECONNECT_DELAY_SEC,
    MT5_RECONNECT_MAX_ATTEMPTS,
    SYMBOL,
)
from event_log import EventLog
from market_data import MarketDataManager
from order_executor import OrderExecutor
from risk_governor import RiskGovernor
from team_office import TeamOffice
from web_dashboard import WebDashboard

LOCK_FILE = "sldpax.lock"


def acquire_single_instance() -> object:
    """Refuse to run two engines on the same account (double-click guard)."""
    import msvcrt

    handle = open(LOCK_FILE, "a+b")
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return handle
    except OSError:
        handle.close()
        return None

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def configure_logging() -> None:
    """File log (rotating) plus a console handler for faults and trade events."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    console_handler.setLevel(logging.WARNING)
    root.addHandler(console_handler)


def connect_mt5(events: EventLog) -> bool:
    """Connect to MT5, retrying briefly to allow the terminal to finish starting."""
    for attempt in range(1, MT5_RECONNECT_MAX_ATTEMPTS + 1):
        if mt5.initialize():
            return True
        error = mt5.last_error()
        if attempt < MT5_RECONNECT_MAX_ATTEMPTS:
            events.info(f"MT5 not ready (error {error}), retry {attempt}/{MT5_RECONNECT_MAX_ATTEMPTS}...")
            time.sleep(MT5_RECONNECT_DELAY_SEC)
        else:
            events.error(f"Failed to connect to MetaTrader 5 after {attempt} attempts: {error}")
    return False


def main() -> int:
    configure_logging()
    logger = logging.getLogger("SLDPAX_Main")
    events = EventLog(EVENT_LOG_SIZE)

    lock = acquire_single_instance()
    if lock is None:
        print("[FATAL] Another SLDPAX engine is already running (sldpax.lock held).")
        return 4

    print("=" * 60)
    print(" [INIT] SLDPAX institutional trading desk")
    print("=" * 60)

    if not connect_mt5(events):
        print(f"[FATAL] Could not connect to MetaTrader 5. Start the terminal and log in first.")
        return 1

    acc = mt5.account_info()
    if acc is None:
        print("[FATAL] Cannot read account info. Ensure MT5 is logged in.")
        mt5.shutdown()
        return 1

    print(f"[OK] Account {acc.login} ({acc.company}) | Balance ${acc.balance:,.2f} | Equity ${acc.equity:,.2f}")

    if not mt5.symbol_select(SYMBOL, True):
        print(f"[FATAL] Symbol {SYMBOL} is not available on this account. Check Market Watch / broker naming.")
        mt5.shutdown()
        return 1
    print(f"[OK] Symbol {SYMBOL} selected")

    market_data = MarketDataManager(symbol=SYMBOL)
    risk_governor = RiskGovernor(events)

    try:
        risk_governor.start()
    except RuntimeError as exc:
        print(f"\n[FATAL] Account validation failed:\n  {exc}")
        mt5.shutdown()
        return 2

    order_executor = OrderExecutor(risk_governor, events)
    team_office = TeamOffice(market_data, order_executor, events)

    web = WebDashboard(risk_governor, events)
    web.start()
    print("[OK] Desk online, risk governor armed, web dashboard on http://127.0.0.1:8080/")
    print("Starting monitoring loop. Press Ctrl+C to stop.\n")
    time.sleep(1.5)

    exit_code = 0
    consecutive_failures = 0
    offline_banner = False

    try:
        while True:
            if mt5.account_info() is None or mt5.terminal_info() is None:
                consecutive_failures += 1
                logger.error(
                    "MT5 connection lost (failure %d/%d).",
                    consecutive_failures,
                    MT5_RECONNECT_MAX_ATTEMPTS,
                )
                if consecutive_failures >= MT5_RECONNECT_MAX_ATTEMPTS:
                    events.error("MT5 connection lost repeatedly. Shutting down to protect the account.")
                    exit_code = 3
                    break
                if not offline_banner:
                    offline_banner = True
                    events.error("MT5 terminal disconnected - attempting reconnect.")
                    print("\n[MT5] Terminal disconnected. Reconnecting...")
                mt5.shutdown()
                time.sleep(MT5_RECONNECT_DELAY_SEC)
                if connect_mt5(events):
                    mt5.symbol_select(SYMBOL, True)
                    consecutive_failures = 0
                    offline_banner = False
                    print("[MT5] Reconnected. Resuming.\n")
                continue
            consecutive_failures = 0

            risk_governor.update_daily_state()
            risk_governor.reconcile()
            team_office.evaluate_all()

            time.sleep(LOOP_INTERVAL_SEC)

    except KeyboardInterrupt:
        print("\n\n[HALT] Shutdown signal received (Ctrl+C).")
    except Exception:
        logger.exception("Fatal error in the trading loop")
        events.error("Fatal exception in the trading loop - see trading_engine.log")
        exit_code = 1
    finally:
        web.stop()
        risk_governor.flush()
        mt5.shutdown()
        if lock is not None:
            lock.close()
            try:  # we held the lock; clear it so the next launch can start
                os.remove(LOCK_FILE)
            except OSError:
                pass
        print("[SHUTDOWN] MT5 connection closed. Risk state persisted.")
        print("           NOTE: any open positions remain live. Manage them in MT5.")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
