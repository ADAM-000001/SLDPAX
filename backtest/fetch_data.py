"""
Historical data fetcher for the SLDPAX backtester.

Pulls one year of XAUUSD bars from the local MetaTrader 5 terminal and caches
them as CSV under data/. MT5 rejects a single request that would exceed the
terminal's maxbars, so intraday timeframes are fetched in chunks and stitched.
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import MetaTrader5 as mt5  # noqa: E402

from config import SYMBOL  # noqa: E402

DATA_DIR = os.path.join(PROJECT_ROOT, "data")
CHUNK_DAYS = 45
TIMEFRAMES = ("M5", "M15", "M30", "H1")
_COLUMNS = ["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]


def path_for(symbol: str, tf: str) -> str:
    return os.path.join(DATA_DIR, f"{symbol}_{tf}.csv")


def _fetch_chunk(symbol: str, tf, frm: datetime, to: datetime):
    """copy_rates_range with retries; returns a DataFrame or None."""
    for attempt in range(4):
        rates = mt5.copy_rates_range(symbol, tf, frm, to)
        if rates is not None and len(rates):
            return pd.DataFrame(rates)
        time.sleep(1.0 + attempt)
    return None


def fetch(symbol: str, tf_name: str, days: int, force: bool = False) -> pd.DataFrame:
    out_path = path_for(symbol, tf_name)
    if os.path.exists(out_path) and not force:
        return pd.read_csv(out_path, parse_dates=["datetime"])

    os.makedirs(DATA_DIR, exist_ok=True)
    tf = getattr(mt5, f"TIMEFRAME_{tf_name}")
    to = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    frm = to - timedelta(days=days)

    print(f"  fetching {symbol} {tf_name}: {frm.date()} -> {to.date()}")
    frames = []
    cursor = frm
    while cursor < to:
        chunk_to = min(cursor + timedelta(days=CHUNK_DAYS), to)
        df = _fetch_chunk(symbol, tf, cursor, chunk_to)
        if df is None:
            print(f"    !! no data for {cursor.date()} -> {chunk_to.date()} ({mt5.last_error()})")
        else:
            frames.append(df)
            print(f"    {cursor.date()} -> {chunk_to.date()}: {len(df):>6} bars")
        cursor = chunk_to

    if not frames:
        raise SystemExit(f"FATAL: no {tf_name} data returned for {symbol}")

    merged = pd.concat(frames, ignore_index=True)
    merged = merged.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    merged["datetime"] = pd.to_datetime(merged["time"], unit="s", utc=True)
    for col in _COLUMNS:
        if col not in merged.columns:
            merged[col] = 0
    merged = merged[_COLUMNS + ["datetime"]]
    merged.to_csv(out_path, index=False)
    print(f"  saved {len(merged):>7} bars -> {os.path.relpath(out_path, PROJECT_ROOT)}")
    return merged


def load(symbol: str, tf_name: str) -> pd.DataFrame:
    p = path_for(symbol, tf_name)
    if not os.path.exists(p):
        raise SystemExit(f"FATAL: {p} not found. Run:  python backtest\\fetch_data.py")
    return pd.read_csv(p, parse_dates=["datetime"])


def main() -> int:
    days = 365
    terminal = None
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg in ("--force", "-f"):
            continue
        if arg == "--terminal":
            terminal = argv[i + 1] if i + 1 < len(argv) else None
        elif arg.startswith("--terminal="):
            terminal = arg.split("=", 1)[1]
        elif arg.isdigit():
            days = int(arg)
    force = "--force" in argv or "-f" in argv

    if terminal:
        # Accept the install directory or terminal64.exe; MT5 wants the executable.
        if not os.path.exists(terminal):
            print(f"FATAL: terminal path does not exist: {terminal}")
            return 1
        terminal = os.path.abspath(terminal)
        if os.path.isdir(terminal):
            exe = os.path.join(terminal, "terminal64.exe")
            if not os.path.exists(exe):
                print(f"FATAL: no terminal64.exe in {terminal}")
                return 1
            terminal = exe
        ok = mt5.initialize(path=terminal)
    else:
        ok = mt5.initialize()
    if not ok:
        print(f"FATAL: cannot start MetaTrader 5: {mt5.last_error()}")
        return 1

    info = mt5.symbol_info(SYMBOL)
    if info is None or not mt5.symbol_select(SYMBOL, True):
        print(f"FATAL: {SYMBOL} unavailable on this terminal")
        return 1
    print(f"Source: {SYMBOL}  digits={info.digits} point={info.point} "
          f"contract={info.trade_contract_size} stops_level={info.trade_stops_level}")
    ti = mt5.terminal_info()
    if ti:
        print(f"Terminal: {ti.company} build {ti.build}  {ti.path}")
        print(f"         {ti.data_path}")
    print(f"Window: last {days} days\n")

    summary = {}
    for tf in TIMEFRAMES:
        df = fetch(SYMBOL, tf, days, force=force)
        summary[tf] = (len(df), df["datetime"].iloc[0], df["datetime"].iloc[-1],
                       float(df["close"].min()), float(df["close"].max()))

    tick = mt5.symbol_info_tick(SYMBOL)
    if tick:
        print(f"\nCurrent tick: bid {tick.bid} ask {tick.ask} spread {tick.ask - tick.bid:.2f}")
    mt5.shutdown()

    print(f"\n{'TF':<5}{'Bars':>9}  {'From':<20}{'To':<22}{'Low':>10}{'High':>10}")
    for tf, (n, a, b, lo, hi) in summary.items():
        print(f"{tf:<5}{n:>9}  {str(a)[:19]:<20}{str(b)[:22]:<22}{lo:>10.2f}{hi:>10.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
