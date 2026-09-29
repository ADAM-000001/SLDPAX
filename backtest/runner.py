"""
One-year historical backtest for the SLDPAX desk.

Runs the PRODUCTION modules - RiskGovernor, OrderExecutor, TeamOffice,
MarketDataManager - completely unmodified. The only thing swapped out is the
MetaTrader5 module, which is replaced by `backtest.simulator` before any engine
module is imported. Nothing about the strategy, sizing, risk caps or execution
pre-flight is re-implemented here, so what is measured is what would trade.

Replay order per M5 bar (no lookahead):
    1. bar i-1 has closed  -> resolve stop/targets against its true high/low
    2. bar i has just opened -> clock jumps to i; equity, risk and signals are
       evaluated using only bar i's OPEN, and orders fill at that open
"""

import argparse
import json
import os
import sys
import tempfile
import time as walltime
from collections import Counter
from datetime import datetime, timezone

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import MetaTrader5 as real_mt5  # noqa: E402  (the real terminal, read-only)

from backtest import simulator as sim_mod  # noqa: E402
from backtest import fetch_data  # noqa: E402

_DT = sim_mod._DT
TF_NAMES = {5: "M5", 15: "M15", 30: "M30", 16385: "H1"}


# ---------------------------------------------------------------- data loading
def load_feeds(symbol: str, verbose: bool = True) -> dict:
    feeds = {}
    for tf_str in ("M5", "M15", "M30", "H1"):
        df = fetch_data.load(symbol, tf_str)
        if df is None or df.empty:
            raise SystemExit(f"No cached {tf_str} data for {symbol}. Run fetch_data.py first.")
        rec = np.empty(len(df), dtype=_DT)
        naive = df["datetime"].dt.tz_localize(None)
        rec["time"] = naive.to_numpy(dtype="datetime64[s]").astype("int64")
        rec["open"] = df["open"].to_numpy()
        rec["high"] = df["high"].to_numpy()
        rec["low"] = df["low"].to_numpy()
        rec["close"] = df["close"].to_numpy()
        rec["tick_volume"] = df["tick_volume"].fillna(0).to_numpy()
        rec["spread"] = 0
        rec["real_volume"] = rec["tick_volume"]
        order = np.argsort(rec["time"], kind="stable")
        feeds[tf_str] = sim_mod.Feed(tf=tf_str, times=rec["time"][order], bars=rec[order])
        if verbose:
            t0 = datetime.fromtimestamp(int(feeds[tf_str].times[0]), timezone.utc)
            t1 = datetime.fromtimestamp(int(feeds[tf_str].times[-1]), timezone.utc)
            print(f"  {tf_str:>4}  {len(df):>6} bars   {t0:%Y-%m-%d %H:%M} -> {t1:%Y-%m-%d %H:%M}")
    return feeds


def warmup_index(feeds: dict, min_h1: int = 200) -> int:
    """First M5 bar at which the H1 regime filter has enough history."""
    h1 = feeds["H1"]
    m5 = feeds["M5"]
    for i in range(len(m5.times)):
        if int(np.searchsorted(h1.times, m5.times[i], side="right")) >= min_h1:
            return i
    raise SystemExit("Not enough H1 history in the cached data to warm up the EMA200 filter.")


# ------------------------------------------------------------------ event log
class AuditLog:
    """Counts every desk event without retaining the stream."""

    def __init__(self):
        from event_log import EventLog  # imported after the fake MT5 is installed
        self._inner = EventLog(200)
        self.counts = Counter()
        self.blocks = Counter()
        self.errors = Counter()

    def record(self, level, message):
        self.counts[level] += 1
        if level == "BLOCK":
            self.blocks[_reason(message)] += 1
        elif level == "ERR":
            self.errors[_reason(message)] += 1
        self._inner.record(level, message)

    def info(self, m):
        self.record("INFO", m)

    def trade(self, m):
        self.record("TRADE", m)

    def block(self, m):
        self.record("BLOCK", m)

    def error(self, m):
        self.record("ERR", m)

    def recent(self, n, min_level="INFO"):
        return self._inner.recent(n, min_level)


def _reason(message: str) -> str:
    """Reduce a block message to its reason so repeats collapse."""
    if "spread $" in message:
        return "spread above ceiling"
    if "exceeds" in message and "ceiling" in message and "Size rejected" in message:
        return "lot size breaches hard risk ceiling"
    if "Size rejected" in message:
        return "lot size breaches hard risk ceiling"
    if "Min-lot floor" in message:
        return "min-lot floor engaged"
    if "OFFICE HALTED" in message:
        return "daily loss circuit breaker"
    if "Position cap reached" in message:
        return "max open positions"
    if "exposure cap reached" in message:
        return "max same-direction positions"
    if "already has" in message:
        return "employee position cap"
    if "benched for the day" in message:
        return "employee daily loss quota"
    if "daily trade cap" in message:
        return "employee daily trade cap"
    if "cooling down" in message:
        return "entry cooldown"
    if "outside" in message and "volatility regime" in message:
        return "ATR stop outside band"
    if "below broker minimum" in message:
        return "stop below broker minimum"
    if "below 1:" in message:
        return "effective RR below floor"
    if "degenerate SL/TP" in message:
        return "degenerate SL/TP"
    if "inverted bracket" in message:
        return "inverted bracket (target behind price)"
    if "employee is disabled" in message:
        return "employee disabled in config"
    if "margin check refused" in message:
        return "margin refused"
    if "not fully tradable" in message:
        return "symbol not tradable"
    if "no valid tick" in message:
        return "no tick"
    if "cannot resolve symbol" in message:
        return "symbol unresolvable"
    if "CLOSED" in message:
        return "position closed (result)"
    if "New UTC trading day" in message:
        return "utc day rollover"
    return message[:60]


# ----------------------------------------------------------------- clock patch
class _TimeShim:
    def __init__(self, clock):
        self._c = clock

    def time(self):
        return self._c.now()

    def monotonic(self):
        return self._c.now()

    def sleep(self, _s):
        return None

    def perf_counter(self):
        return walltime.perf_counter()


def patch_clocks(sim, modules) -> None:
    shim = _TimeShim(sim.clock)
    replay_dt = type("ReplayDateTime", (sim_mod.ReplayDateTime,), {"clock": sim.clock})
    for mod in modules:
        if hasattr(mod, "time"):
            mod.time = shim
        if hasattr(mod, "datetime"):
            mod.datetime = replay_dt


# --------------------------------------------------------------------- runner
def run(args) -> dict:
    symbol = args.symbol
    if not real_mt5.initialize():
        raise SystemExit("Could not initialise MetaTrader 5 to read the symbol specification.")
    symbol_info = real_mt5.symbol_info(symbol)
    if symbol_info is None:
        raise SystemExit(f"Symbol {symbol} not found in the terminal.")
    spec = {k: getattr(symbol_info, k) for k in (
        "digits", "point", "trade_contract_size", "volume_min", "volume_max",
        "volume_step", "trade_stops_level", "filling_mode", "trade_mode")}
    real_mt5.shutdown()
    print(f"Symbol spec from terminal: {spec}")

    print("Loading cached history...")
    feeds = load_feeds(symbol)
    m5 = feeds["M5"]
    start_i = warmup_index(feeds, args.min_h1)
    end_i = len(m5.times) if args.max_bars <= 0 else min(len(m5.times), start_i + args.max_bars)
    sim = sim_mod.Simulator(
        symbol=symbol, symbol_info=sim_mod.types.SimpleNamespace(**spec),
        feeds=feeds, start_epoch=float(m5.times[start_i]),
        initial_balance=args.balance, base_spread=args.spread,
        commission=args.commission, swap=args.swap,
    )
    print(f"Warmup ends at M5 bar {start_i} "
          f"({datetime.fromtimestamp(int(m5.times[start_i]), timezone.utc):%Y-%m-%d %H:%M} UTC)")
    print(f"Replaying bars {start_i}..{end_i} of {len(m5.times)} "
          f"on ${args.balance:,.2f} with ${args.spread:.2f} base spread\n")

    sim_mod.install(sim)

    import config
    import event_log as event_log_mod
    import market_data as market_data_mod
    import order_executor as order_executor_mod
    import risk_governor as risk_governor_mod
    import team_office as team_office_mod

    patch_clocks(sim, (market_data_mod, risk_governor_mod, event_log_mod))

    events = AuditLog()
    state_path = os.path.join(tempfile.gettempdir(), f"sldpax_bt_{os.getpid()}.json")
    for path in (state_path, state_path + ".corrupt"):
        if os.path.exists(path):
            os.remove(path)

    market_data = market_data_mod.MarketDataManager(symbol=symbol)
    governor = risk_governor_mod.RiskGovernor(events, state_path=state_path)
    governor.start()
    executor = order_executor_mod.OrderExecutor(governor, events)
    office = team_office_mod.TeamOffice(market_data, executor, events)

    bars = m5.bars
    t0 = walltime.perf_counter()
    step = max(1, (end_i - start_i) // 20)
    breaches = 0
    prev_breaker = False

    for i in range(start_i, end_i):
        if i > start_i:
            sim.process_exits(bars[i - 1])
        sim.advance_to_bar(bars[i])
        governor.update_daily_state()
        governor.reconcile()
        office.evaluate_all()
        sim.mark()
        if governor.circuit_breaker_triggered and not prev_breaker:
            breaches += 1
        prev_breaker = governor.circuit_breaker_triggered
        done = i - start_i
        if done % step == 0 and done:
            el = walltime.perf_counter() - t0
            eta = el / done * (end_i - i)
            print(f"  {datetime.fromtimestamp(int(bars[i]['time']), timezone.utc):%Y-%m-%d} "
                  f"{i - start_i:>6}/{end_i - start_i}  trades={len(sim.round_trips):>4} "
                  f"eq=${sim.equity():>10,.2f}  [{el:5.1f}s elapsed, {eta:5.1f}s left]", flush=True)

    # close anything still open at the end of the window at the final close
    sim.force_close_all(bars[end_i - 1])
    sim.mark()
    elapsed = walltime.perf_counter() - t0

    halt_flags = {
        "circuit_breaker": governor.circuit_breaker_triggered,
        "hard_cutoff": governor.hard_cutoff_triggered,
        "overall_dd_stop": governor.overall_dd_stopped,
        "emergency": governor.emergency_stop,
        "final_peak_equity": governor.peak_equity,
    }

    if os.path.exists(state_path):
        os.remove(state_path)

    return {
        "symbol": symbol, "spec": spec, "sim": sim, "events": events,
        "governor": governor, "start": int(m5.times[start_i]), "end": int(bars[end_i - 1]['time']),
        "bars": end_i - start_i, "elapsed": elapsed, "breaches": breaches,
        "halt_flags": halt_flags,
        "args": vars(args), "config_snapshot": _config_snapshot(config),
    }


def _config_snapshot(config) -> dict:
    keys = ("ACCOUNT_RISK_PERCENT", "MAX_SINGLE_TRADE_RISK_PCT", "MAX_DAILY_LOSS_PCT",
            "HARD_DAILY_LOSS_PCT", "DD_RISK_TIERS", "OVERALL_DD_STOP_PCT",
            "EMERGENCY_DD_PCT", "MAX_OPEN_POSITIONS", "MAX_POSITIONS_PER_EMP",
            "MAX_SAME_DIRECTION_POSITIONS",
            "MAX_DAILY_TRADES_PER_EMP", "MAX_DAILY_LOSSES_PER_EMP", "MIN_ENTRY_INTERVAL_SEC",
            "ATR_MULTIPLIER", "ATR_PERIOD", "MIN_STOP_DISTANCE_USD", "MAX_STOP_DISTANCE_USD",
            "BREAKOUT_BUFFER_ATR", "MAX_SPREAD_USD", "MAX_DEVIATION_POINTS",
            "MIN_ACCEPTABLE_RR", "MIN_HISTORY_BARS", "BAR_SIGNAL_GRACE_SEC", "SYMBOL")
    return {k: getattr(config, k) for k in keys if hasattr(config, k)}


# ----------------------------------------------------------------------- main
def main() -> int:
    p = argparse.ArgumentParser(description="One-year SLDPAX historical backtest")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--balance", type=float, default=10_000.0)
    p.add_argument("--spread", type=float, default=sim_mod.BASE_SPREAD_USD)
    p.add_argument("--commission", type=float, default=sim_mod.COMMISSION_PER_LOT)
    p.add_argument("--swap", type=float, default=sim_mod.SWAP_USD_PER_LOT_DAY)
    p.add_argument("--max-bars", type=int, default=0, help="0 = full history")
    p.add_argument("--min-h1", type=int, default=200)
    p.add_argument("--out", default=os.path.join(HERE, "results"))
    p.add_argument("--tag", default="base")
    args = p.parse_args()

    result = run(args)

    from backtest import report
    os.makedirs(args.out, exist_ok=True)
    paths = report.write(result, args.out, args.tag)
    report.print_console(result, paths)
    return 0


if __name__ == "__main__":
    sys.exit(main())
