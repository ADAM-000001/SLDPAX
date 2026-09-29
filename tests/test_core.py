"""Offline verification of the rectified SLDPAX engine."""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mt5_stub import (  # noqa: E402
    FAILS, STATE, bar, check, fresh_account, obj, reset_orders, summary, synthetic,
)
import mt5_stub  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import config  # noqa: E402
import event_log as event_log_mod  # noqa: E402
import market_data as md_mod  # noqa: E402
import order_executor as exec_mod  # noqa: E402
import risk_governor as risk_mod  # noqa: E402
import team_office as team_mod  # noqa: E402

md = md_mod.MarketDataManager("XAUUSD")
Gov = risk_mod.RiskGovernor
events = event_log_mod.EventLog(300)
TMP = tempfile.mkdtemp()
SP = os.path.join(TMP, "risk_state.json")


def frame(rows):
    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df


def new_gov(equity=10000.0, margin_mode=2, **over):  # 2 = RETAIL_HEDGING (live MT5)
    fresh_account(equity, margin_mode)
    g = Gov(events, SP)
    g.update_daily_state(force=True)
    g.day_start_equity = equity
    g.circuit_breaker_triggered = False
    g.hard_cutoff_triggered = False
    g.peak_equity = equity
    g._flushed_peak = equity
    g.overall_dd_stopped = False
    g.emergency_stop = False
    g._processed_deals.clear()
    g._tracked.clear()
    g._pos_emp_map.clear()
    for e in range(1, 9):
        g.daily_emp_trades[e] = 0
        g.daily_emp_wins[e] = 0
        g.daily_emp_losses[e] = 0
        g.last_trade_time[e] = 0.0
    for k, v in over.items():
        setattr(g, k, v)
    return g


# ===================================================================== 1
print("\n[1] Indicator fidelity: Wilder RMA (matches MT5)")
rng = np.random.default_rng(42)
n = 300
close = 2400 + np.cumsum(rng.normal(0, 1.0, n))
pdf = pd.DataFrame({
    "time": np.arange(n), "open": close,
    "high": close + rng.uniform(0.2, 1.5, n),
    "low": close - rng.uniform(0.2, 1.5, n),
    "close": close, "tick_volume": 100,
    "datetime": pd.to_datetime(np.arange(n), unit="s", utc=True),
})
period = config.ATR_PERIOD
tr = md_mod.MarketDataManager.true_range(pdf)
ref = float(pd.Series(tr.values[1:period + 1]).mean())
for i in range(period + 1, n):
    ref = (ref * (period - 1) + float(tr.iloc[i])) / period
got = float(md.calculate_atr(pdf, period).iloc[-1])
check("ATR matches an independent Wilder recursion", abs(got - ref) / ref < 1e-9,
      f"got={got:.8f} ref={ref:.8f}")
check("ATR is NOT the old SMA variant", abs(got - float(tr.rolling(period).mean().iloc[-1])) > 1e-6)

adx, pdi, mdi = md.calculate_adx(pdf, period)
check("ADX within 0..100", 0.0 <= adx.iloc[-1] <= 100.0, str(adx.iloc[-1]))
check("+DI and -DI within 0..100", 0.0 <= pdi.iloc[-1] <= 100.0 and 0.0 <= mdi.iloc[-1] <= 100.0)
flat = pd.DataFrame({"high": [10.0] * 80, "low": [10.0] * 80, "close": [10.0] * 80,
                     "datetime": pd.to_datetime(range(80), unit="s", utc=True)})
check("ADX is undefined (NaN) when ATR is zero, not a bogus number",
      bool(pd.isna(md.calculate_adx(flat, period)[0].iloc[-1])))
trend = []
lv = 100.0
for i in range(80):
    lv += 1.0
    trend.append(bar(1000 + i * 900, lv - 1.0, lv + 0.3, lv - 1.3, lv))
adx_t, _, _ = md.calculate_adx(frame(trend), period)
check("ADX is high in a clean uptrend", float(adx_t.iloc[-1]) > 40, str(adx_t.iloc[-1]))

# ===================================================================== 2
print("\n[2] NR7 off-by-one correction")
rows = [bar(1000 + i * 900, 100.0, 105.0, 95.0, 100.0) for i in range(12)]
rows[9]["high"], rows[9]["low"] = 100.4, 99.6   # bar -3 is the compression bar
ndf = frame(rows)
check("is_nr7(offset=-3) flags the compression bar", md.is_nr7(ndf, offset=-3) is True)
check("is_nr7(offset=-2) is False once the next bar expands", md.is_nr7(ndf, offset=-2) is False)
check("is_nr7 is False on an undersized frame", md.is_nr7(ndf.head(3), offset=-3) is False)

# ===================================================================== 3
print("\n[3] Fair Value Gap detection")
frows = [bar(1000 + i * 900, 100.0, 101.0, 99.0, 100.0) for i in range(12)]
frows[6]["high"] = 99.5          # high[6]
frows[8]["low"] = 102.0          # low[8] > high[6]  -> bullish FVG at index 8
frows[8]["close"] = 103.0
gaps = md.find_fvgs(frame(frows), lookback=12, end_offset=-2, min_gap=0.1)
bull = [g for g in gaps if g["type"] == "bullish"]
check("bullish FVG is detected", len(bull) == 1, str(gaps))
if bull:
    g = bull[0]
    check("FVG bounds are the void edges",
          abs(g["bottom"] - 99.5) < 1e-9 and abs(g["top"] - 102.0) < 1e-9, str(g))
    check("FVG midpoint is the mean of the void", abs(g["mid"] - 100.75) < 1e-9)
check("gaps never sit on the signal bar", all(g["pos"] <= len(frows) - 3 for g in gaps))
srows = [bar(1000 + i * 900, 100.0, 101.0, 99.0, 100.0) for i in range(12)]
srows[6]["low"] = 100.5
srows[8]["high"] = 98.0
check("bearish FVG is detected",
      any(g["type"] == "bearish" for g in md.find_fvgs(frame(srows), lookback=12, end_offset=-2, min_gap=0.1)))
check("min_gap filters noise gaps",
      md.find_fvgs(frame(frows), lookback=12, end_offset=-2, min_gap=500.0) == [])

# ===================================================================== 4
print("\n[4] Position sizing and the hard risk ceiling")
g = new_gov()
lot, risk = g.calculate_lot_size(2.00)
check("lot sized for 0.35% of a $10k equity", abs(lot - 0.17) < 1e-9, f"lot={lot}")
check("actual risk equals lot x contract x stop", abs(risk - lot * 100.0 * 2.00) < 1e-9)
check("actual risk is within the target", risk <= 35.0 + 1e-9, f"risk={risk}")
lot2, _ = g.calculate_lot_size(100.0)
check("a stop so wide that min-lot breaches the ceiling is REFUSED", lot2 is None, f"lot={lot2}")
lot4, risk4 = g.calculate_lot_size(40.0)
check("min-lot floor is engaged but stays inside the hard ceiling",
      lot4 == 0.01 and risk4 == 40.0, f"lot={lot4} risk={risk4}")
check("the min-lot overshoot is surfaced as a warning event",
      any("Min-lot floor" in e["message"] for e in events.recent(50)))
check("min-lot never exceeds MAX_SINGLE_TRADE_RISK_PCT",
      risk4 <= 10000.0 * config.MAX_SINGLE_TRADE_RISK_PCT / 100.0 + 1e-9, f"risk={risk4}")
check("a stop that fits the grid exactly never overshoots",
      g.calculate_lot_size(2.00)[1] <= 35.0 + 1e-9)
snap = Gov._snap_to_step
check("_snap_to_step floors to the lot grid", abs(snap(0.1799, 0.01, 0.01, 10) - 0.17) < 1e-9)
check("_snap_to_step honours volume_min", abs(snap(0.0001, 0.01, 0.01, 10) - 0.01) < 1e-9)
check("_snap_to_step honours volume_max", abs(snap(99.0, 0.01, 0.01, 5.0) - 5.0) < 1e-9)
check("_snap_to_step handles a 0.001 step cleanly", abs(snap(0.12345, 0.001, 0.001, 10) - 0.123) < 1e-9)
check("_snap_to_step has no float drift", snap(0.07, 0.01, 0.01, 10) == 0.07)

# ===================================================================== 5
print("\n[5] Crash-safe persistence")
g = new_gov()
g.daily_emp_losses[3] = 1
g.daily_emp_wins[2] = 4
g.circuit_breaker_triggered = True
g.peak_equity = 12345.0
g.emergency_stop = True
g.last_trade_time[5] = 12345.0
g._save()
check("risk_state.json is written", os.path.exists(SP))
check("no .tmp file is left behind", not any(f.endswith(".tmp") for f in os.listdir(TMP)),
      str(os.listdir(TMP)))

g2 = Gov(events, SP)
g2.update_daily_state(force=True)
check("day-start equity anchor survives a restart", abs(g2.day_start_equity - 10000.0) < 1e-9,
      str(g2.day_start_equity))
check("daily losses survive a restart", g2.daily_emp_losses[3] == 1)
check("daily wins survive a restart", g2.daily_emp_wins[2] == 4)
check("circuit breaker stays latched across a restart", g2.circuit_breaker_triggered is True)
check("the equity peak survives a restart", abs(g2.peak_equity - 12345.0) < 1e-9,
      str(g2.peak_equity))
check("the emergency cutoff survives a restart", g2.emergency_stop is True)
check("cooldown timestamps survive a restart", abs(g2.last_trade_time[5] - 12345.0) < 1e-6)
allowed, reason = g2.is_trade_allowed(1)
check("a tripped desk refuses new trades after a restart", not allowed, reason)

corrupt = os.path.join(TMP, "bad.json")
with open(corrupt, "w", encoding="utf-8") as fh:
    fh.write("{not json")
store = risk_mod.RiskStateStore(corrupt)
check("a corrupt state file is quarantined, not crashed on", store.load() is None)
check("the corrupt file is renamed for inspection", os.path.exists(corrupt + ".corrupt"))

# ===================================================================== 6
print("\n[6] Deal reconciliation is incremental and idempotent")
g = new_gov(equity=9965.0)
pid = 777
g._pos_emp_map[pid] = 1
g._tracked[pid] = {"emp_id": 1, "side": "BUY", "pnl": 0.0, "ticket": 555}
STATE["positions"] = []
STATE["deals"] = [obj(ticket=90001, position_id=pid, magic=101001,
                      entry=mt5_stub.mt5.DEAL_ENTRY_OUT,
                      profit=-35.0, swap=-1.5, commission=-0.5)]
for _ in range(5):
    g.reconcile(force=True)
check("one loss is counted exactly once after 5 reconciles", g.daily_emp_losses[1] == 1,
      f"losses={g.daily_emp_losses[1]}")
check("the loss bucket is not inflated", g.daily_emp_losses[2] == 0)

pid2 = 778
g._tracked[pid2] = {"emp_id": 2, "side": "SELL", "pnl": 0.0, "ticket": 556}
g._pos_emp_map[pid2] = 2
STATE["deals"].append(obj(ticket=90002, position_id=pid2, magic=101002,
                          entry=mt5_stub.mt5.DEAL_ENTRY_OUT,
                          profit=70.0, swap=-2.0, commission=-0.5))
for _ in range(5):
    g.reconcile(force=True)
check("one win is counted exactly once", g.daily_emp_wins[2] == 1, f"wins={g.daily_emp_wins[2]}")
check("closed positions leave the tracked book", pid not in g._tracked and pid2 not in g._tracked)

pid3 = 779
g._tracked[pid3] = {"emp_id": 3, "side": "BUY", "pnl": 0.0, "ticket": 557}
g._pos_emp_map[pid3] = 3
STATE["positions"] = [obj(ticket=9001, identifier=pid3, magic=101003,
                          type=mt5_stub.mt5.ORDER_TYPE_BUY, volume=0.1,
                          price_open=2400.0, sl=2390.0, tp=2420.0, profit=5.0)]
STATE["deals"].append(obj(ticket=90003, position_id=pid3, magic=101003,
                          entry=mt5_stub.mt5.DEAL_ENTRY_OUT,
                          profit=10.0, swap=0.0, commission=0.0))
g.reconcile(force=True)
check("a partial close scores nothing while the position is open", g.daily_emp_wins[3] == 0)
check("a partial close accumulates PnL on the position", abs(g._tracked[pid3]["pnl"] - 10.0) < 1e-9)
STATE["positions"] = []
g.reconcile(force=True)
check("the full close then scores exactly one win", g.daily_emp_wins[3] == 1,
      f"wins={g.daily_emp_wins[3]}")

pid4 = 780
g._tracked[pid4] = {"emp_id": 4, "side": "BUY", "pnl": 0.0, "ticket": 558}
STATE["positions"] = None
STATE["deals"] = []
g.reconcile(force=True)
check("positions_get()=None does NOT finalize a position", pid4 in g._tracked)

gnet = new_gov()
STATE["positions"] = []
gnet._tracked[880] = {"emp_id": 4, "side": "SELL", "pnl": 0.0, "ticket": 559}
STATE["deals"] = [obj(ticket=90004, position_id=880, magic=101004,
                      entry=mt5_stub.mt5.DEAL_ENTRY_OUT, profit=-10.0, swap=0.0, commission=0.0)]
gnet.reconcile(force=True)
check("a position attributed only by magic is still scored", gnet.daily_emp_losses[4] == 1,
      f"losses={gnet.daily_emp_losses[4]}")

# ===================================================================== 7
print("\n[7] Exposure caps")
g = new_gov()
STATE["positions"] = [
    obj(ticket=1, identifier=11, magic=101001, type=mt5_stub.mt5.ORDER_TYPE_BUY,
        volume=0.1, price_open=2400.0, sl=2390.0, tp=2420.0, profit=0.0),
]
ok, why = g.is_trade_allowed(1, "BUY")
check("the single-position cap blocks a second trade", not ok and ("Position cap" in why or "already has" in why), why)
ok, why = g.is_trade_allowed(3, "SELL")
check("one open position blocks any other office trade", not ok and "Position cap" in why, why)
check("net_exposure sees the one long",
      g.net_exposure() == (1, 0), str(g.net_exposure()))
STATE["positions"] = []
g.daily_emp_trades[5] = config.MAX_DAILY_TRADES_PER_EMP
ok, why = g.is_trade_allowed(5, "BUY")
check("the daily trade cap is enforced", not ok and "trade cap" in why, why)
g.daily_emp_trades[5] = 0
g.last_trade_time[6] = time.time()
ok, why = g.is_trade_allowed(6, "BUY")
check("the entry cooldown is enforced", not ok and "cooling down" in why, why)
g.last_trade_time[6] = 0.0
g.daily_emp_losses[7] = 1
ok, why = g.is_trade_allowed(7, "BUY")
check("an employee with a loss is benched", not ok and "benched" in why, why)
g.daily_emp_losses[7] = 0
STATE["positions"] = [
    obj(ticket=i + 1, identifier=20 + i, magic=101001 + i,
        type=mt5_stub.mt5.ORDER_TYPE_BUY, volume=0.1, price_open=2400.0,
        sl=2390.0, tp=2420.0, profit=0.0) for i in range(config.MAX_OPEN_POSITIONS)
]
ok, why = g.is_trade_allowed(8, "BUY")
check("the office position cap is enforced", not ok and "Position cap" in why, why)

# The same-direction correlation cap is a separate gate that only bites when the
# position cap is relaxed; exercise its code path with loosened caps.
old_open, old_dir = risk_mod.MAX_OPEN_POSITIONS, risk_mod.MAX_SAME_DIRECTION_POSITIONS
try:
    risk_mod.MAX_OPEN_POSITIONS, risk_mod.MAX_SAME_DIRECTION_POSITIONS = 3, 2
    STATE["positions"] = [
        obj(ticket=i + 1, identifier=30 + i, magic=101001 + i,
            type=mt5_stub.mt5.ORDER_TYPE_BUY, volume=0.1, price_open=2400.0,
            sl=2390.0, tp=2420.0, profit=0.0) for i in range(2)
    ]
    ok, why = g.is_trade_allowed(3, "BUY")
    check("a third same-side long is blocked", not ok and "exposure cap" in why, why)
    ok, why = g.is_trade_allowed(3, "SELL")
    check("an opposite-side trade is NOT blocked by the long cap", ok, why)
finally:
    risk_mod.MAX_OPEN_POSITIONS, risk_mod.MAX_SAME_DIRECTION_POSITIONS = old_open, old_dir

# ===================================================================== 7b
print("\n[7b] Drawdown tiers and emergency latch")
g = new_gov()
check("fresh account has zero drawdown and default risk", g.overall_dd_pct() == 0.0
      and abs(g.current_risk_percent() - config.ACCOUNT_RISK_PERCENT) < 1e-9,
      f"dd={g.overall_dd_pct():.2f} risk={g.current_risk_percent()}")
STATE["account"].equity = 9700.0
g.update_daily_state(force=True)
check("risk drops to 0.25% once down 3% from peak",
      g.overall_dd_pct() >= 3.0 and abs(g.current_risk_percent() - 0.25) < 1e-9,
      f"dd={g.overall_dd_pct():.2f} risk={g.current_risk_percent()}")
STATE["account"].equity = 9500.0
g.update_daily_state(force=True)
check("risk drops to 0.125% once down 5% from peak",
      abs(g.current_risk_percent() - 0.125) < 1e-9, str(g.current_risk_percent()))
STATE["account"].equity = 9400.0
g.update_daily_state(force=True)
check("6% drawdown latches the overall DD protection",
      g.overall_dd_stopped is True, str(g.overall_dd_stopped))
ok, why = g.is_trade_allowed(1, "BUY")
check("DD protection blocks new entries", not ok and "DD PROTECTION" in why.upper(), why)
STATE["account"].equity = 9300.0
g.update_daily_state(force=True)
check("7% drawdown sets the absolute emergency latch", g.emergency_stop is True,
      str(g.emergency_stop))
ok, why = g.is_trade_allowed(1, "BUY")
check("the emergency latch refuses everything", not ok and "EMERGENCY" in why, why)

g2 = new_gov()
reset_orders()
STATE["positions"] = [
    obj(ticket=41, identifier=41, magic=101001, type=mt5_stub.mt5.ORDER_TYPE_BUY,
        volume=0.1, price_open=2400.0, sl=2390.0, tp=2420.0, profit=0.0),
    obj(ticket=42, identifier=42, magic=101002, type=mt5_stub.mt5.ORDER_TYPE_BUY,
        volume=0.1, price_open=2400.0, sl=2390.0, tp=2420.0, profit=0.0),
]
g2.peak_equity = 10000.0
g2.current_equity = 9300.0
g2._update_latches()
check("the emergency flatten sends a close-by-position DEAL per office position",
      len(STATE["sent"]) == 2, str(STATE["sent"]))
check("every close clears the open book",
      all(rq.get("position") is not None for rq in STATE["sent"]))
check("the emergency comment is attached to each close",
      all(rq["comment"] == "EMERGENCY_DD_CUTOFF"[:20] for rq in STATE["sent"]))
check("positions are gone after the flatten", STATE["positions"] == [])

g3 = new_gov()
g3.peak_equity = 10000.0
g3.overall_dd_stopped = True
STATE["account"].equity = 9800.0
g3.current_equity = 9800.0
g3._rollover_day("2099-01-01", 9800.0)
check("DD protection re-arms once recovered below 6% on a new day",
      g3.overall_dd_stopped is False)
g3.emergency_stop = True
g3._rollover_day("2099-01-02", 9800.0)
check("the emergency cutoff is NEVER auto-cleared by a new day", g3.emergency_stop is True)

# ===================================================================== 7c
print("\n[7c] Hard daily safety cutoff (3.0%)")
g = new_gov()
g.day_start_equity = 10000.0
STATE["account"].equity = 9680.0
g.update_daily_state(force=True)
check("a -3.2% day trips the hard cutoff latch", g.hard_cutoff_triggered is True,
      f"day_pnl={g.day_pnl:.2f}")
check("the hard cutoff implies the daily breaker too", g.circuit_breaker_triggered is True)
ok, why = g.is_trade_allowed(1, "BUY")
check("the hard cutoff blocks new entries", not ok and "hard daily" in why.lower(), why)

print("\n[7b] Account mode gate")
gn = new_gov(margin_mode=0)
raised = ""
try:
    gn.start()
except RuntimeError as exc:
    raised = str(exc)
check("a NETTING account is refused at startup", "NETTING" in raised, raised[:60])
gh = new_gov(margin_mode=2)
gh.start()
check("a HEDGING account starts cleanly", gh.hedging_mode is True)

# ===================================================================== 8
print("\n[8] Executor pre-flight gauntlet")
g = new_gov()
STATE["positions"] = []
reset_orders()
STATE["tick"] = obj(bid=2400.50, ask=2400.70)
ex = exec_mod.OrderExecutor(g, events)
res = ex.execute_order(1, "BUY", 2.0, "T1")
check("a valid order reaches the broker", res is not None and len(STATE["sent"]) == 1)
check("margin was validated before sending", len(STATE["checks"]) == 1)
if res:
    rq = STATE["sent"][0]
    check("magic is derived from the employee id", rq["magic"] == config.magic_for(1) == 101001)
    check("comment carries the employee id", rq["comment"].startswith("Emp1_"))
    check("BUY stop is below entry", rq["sl"] < rq["price"])
    check("BUY target is 2x the stop (RR 1:2)",
          abs((rq["tp"] - rq["price"]) - 2 * (rq["price"] - rq["sl"])) < 0.011)
    check("risk is reported back to the caller", res["risk_usd"] > 0)
    check("fill mode follows the symbol's supported bits", rq["type_filling"] == mt5_stub.mt5.ORDER_FILLING_FOK)
g.last_trade_time[2] = 0.0
STATE["tick"] = obj(bid=2400.50, ask=2405.00)
reset_orders()
check("a blown spread is refused", ex.execute_order(2, "BUY", 2.0, "T2") is None and not STATE["sent"])
STATE["tick"] = obj(bid=2400.50, ask=2400.70)

for label, stop in (("noise floor", 0.05), ("volatility ceiling", 40.0),
                    ("broker stops-level", 0.15)):
    g.last_trade_time[2] = 0.0
    reset_orders()
    check(f"a stop outside limits ({label}) is refused",
          ex.execute_order(2, "BUY", stop, "T") is None and not STATE["sent"], f"stop={stop}")

g.last_trade_time[2] = 0.0
reset_orders()
check("a structural TP with poor R:R is refused",
      ex.execute_order(2, "SELL", 2.0, "T6", tp_price=2402.0) is None and not STATE["sent"])
g.last_trade_time[2] = 0.0
reset_orders()
r = ex.execute_order(2, "SELL", 2.0, "T7", tp_price=2390.0)
check("a structural TP with good R:R is accepted", r is not None)
if r:
    check("the structural TP is used verbatim", abs(r["tp"] - 2390.0) < 0.011, str(r["tp"]))

g.last_trade_time[2] = 0.0
reset_orders()
STATE["order_check_retcode"] = mt5_stub.mt5.TRADE_RETCODE_REQUOTE
check("a failed margin check blocks the send",
      ex.execute_order(2, "BUY", 2.0, "T8") is None and not STATE["sent"])
STATE["order_check_retcode"] = mt5_stub.mt5.TRADE_RETCODE_DONE
g.last_trade_time[2] = 0.0
reset_orders()
trades_before = g.daily_emp_trades[2]
STATE["order_send_retcode"] = mt5_stub.mt5.TRADE_RETCODE_INVALID_STOPS
check("a broker rejection is not counted as a filled trade",
      ex.execute_order(2, "BUY", 2.0, "T9") is None and g.daily_emp_trades[2] == trades_before,
      f"before={trades_before} after={g.daily_emp_trades[2]}")
check("a rejected order starts no cooldown", g.last_trade_time[2] == 0.0)
STATE["order_send_retcode"] = mt5_stub.mt5.TRADE_RETCODE_DONE

STATE["symbol"] = obj(filling_mode=0, visible=True, digits=2, point=0.01,
                      trade_contract_size=100.0, volume_min=0.01, volume_max=10.0,
                      volume_step=0.01, trade_stops_level=10, trade_mode=0)
g.last_trade_time[2] = 0.0
reset_orders()
check("a non-tradable symbol is refused", ex.execute_order(2, "BUY", 2.0, "TA") is None and not STATE["sent"])
STATE["symbol"] = obj(filling_mode=1, visible=True, digits=2, point=0.01,
                      trade_contract_size=100.0, volume_min=0.01, volume_max=10.0,
                      volume_step=0.01, trade_stops_level=10, trade_mode=4)

print(f"\n{'=' * 60}")
sys.exit(summary())
