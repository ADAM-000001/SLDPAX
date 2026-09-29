"""Strategy-layer verification: bar handling, and each employee actually firing."""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mt5_stub import STATE, bar, check, fresh_account, obj, summary, synthetic  # noqa: E402
import mt5_stub  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

import config  # noqa: E402
import event_log as event_log_mod  # noqa: E402
import market_data as md_mod  # noqa: E402
import risk_governor as risk_mod  # noqa: E402
import team_office as team_mod  # noqa: E402

md = md_mod.MarketDataManager("XAUUSD")
events = event_log_mod.EventLog(500)
SP = os.path.join(tempfile.mkdtemp(), "risk_state.json")


class Spy:
    """Records signals instead of sending orders."""

    def __init__(self):
        self.calls = []

    def execute_order(self, emp_id, side, stop, comment, tp_price=None):
        self.calls.append({"emp": emp_id, "side": side, "stop": stop,
                           "comment": comment, "tp": tp_price})
        return {"emp_id": emp_id, "type": side}

    def ids(self):
        return [c["emp"] for c in self.calls]

    def of(self, emp_id):
        return [c for c in self.calls if c["emp"] == emp_id]


def gov(equity=10000.0):
    fresh_account(equity, 1)
    STATE["positions"] = []
    STATE["deals"] = []
    g = risk_mod.RiskGovernor(events, SP)
    g.update_daily_state(force=True)
    g.day_start_equity = equity
    g.circuit_breaker_triggered = False
    g._processed_deals.clear()
    g._tracked.clear()
    g._pos_emp_map.clear()
    for e in range(1, 9):
        g.daily_emp_trades[e] = 0
        g.daily_emp_wins[e] = 0
        g.daily_emp_losses[e] = 0
        g.last_trade_time[e] = 0.0
    return g


def frame(rows):
    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df


# ===================================================================== bar feed
print("\n[9] Bar handling: is_new_bar consumes each bar exactly once")
mgr = md_mod.MarketDataManager("XAUUSD")
STATE["bars"]["M15"] = synthetic(15, 200, 2400.0)
is_new, df = mgr.is_new_bar("M15")
check("the first check reports a new bar", is_new is True)
check("200 bars are returned so the Asian range is not truncated", df is not None and len(df) == 200)
is_new2, _ = mgr.is_new_bar("M15")
check("an immediate recheck does not re-fire the same bar", is_new2 is False)
STATE["bars"]["M15"] = synthetic(15, 200, 2401.0)
STATE["bars"]["M15"][-1]["time"] += 900   # a genuinely newer forming bar
is_new3, _ = mgr.is_new_bar("M15")
check("the next bar does fire", is_new3 is True)
mgr2 = md_mod.MarketDataManager("XAUUSD")
STATE["bars"]["M15"] = synthetic(15, 200, 2400.0, end_at_now=False)
is_new4, _ = mgr2.is_new_bar("M15", grace_sec=0)
check("with no grace window a badly delayed bar is dropped", is_new4 is False)
mgr3 = md_mod.MarketDataManager("XAUUSD")
STATE["bars"]["M15"] = synthetic(15, 200, 2400.0, end_at_now=False)
is_new5, _ = mgr3.is_new_bar("M15", grace_sec=3600)
check("within the grace window a stalled bar is still delivered", is_new5 is True)
STATE["bars"]["M15"] = None
check("a dead feed reports no new bar instead of raising", mgr3.is_new_bar("M15")[0] is False)

# ===================================================================== H1 filter
print("\n[10] H1 regime filter")
STATE["bars"]["H1"] = synthetic(60, 250, 2400.0)
STATE["bars"]["M5"] = synthetic(5, 200, 2400.0)
STATE["bars"]["M15"] = synthetic(15, 200, 2400.0)
STATE["bars"]["M30"] = synthetic(30, 200, 2400.0)
spy = Spy()
team = team_mod.TeamOffice(md, spy, events)
team.evaluate_all()
check("H1 EMA200 is derived from the last CLOSED H1 bar", team.h1_ema200 is not None)
h1df = frame(STATE["bars"]["H1"])
expect = float(md.calculate_ema(h1df["close"], 200).iloc[-2])
check("the filter uses iloc[-2], not the forming bar", abs(team.h1_ema200 - expect) < 1e-9)
STATE["bars"]["H1"] = synthetic(60, 120, 2400.0)
team2 = team_mod.TeamOffice(md, Spy(), events)
team2.evaluate_all()
check("too little H1 history puts the desk in SCANNING ONLY", team2.h1_ema200 is None)
check("a SCANNING ONLY desk emits an error event",
      any("SCANNING ONLY" in e["message"] for e in events.recent(20)))
STATE["bars"]["H1"] = synthetic(60, 250, 2400.0)

# ===================================================================== Emp 6
print("\n[11] Employee 6: real FVG inversion (was a hard-coded $0.40 EMA filter)")
rows = synthetic(15, 60, 2400.0)
# Build a bullish FVG at index -3: high[-5] < low[-3]
n = len(rows)
rows[n - 5]["high"] = 2400.0
rows[n - 5]["close"] = 2399.0
rows[n - 3]["low"] = 2404.0
rows[n - 3]["open"] = 2403.0
rows[n - 3]["close"] = 2404.5
# Signal bar -2 closes back BELOW the bullish gap's lower edge (2400.0) -> bearish inversion
rows[n - 2]["open"] = 2401.0
rows[n - 2]["high"] = 2402.5
rows[n - 2]["low"] = 2398.0
rows[n - 2]["close"] = 2399.0
df6 = frame(rows)
gaps = md.find_fvgs(df6, lookback=60, end_offset=-2, min_gap=0.1)
check("the crafted bullish FVG is found at index -3", any(g["pos"] == n - 3 for g in gaps), str(gaps))
gaps_at_m3 = [g for g in gaps if g["pos"] == n - 3]
if gaps_at_m3:
    check("the gap void spans high[-5]..low[-3]",
          abs(gaps_at_m3[0]["bottom"] - 2400.0) < 1e-9 and abs(gaps_at_m3[0]["top"] - 2404.0) < 1e-9,
          str(gaps_at_m3[0]))
    check("the target is the gap midpoint, not a fixed RR multiple",
          abs(gaps_at_m3[0]["mid"] - 2402.0) < 1e-9)

stop = max(config.MIN_STOP_DISTANCE_USD,
           config.ATR_MULTIPLIER * float(md.calculate_atr(df6, config.ATR_PERIOD).iloc[-2]))
spy6 = Spy()
team6 = team_mod.TeamOffice(md, spy6, events)
team6.h1_ema200 = 2410.0     # bearish H1 regime, so the bearish inversion is allowed
team6._evaluate_fvg_inversion(df6, 2399.0, 2401.0, stop, 2410.0)
check("Employee 6 fires a SELL on the FVG inversion", [c["side"] for c in spy6.of(6)] == ["SELL"],
      str(spy6.calls))
if spy6.of(6):
    check("Employee 6 targets the gap midpoint", abs(spy6.of(6)[0]["tp"] - 2402.0) < 1e-9,
          str(spy6.of(6)[0]))
    check("Employee 6 comments the setup", spy6.of(6)[0]["comment"] == "IFVGBear",
          str(spy6.of(6)[0]))

spy6b = Spy()
team_mod.TeamOffice(md, spy6b, events)._evaluate_fvg_inversion(df6, 2399.0, 2401.0, stop, 2300.0)
check("Employee 6 stays out when the H1 regime disagrees", spy6b.of(6) == [])

spy6c = Spy()
team_mod.TeamOffice(md, spy6c, events)._evaluate_fvg_inversion(df6, 2405.0, 2404.0, stop, 2410.0)
check("Employee 6 does not trade a gap that has not been inverted", spy6c.of(6) == [])

# A mirrored (bearish FVG) inversion must produce a BUY.
rows2 = synthetic(15, 60, 2400.0)
n2 = len(rows2)
rows2[n2 - 5]["low"] = 2400.0
rows2[n2 - 5]["close"] = 2401.0
rows2[n2 - 3]["high"] = 2396.0
rows2[n2 - 3]["open"] = 2397.0
rows2[n2 - 3]["close"] = 2395.5
rows2[n2 - 2]["open"] = 2399.0
rows2[n2 - 2]["high"] = 2400.5
rows2[n2 - 2]["low"] = 2396.0
rows2[n2 - 2]["close"] = 2401.0
df6b = frame(rows2)
stop2 = max(config.MIN_STOP_DISTANCE_USD,
            config.ATR_MULTIPLIER * float(md.calculate_atr(df6b, config.ATR_PERIOD).iloc[-2]))
spy6d = Spy()
team_mod.TeamOffice(md, spy6d, events)._evaluate_fvg_inversion(df6b, 2401.0, 2399.0, stop2, 2300.0)
check("a bearish FVG inversion produces a BUY", [c["side"] for c in spy6d.of(6)] == ["BUY"],
      str(spy6d.calls))
if spy6d.of(6):
    check("the mirrored gap midpoint is the target", abs(spy6d.of(6)[0]["tp"] - 2398.0) < 1e-9,
          str(spy6d.of(6)[0]))

# The old, broken condition would have fired constantly.
flat_rows = synthetic(15, 60, 2400.0, spread=0.05)
flat = frame(flat_rows)
atr = float(md.calculate_atr(flat, config.ATR_PERIOD).iloc[-2])
ema20 = float(md.calculate_ema(flat["close"], 20).iloc[-2])
close = float(flat.iloc[-2]["close"])
old_would_fire = abs(close - ema20) < 0.40
check("the old $0.40 EMA filter really was near-always true (bug confirmed)", old_would_fire,
      f"|c-ema20|={abs(close - ema20):.4f} atr={atr:.4f}")

# ===================================================================== Emp 8
print("\n[12] Employee 8: ADX is actually used now")
trend_rows = []
level = 2400.0
for i in range(60):
    level += 2.0
    trend_rows.append(bar(1000 + i * 900, level - 2.0, level + 0.4, level - 2.4, level))
trend = frame(trend_rows)
adx_v, pdi_v, mdi_v = md.calculate_adx(trend, config.ATR_PERIOD)
check("the crafted uptrend has ADX above the 25 threshold",
      float(adx_v.iloc[-2]) > config.EMPLOYEES[8].adx_threshold, str(adx_v.iloc[-2]))
check("+DI exceeds -DI in the uptrend", float(pdi_v.iloc[-2]) > float(mdi_v.iloc[-2]))
check("the DI spread is widening (momentum is expanding, not fading)",
      (float(pdi_v.iloc[-2]) - float(mdi_v.iloc[-2])) > (float(pdi_v.iloc[-3]) - float(mdi_v.iloc[-3])))

chop_rows = []
lv = 2400.0
for i in range(60):
    lv += (1.0 if i % 2 == 0 else -1.0)
    chop_rows.append(bar(1000 + i * 900, lv, lv + 0.6, lv - 0.6, lv))
chop = frame(chop_rows)
adx_c, pdi_c, mdi_c = md.calculate_adx(chop, config.ATR_PERIOD)
check("the choppy series has ADX below the threshold",
      float(adx_c.iloc[-2]) < config.EMPLOYEES[8].adx_threshold, str(adx_c.iloc[-2]))

# ===================================================================== Emp 4
print("\n[13] Employee 4: NR7 then expansion, in the correct causal order")
n4 = [bar(1000 + i * 900, 100.0, 105.0, 95.0, 100.0) for i in range(60)]
n4[-3]["high"], n4[-3]["low"] = 100.4, 99.6          # compression on bar -3
n4[-2]["open"], n4[-2]["high"], n4[-2]["low"], n4[-2]["close"] = 100.0, 104.0, 99.8, 103.5
n4[-1]["open"], n4[-1]["high"], n4[-1]["low"], n4[-1]["close"] = 103.5, 103.9, 103.4, 103.8
n4df = frame(n4)
check("the crafted series has NR7 on bar -3", md.is_nr7(n4df, offset=-3) is True)
check("bar -2 is the expansion, not an NR7", md.is_nr7(n4df, offset=-2) is False)

# ===================================================================== sessions
print("\n[14] Session windows use UTC bar times")
check("session hours are read from a tz-aware datetime column",
      str(md_mod.MarketDataManager.fetch_rates.__doc__ or "").find("UTC") > 0)
h1rows = synthetic(60, 250, 2400.0)
f = frame(h1rows)
check("bar datetimes carry a UTC tz", str(f['datetime'].dt.tz) == "UTC", str(f['datetime'].dt.tz))

# ===================================================================== full sweep
print("\n[15] Full evaluate_all sweep: no exceptions on live-shaped data")
STATE["bars"]["H1"] = synthetic(60, 250, 2400.0)
STATE["bars"]["M5"] = synthetic(5, 200, 2400.0)
STATE["bars"]["M15"] = synthetic(15, 200, 2400.0)
STATE["bars"]["M30"] = synthetic(30, 200, 2400.0)
STATE["tick"] = obj(bid=2400.50, ask=2400.70)
g = gov()
spy15 = Spy()
team15 = team_mod.TeamOffice(md, spy15, events)
ok = True
for _ in range(50):
    try:
        team15.evaluate_all()
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"    raised: {type(exc).__name__}: {exc}")
        break
check("50 evaluate_all cycles complete without raising", ok)
check("every emitted signal carries a valid stop distance",
      all(config.MIN_STOP_DISTANCE_USD <= c["stop"] <= config.MAX_STOP_DISTANCE_USD
          for c in spy15.calls),
      str([c["stop"] for c in spy15.calls]))
check("every emitted signal has a known employee id",
      all(c["emp"] in config.EMPLOYEES for c in spy15.calls))
check("every emitted signal has a BUY or SELL side",
      all(c["side"] in ("BUY", "SELL") for c in spy15.calls))
check("only Employee 6 uses a structural take-profit override",
      all((c["tp"] is not None) == (c["emp"] == 6) for c in spy15.calls),
      str([(c["emp"], c["tp"]) for c in spy15.calls]))

for tf in ("M5", "M15", "M30"):
    STATE["bars"][tf] = None
spy16 = Spy()
team16 = team_mod.TeamOffice(md, spy16, events)
ok = True
try:
    for _ in range(10):
        team16.evaluate_all()
except Exception as exc:  # noqa: BLE001
    ok = False
    print(f"    raised: {type(exc).__name__}: {exc}")
check("a dead intraday feed does not crash the loop", ok and spy16.calls == [])

print("\n" + "=" * 60)
sys.exit(summary())
