"""Why are 3 of 8 strategies barely trading? Diagnose the ATR stop band and Emp 6 geometry."""

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import config  # noqa: E402
from backtest import fetch_data  # noqa: E402

MULT = config.ATR_MULTIPLIER
MIN_S, MAX_S = config.MIN_STOP_DISTANCE_USD, config.MAX_STOP_DISTANCE_USD


def atr_of(df: pd.DataFrame, period: int = config.ATR_PERIOD) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def main() -> int:
    print(f"Engine ATR stop = ATR({config.ATR_PERIOD}) x {MULT}, "
          f"accepted only within [${MIN_S}, ${MAX_S}]\n")
    print(f"{'TF':<5}{'ATR p10':>9}{'p50':>8}{'p90':>8}{'p99':>8}"
          f"{'| stop p50':>12}{'p90':>8}{'| below MIN':>12}{'above MAX':>12}")
    for tf in ("M5", "M15", "M30", "H1"):
        df = fetch_data.load(config.SYMBOL, tf)
        a = atr_of(df).dropna()
        stop = MULT * a
        print(f"{tf:<5}{a.quantile(.10):>9.2f}{a.median():>8.2f}{a.quantile(.90):>8.2f}"
              f"{a.quantile(.99):>8.2f}{stop.median():>12.2f}{stop.quantile(.90):>8.2f}"
              f"{100*(stop < MIN_S).mean():>11.1f}%{100*(stop > MAX_S).mean():>11.1f}%")

    print(f"\nThe M30 desk (Emp 5) computes its stop on the M30 frame, where the median "
          f"stop is ${MULT*atr_of(fetch_data.load(config.SYMBOL,'M30')).median():.2f}.")
    print(f"With MAX_STOP_DISTANCE_USD = ${MAX_S}, "
          f"{100*(MULT*atr_of(fetch_data.load(config.SYMBOL,'M30')).dropna() > MAX_S).mean():.0f}% "
          f"of all M30 signals are discarded before sizing.")

    # ---- Emp 6 geometry: how often is the FVG midpoint on the wrong side of entry?
    print("\n" + "=" * 74)
    print(" Emp 6 (FVG inversion) take-profit geometry")
    print("=" * 74)
    import market_data as md

    raw = fetch_data.load(config.SYMBOL, "M15")
    df = raw.copy()
    df = df.iloc[:-1].reset_index(drop=True)   # treat last row as closed
    calc_atr = md.MarketDataManager.calculate_atr
    find_fvgs = md.MarketDataManager.find_fvgs

    inverted = total = 0
    half_spread = fetch_data.load(config.SYMBOL, "M5").pipe(lambda _: 0.20)  # 0.40/2
    for pos in range(2, len(df) - 2):
        bars = df.iloc[:pos + 1]
        atr = calc_atr(bars, config.ATR_PERIOD)
        if pd.isna(atr.iloc[pos]):
            continue
        stop = max(MIN_S, MULT * float(atr.iloc[pos]))
        min_gap = config.BREAKOUT_BUFFER_ATR * stop / MULT
        gaps = find_fvgs(bars, lookback=40, end_offset=0, min_gap=min_gap)
        bar = bars.iloc[-1]
        c, o = float(bar["close"]), float(bar["open"])
        # The executor fills at the next bar's open, not at the signal close.
        fill = float(bars.iloc[-1]["open"]) * 0.0 + float(df.iloc[pos + 1]["open"])
        for gap in reversed(gaps):
            if gap["type"] == "bearish" and c > gap["top"] and c > o:
                total += 1
                if gap["mid"] < fill - half_spread:   # BUY target below the fill
                    inverted += 1
                break
            if gap["type"] == "bullish" and c < gap["bottom"] and c < o:
                total += 1
                if gap["mid"] > fill + half_spread:   # SELL target above the fill
                    inverted += 1
                break

    print(f"  M15 IFVG signals                : {total}")
    print(f"  of those, take-profit on the WRONG side of the fill price : {inverted} "
          f"({100*inverted/max(1,total):.1f}%)")
    print("\n  A BUY whose TP sits below the fill, or a SELL whose TP sits above it,")
    print("  still passes the executor's abs(reward)/abs(risk) R:R test, is sent to")
    print("  the broker, and comes back as INVALID_STOPS. The signal is thrown away and")
    print("  the desk paid the spread. Those are the 1,667 (base) / 2,106 (tight)")
    print("  simulator rejections recorded in the backtest runs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
