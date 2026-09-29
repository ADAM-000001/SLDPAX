"""Side-by-side comparison of backtest scenarios plus fragility checks."""

import csv
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")


def load(tag):
    path = os.path.join(RESULTS, f"report_{tag}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def trades(tag):
    with open(os.path.join(RESULTS, f"trades_{tag}.csv"), encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    stats = {}
    if sys.argv[1:]:
        tags = [t for t in sys.argv[1:] if (stats.setdefault(t, load(t))) is not None]
    else:
        tags = [os.path.splitext(f)[0][len("report_"):]
                for f in os.listdir(RESULTS) if f.startswith("report_") and f.endswith(".json")]
        tags = [t for t in tags if (stats.setdefault(t, load(t))) is not None]
    if not tags:
        print("no report_*.json files in backtest\\results - run runner.py first")
        return 1

    line = "=" * 78
    print(f"\n{line}\n SCENARIO COMPARISON (1 year of XAUUSD, $10,000)\n{line}")
    rows = [
        ("Net P&L", lambda s: f"${s['returns']['net_pnl']:,.0f}"),
        ("Total return", lambda s: f"{s['returns']['total_return_pct']:.2f}%"),
        ("CAGR", lambda s: f"{s['returns']['cagr_pct']:.2f}%"),
        ("Max drawdown", lambda s: f"${s['returns']['max_dd']:,.0f} ({s['returns']['max_dd_pct']:.2f}%)"),
        ("Recovery factor", lambda s: f"{s['returns']['recovery_factor']:.2f}"),
        ("Trades", lambda s: str(s["overall"]["n"])),
        ("Win rate", lambda s: f"{s['overall']['win_rate']:.1f}%"),
        ("Profit factor", lambda s: f"{s['overall']['profit_factor']:.2f}"),
        ("Payoff", lambda s: f"{s['overall']['payoff']:.2f}"),
        ("Expectancy", lambda s: f"${s['overall']['avg']:,.2f}"),
        ("Expectancy (R)", lambda s: f"{s['overall']['avg_r']:.3f}R"),
        ("Total R", lambda s: f"{s['overall']['total_r']:.1f}R"),
        ("Max loss streak", lambda s: str(s["overall"]["max_loss_streak"])),
        ("Daily breaker trips", lambda s: str(s["meta"]["daily_breaker_trips"])),
        ("Simulator rejects", lambda s: str(sum(s["meta"]["sim_rejections"].values()) or 0)),
    ]
    w = max(len(r[0]) for r in rows) + 2
    print(f"{'metric':<{w}}" + "".join(f"{t:>18}" for t in tags))
    for name, fn in rows:
        print(f"{name:<{w}}" + "".join(f"{fn(stats[t]):>18}" for t in tags))

    print(f"\n{line}\n PER-EMPLOYEE  (trades | win% | PF | net $ | expectancy R)\n{line}")
    for emp in range(1, 9):
        line_out = f"  Emp {emp}  "
        for t in tags:
            e = stats[t]["employees"].get(str(emp), stats[t]["employees"].get(emp, {}))
            if not e or not e.get("n"):
                line_out += f"{'no trades':>22}"
            else:
                pf = e["profit_factor"]
                pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
                line_out += f"{e['n']:>5} {e['win_rate']:>5.1f}% {pf_s:>5} {e['net']:>9,.0f} {e['avg_r']:>7.3f}R".rjust(22)
        print(line_out)

    for t in tags:
        tr = trades(t)
        if not tr:
            continue
        nets = sorted((float(r["net"]), r["signal"], r["exit_time"]) for r in tr)
        total = sum(float(r["net"]) for r in tr)
        top5 = sum(n for n, _, _ in nets[-5:])
        bot5 = sum(n for n, _, _ in nets[:5])
        best_month = max(stats[t]["monthly"].items(), key=lambda kv: kv[1]) \
            if stats[t]["monthly"] else ("-", 0)
        worst_month = min(stats[t]["monthly"].items(), key=lambda kv: kv[1]) \
            if stats[t]["monthly"] else ("-", 0)
        by_emp = defaultdict(float)
        for r in tr:
            by_emp[int(r["emp"])] += float(r["net"])
        top_emp = max(by_emp.items(), key=lambda kv: kv[1]) if by_emp else (0, 0)
        losing = [e for e, v in by_emp.items() if v < 0]

        print(f"\n{line}\n FRAGILITY [{t}]\n{line}")
        print(f"  net profit                ${total:>10,.2f}")
        print(f"  best 5 trades             ${top5:>10,.2f}  ({100*top5/total if total else 0:.0f}% of net)")
        print(f"  worst 5 trades            ${bot5:>10,.2f}")
        print(f"  best month                {best_month[0]}  ${best_month[1]:>10,.2f}")
        print(f"  worst month               {worst_month[0]}  ${worst_month[1]:>10,.2f}")
        print(f"  top employee              Emp {top_emp[0]}  ${top_emp[1]:,.2f} "
              f"({100*top_emp[1]/total if total else 0:.0f}% of net)")
        print(f"  losing employees          {losing if losing else 'none'}")
        pos_months = sum(1 for v in stats[t]["monthly"].values() if v > 0)
        print(f"  positive months           {pos_months}/{len(stats[t]['monthly'])}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
