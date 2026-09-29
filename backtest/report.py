"""Performance reporting for the SLDPAX backtest."""

import csv
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

DAY = 86400.0


# ------------------------------------------------------------------- helpers
def _stats(pnls: list) -> dict:
    n = len(pnls)
    if n == 0:
        return {"n": 0, "wins": 0, "losses": 0, "flat": 0, "win_rate": 0.0,
                "gross_win": 0.0, "gross_loss": 0.0, "profit_factor": 0.0,
                "net": 0.0, "avg": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "payoff": 0.0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    flat = [p for p in pnls if p == 0]
    gw, gl = sum(wins), sum(losses)
    avg_win = gw / len(wins) if wins else 0.0
    avg_loss = gl / len(losses) if losses else 0.0
    return {
        "n": n, "wins": len(wins), "losses": len(losses), "flat": len(flat),
        "win_rate": 100.0 * len(wins) / n,
        "gross_win": gw, "gross_loss": gl,
        "profit_factor": (gw / abs(gl)) if gl else (float("inf") if gw else 0.0),
        "net": sum(pnls), "avg": sum(pnls) / n,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "payoff": (avg_win / abs(avg_loss)) if avg_loss else float("inf"),
    }


def _r_stats(rs: list) -> dict:
    if not rs:
        return {"avg_r": 0.0, "total_r": 0.0, "worst_r": 0.0, "best_r": 0.0, "hit_target_r": 0.0}
    return {
        "avg_r": sum(rs) / len(rs), "total_r": sum(rs),
        "worst_r": min(rs), "best_r": max(rs),
        "hit_target_r": 100.0 * sum(1 for r in rs if r >= 0.95) / len(rs),
    }


def _max_streak(pnls: list, losing: bool) -> int:
    best = run = 0
    for p in pnls:
        if (p < 0) == losing:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def _drawdown(curve: list) -> dict:
    peak = None
    max_dd = 0.0
    max_dd_pct = 0.0
    peak_t = trough_t = 0
    cur_peak_t = 0
    for t, eq, _, _ in curve:
        if peak is None or eq > peak:
            peak, cur_peak_t = eq, t
        dd = peak - eq
        if dd > max_dd:
            max_dd, peak_t, trough_t = dd, cur_peak_t, t
        if peak > 0:
            max_dd_pct = max(max_dd_pct, 100.0 * dd / peak)
    return {
        "max_dd": max_dd, "max_dd_pct": max_dd_pct,
        "peak_time": peak_t, "trough_time": trough_t,
    }


# ------------------------------------------------------------------- analysis
def analyse(result: dict) -> dict:
    sim = result["sim"]
    events = result["events"]
    trips = sim.round_trips
    curve = sim.equity_curve
    args = result["args"]

    pnls = [t["net"] for t in trips]
    rs = [(t["net"] / t["risk_usd"]) if t["risk_usd"] > 0 else 0.0 for t in trips]
    overall = _stats(pnls)
    overall.update(_r_stats(rs))
    overall["max_loss_streak"] = _max_streak(pnls, True)
    overall["max_win_streak"] = _max_streak(pnls, False)
    overall["avg_lot"] = (sum(t["lot"] for t in trips) / len(trips)) if trips else 0.0
    overall["avg_risk_usd"] = (sum(t["risk_usd"] for t in trips) / len(trips)) if trips else 0.0
    overall["avg_bars_held"] = (sum(t["bars_held"] for t in trips) / len(trips)) if trips else 0.0
    overall["largest_win"] = max(pnls) if pnls else 0.0
    overall["largest_loss"] = min(pnls) if pnls else 0.0
    overall["total_swap"] = sum(t["swap"] for t in trips)
    overall["total_commission"] = sum(t["commission"] for t in trips)

    start_bal = args["balance"]
    end_eq = sim.balance
    window_days = max(1.0, (result["end"] - result["start"]) / DAY)
    years = window_days / 365.0
    total_return = 100.0 * (end_eq - start_bal) / start_bal
    cagr = 100.0 * (((end_eq / start_bal) ** (1.0 / years)) - 1.0) if end_eq > 0 and years > 0 else -100.0
    dd = _drawdown(curve)

    per_emp = defaultdict(list)
    per_emp_r = defaultdict(list)
    per_side = defaultdict(list)
    per_reason = Counter()
    per_month = defaultdict(float)
    per_tf_emp = {}
    for t in trips:
        per_emp[t["emp"]].append(t["net"])
        per_emp_r[t["emp"]].append(t["net"] / t["risk_usd"] if t["risk_usd"] > 0 else 0.0)
        per_side[t["side"]].append(t["net"])
        per_reason[t["reason"]] += 1
        per_month[datetime.fromtimestamp(t["exit_time"], timezone.utc).strftime("%Y-%m")] += t["net"]

    employees = {}
    for emp in sorted(set(list(per_emp.keys()) + list(range(1, 9)))):
        row = _stats(per_emp.get(emp, []))
        row.update(_r_stats(per_emp_r.get(emp, [])))
        employees[emp] = row

    traded_days = max(1, len({datetime.fromtimestamp(t["entry_time"], timezone.utc).date()
                             for t in trips}))
    return {
        "meta": {
            "symbol": result["symbol"], "spec": result["spec"],
            "start": datetime.fromtimestamp(result["start"], timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(result["end"], timezone.utc).isoformat(),
            "bars_replayed": result["bars"], "window_days": window_days,
            "elapsed_sec": round(result["elapsed"], 1),
            "balance_start": start_bal, "balance_end": end_eq,
            "base_spread_usd": args["spread"],
            "commission_per_lot": args["commission"],
            "swap_per_lot_day": args["swap"],
            "daily_breaker_trips": result["breaches"],
            "halt_flags": result.get("halt_flags", {}),
            "sim_rejections": dict(sim.rejections),
            "config": result["config_snapshot"],
        },
        "returns": {
            "net_pnl": end_eq - start_bal, "total_return_pct": total_return, "cagr_pct": cagr,
            "max_dd": dd["max_dd"], "max_dd_pct": dd["max_dd_pct"],
            "recovery_factor": ((end_eq - start_bal) / dd["max_dd"]) if dd["max_dd"] > 0 else 0.0,
            "peak_equity": sim.peak_equity,
            "dd_peak": datetime.fromtimestamp(dd["peak_time"], timezone.utc).isoformat()
            if dd["peak_time"] else None,
            "dd_trough": datetime.fromtimestamp(dd["trough_time"], timezone.utc).isoformat()
            if dd["trough_time"] else None,
        },
        "overall": overall,
        "employees": employees,
        "sides": {k: _stats(v) for k, v in per_side.items()},
        "exit_reasons": dict(per_reason),
        "monthly": {k: round(v, 2) for k, v in sorted(per_month.items())},
        "blocks": dict(events.blocks.most_common(20)),
        "event_counts": dict(events.counts),
        "trades_per_month": len(trips) / max(1e-9, window_days / 30.44),
        "trades_per_trading_day": len(trips) / traded_days if trips else 0.0,
    }


# ---------------------------------------------------------------------- write
def write(result: dict, outdir: str, tag: str) -> dict:
    os.makedirs(outdir, exist_ok=True)
    stats = analyse(result)
    sim = result["sim"]

    eq_path = os.path.join(outdir, f"equity_{tag}.csv")
    with open(eq_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["datetime_utc", "equity", "balance", "open_positions"])
        for t, eq, bal, n in sim.equity_curve:
            w.writerow([datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        f"{eq:.2f}", f"{bal:.2f}", n])

    tr_path = os.path.join(outdir, f"trades_{tag}.csv")
    with open(tr_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["emp", "signal", "side", "lot", "entry_time", "entry", "sl", "tp",
                    "exit_time", "exit", "reason", "gross", "swap", "commission", "net",
                    "risk_usd", "r_multiple", "bars_held"])
        for t in sim.round_trips:
            w.writerow([t["emp"], t["signal"], t["side"], f"{t['lot']:.2f}",
                        datetime.fromtimestamp(t["entry_time"], timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        f"{t['entry']:.2f}", f"{t['sl']:.2f}", f"{t['tp']:.2f}",
                        datetime.fromtimestamp(t["exit_time"], timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        f"{t['exit']:.2f}", t["reason"], f"{t['gross']:.2f}",
                        f"{t['swap']:.2f}", f"{t['commission']:.2f}", f"{t['net']:.2f}",
                        f"{t['risk_usd']:.2f}",
                        f"{t['net']/t['risk_usd'] if t['risk_usd'] else 0:.3f}",
                        t["bars_held"]])

    js_path = os.path.join(outdir, f"report_{tag}.json")
    with open(js_path, "w", encoding="utf-8") as fh:
        json.dump(stats, fh, indent=2, default=str)

    return {"equity": eq_path, "trades": tr_path, "report": js_path, "stats": stats}


# -------------------------------------------------------------------- console
def _fmt_pct(x):
    return "n/a" if x in (float("inf"), float("-inf")) else f"{x:.2f}%"


def print_console(result: dict, paths: dict) -> None:
    s = paths["stats"]
    m, r, o = s["meta"], s["returns"], s["overall"]
    line = "=" * 74
    print(f"\n{line}\n SLDPAX BACKTEST - {m['symbol']} - "
          f"{m['start'][:10]} to {m['end'][:10]} ({m['window_days']:.0f} days)\n{line}")

    print(f"\n[ACCOUNT]")
    print(f"  Start balance      ${m['balance_start']:>12,.2f}")
    print(f"  End balance        ${m['balance_end']:>12,.2f}")
    print(f"  Net P&L            ${r['net_pnl']:>12,.2f}")
    print(f"  Total return       {r['total_return_pct']:>11.2f}%")
    print(f"  CAGR               {r['cagr_pct']:>11.2f}%")
    print(f"  Max drawdown       ${r['max_dd']:>12,.2f}  ({r['max_dd_pct']:.2f}%)")
    print(f"  Recovery factor    {r['recovery_factor']:>12.2f}")
    print(f"  Daily breaker trips{m['daily_breaker_trips']:>12}")
    hf = m.get("halt_flags", {})
    if hf:
        labels = {
            "circuit_breaker": "daily breaker", "hard_cutoff": "hard daily cutoff",
            "overall_dd_stop": "overall DD protection", "emergency": "emergency cutoff",
        }
        active = ", ".join(label for key, label in labels.items() if hf.get(key))
        print(f"  Risk latches held  {active or 'none':>12}")

    print(f"\n[TRADES]  {o['n']} closed  "
          f"({s['trades_per_month']:.1f}/month, {s['trades_per_trading_day']:.1f}/active day)")
    if o["n"]:
        print(f"  Win rate           {_fmt_pct(o['win_rate']):>20}   "
              f"{o['wins']}W / {o['losses']}L")
        print(f"  Profit factor      {o['profit_factor']:>20.2f}")
        print(f"  Payoff (avg W/L)   {o['payoff']:>20.2f}")
        print(f"  Expectancy/trade   ${o['avg']:>19,.2f}")
        print(f"  Expectancy         {o['avg_r']:>19.3f}R")
        print(f"  Avg win / loss     ${o['avg_win']:>10,.2f} / ${o['avg_loss']:,.2f}")
        print(f"  Largest W / L      ${o['largest_win']:>10,.2f} / ${o['largest_loss']:,.2f}")
        print(f"  Total R            {o['total_r']:>20.2f}R")
        print(f"  Avg lot / risk     {o['avg_lot']:>20.3f} / ${o['avg_risk_usd']:,.2f}")
        print(f"  Avg bars held      {o['avg_bars_held']:>20.1f}")
        print(f"  Costs (swap+comm)  ${o['total_swap'] + o['total_commission']:>12,.2f}")
        print(f"  Max loss streak    {o['max_loss_streak']:>20}")
    else:
        print("  No trades were executed.")

    print(f"\n[PER EMPLOYEE]")
    print(f"  {'Emp':<5}{'Trades':>7}{'Win%':>8}{'PF':>8}{'Net $':>11}{'ExpR':>8}{'Exp$':>9}")
    for emp, row in s["employees"].items():
        if not row["n"]:
            print(f"  {emp:<5}{0:>7}{'-':>8}{'-':>8}{'0.00':>11}{'-':>8}{'-':>9}")
            continue
        pf = row["profit_factor"]
        print(f"  {emp:<5}{row['n']:>7}{row['win_rate']:>7.1f}%"
              f"{(pf if pf != float('inf') else 999):>8.2f}{row['net']:>11,.2f}"
              f"{row['avg_r']:>8.3f}{row['avg']:>9,.2f}")

    if s["sides"]:
        print(f"\n[BY SIDE]")
        for side, row in s["sides"].items():
            print(f"  {side:<5}{row['n']:>7} trades  win {row['win_rate']:>5.1f}%  "
                  f"net ${row['net']:>10,.2f}  PF {row['profit_factor']:>5.2f}")
    if s["exit_reasons"]:
        print(f"\n[EXITS]  {s['exit_reasons']}")
    if s["monthly"]:
        print(f"\n[MONTHLY NET P&L]")
        months = list(s["monthly"].items())
        for i in range(0, len(months), 4):
            print("  " + "".join(f"{k}: {v:>10,.2f}   " for k, v in months[i:i + 4]))
    if s["blocks"]:
        print(f"\n[TOP BLOCKED SIGNALS]  (total blocked: {sum(s['blocks'].values()):,})")
        for reason, cnt in list(s["blocks"].items())[:12]:
            print(f"  {cnt:>7,}  {reason}")
    if m["sim_rejections"]:
        print(f"\n[SIMULATOR-LEVEL REJECTIONS]  {m['sim_rejections']}")

    print(f"\n{line}")
    print(f" Costs modelled: spread ${m['base_spread_usd']:.2f} base (widened by session "
          f"and volatility), commission ${m['commission_per_lot']:.2f}/lot, "
          f"swap ${m['swap_per_lot_day']:.2f}/lot/day.")
    print(f" Files: {paths['report']}\n         {paths['trades']}\n         {paths['equity']}")
    print(line)
