"""Data quality report for the backtest feed. Run before trusting any backtest."""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest import fetch_data  # noqa: E402

from config import SYMBOL  # noqa: E402


def main() -> int:
    df = fetch_data.load(SYMBOL, "M5")
    print(f"M5 bars: {len(df)}  {df['datetime'].iloc[0]} -> {df['datetime'].iloc[-1]}")

    d = df.copy()
    d["date"] = d["datetime"].dt.date
    d["range"] = d["high"] - d["low"]
    d["body"] = (d["close"] - d["open"]).abs()
    d["ret"] = d["close"].pct_change() * 100

    print("\n-- Intrabar range (USD) --")
    print(d["range"].describe(percentiles=[0.5, 0.99, 0.999]).round(3).to_string())

    print("\n-- |return| per 5m bar (%) --")
    print(d["ret"].abs().describe(percentiles=[0.5, 0.99, 0.999, 1.0]).round(4).to_string())

    print("\n-- Spread column (USD) --")
    print(d["spread"].describe(percentiles=[0.5, 0.95, 1.0]).round(3).to_string())

    print("\n-- Gaps between consecutive bars (USD) --")
    gaps = d["open"] - d["close"].shift(1)
    print(gaps.abs().describe(percentiles=[0.5, 0.99, 0.999, 1.0]).round(3).to_string())

    print("\n-- Daily OHLC --")
    daily = d.groupby("date").agg(open=("open", "first"), high=("high", "max"),
                                  low=("low", "min"), close=("close", "last"),
                                  bars=("close", "size"))
    daily["range"] = daily["high"] - daily["low"]
    daily["range_pct"] = daily["range"] / daily["open"] * 100
    print(daily[["open", "high", "low", "close", "bars", "range", "range_pct"]].describe(
        percentiles=[0.5, 0.95, 1.0]).round(2).to_string())
    print(f"\nTrading days: {len(daily)}   mean bars/day: {daily['bars'].mean():.1f}")

    print("\n-- Month-end closes --")
    m = d.set_index("datetime")["close"].resample("ME").last()
    for ts, px in m.items():
        print(f"  {ts.date()}  {px:8.2f}")

    print("\n-- Largest 10 single-bar moves (suspect bad ticks) --")
    top = d.reindex(d["ret"].abs().nlargest(10).index)[
        ["datetime", "open", "high", "low", "close", "range"]].copy()
    top["ret%"] = top["close"].pct_change().abs() * 100
    print(top.round(2).to_string(index=False))

    print("\n-- Weekend / gap bars (> $5 open gap) --")
    big = d[gaps.abs() > 5]
    print(f"count: {len(big)} of {len(d)} ({len(big)/len(d)*100:.3f}%)")
    if len(big):
        print(big[["datetime", "open", "high", "low", "close"]].head(12).round(2).to_string(index=False))

    print("\n-- ATR(14) on M5, in USD and % of price --")
    prev = d["close"].shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - prev).abs(),
                    (d["low"] - prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    print(f"  ATR median {atr.median():.2f} USD   p95 {atr.quantile(0.95):.2f}   max {atr.max():.2f}")
    print(f"  ATR as % of price: median {(atr/d['close']*100).median():.3f}%  "
          f"p95 {(atr/d['close']*100).quantile(0.95):.3f}%")
    print(f"  engine MIN_STOP_DISTANCE_USD=1.20  -> ATR below floor on "
          f"{(atr < 1.20).mean()*100:.1f}% of bars")
    print(f"  engine MAX_STOP_DISTANCE_USD=12.00 -> ATR above ceiling on "
          f"{(atr > 12.0).mean()*100:.1f}% of bars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
