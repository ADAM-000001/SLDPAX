"""
================================================================================
Team Office Module: 8 Strategy Employees
================================================================================
Signal generation for each employee on its designated timeframe, all sharing a
single H1 EMA200 regime filter and the desk's risk governor.

Strategy corrections
--------------------
* Emp 1  - Asian range window was truncated by a 100-bar fetch; now uses a
           200-bar M5 window and a buffered breakout.
* Emp 3  - Required only a close above the bar's own open after a sweep. Now
           requires a genuine reclaim of the swept level.
* Emp 4  - NR7 compression bar was being detected on the wrong bar (off-by-one)
           and the setup could only fire on a range break that had already
           happened. Now: bar -3 compresses, bar -2 breaks it with a buffer.
* Emp 5  - Session VWAP needs a full 288-bar UTC day; short windows silently
           produced a wrong VWAP early in the session.
* Emp 6  - Was `abs(close - ema20) < 0.40`, a hard-coded 40-cent threshold that
           is true on nearly every M15 bar of XAUUSD. It also never computed an
           FVG despite its name. Replaced with a real Inversion Fair Value Gap:
           a volatility-scaled three-candle imbalance that price has traded
           back through, targeting the gap midpoint.
* Emp 7  - Faded sweeps in strong trends. Now refuses to counter-trend when
           M5 ADX exceeds `max_adx_to_fade`.
* Emp 8  - Computed ADX and then threw it away; it never gated on trend
           strength. Now requires ADX above threshold plus a widening
           directional index spread.
"""

from typing import Optional

import pandas as pd

from config import (
    ATR_MULTIPLIER,
    ATR_PERIOD,
    BREAKOUT_BUFFER_ATR,
    EMPLOYEES,
    MIN_HISTORY_BARS,
    MIN_STOP_DISTANCE_USD,
    VWAP_M5_BARS,
)
from event_log import EventLog
from market_data import MarketDataManager


class TeamOffice:
    def __init__(self, market_data: MarketDataManager, executor, event_log: EventLog):
        self.market_data = market_data
        self.executor = executor
        self.events = event_log
        self.h1_ema200: Optional[float] = None
        self._warned_h1 = False

    # ==================================================================== driver
    def evaluate_all(self) -> None:
        # H1 regime filter, evaluated on the last CLOSED H1 bar for consistency
        # with every intraday strategy (which all use iloc[-2]).
        df_h1 = self.market_data.fetch_rates("H1")
        if df_h1 is None or len(df_h1) < MIN_HISTORY_BARS:
            if not self._warned_h1:
                self._warned_h1 = True
                self.events.error(
                    f"H1 history {0 if df_h1 is None else len(df_h1)}/{MIN_HISTORY_BARS} bars - "
                    f"EMA200 regime filter unavailable, desk is SCANNING ONLY."
                )
            return

        ema = self.market_data.calculate_ema(df_h1['close'], 200)
        value = ema.iloc[-2] if len(ema) >= 2 else ema.iloc[-1]
        if pd.isna(value):
            self.events.error("H1 EMA200 is NaN - desk is SCANNING ONLY.")
            return
        self.h1_ema200 = float(value)

        for timeframe, handler, min_bars in (
            ("M5", self._evaluate_m5_employees, 30),
            ("M15", self._evaluate_m15_employees, 30),
            ("M30", self._evaluate_m30_employees, 30),
        ):
            is_new, df = self.market_data.is_new_bar(timeframe)
            if is_new and df is not None and len(df) >= min_bars:
                handler(df, self.h1_ema200)

    # ------------------------------------------------------------------ utilities
    def _stop_distance(self, df: pd.DataFrame, offset: int = -2) -> Optional[float]:
        atr = self.market_data.calculate_atr(df, ATR_PERIOD)
        value = atr.iloc[offset]
        if pd.isna(value):
            return None
        return max(MIN_STOP_DISTANCE_USD, ATR_MULTIPLIER * float(value))

    def _bar(self, df: pd.DataFrame, offset: int) -> pd.Series:
        return df.iloc[offset]

    # ================================================================= M5 desk
    def _evaluate_m5_employees(self, df: pd.DataFrame, h1_ema200: float) -> None:
        last = self._bar(df, -2)
        prev = self._bar(df, -3)
        dt = last['datetime']
        hour, minute = dt.hour, dt.minute

        c, o = float(last['close']), float(last['open'])
        h, l = float(last['high']), float(last['low'])

        stop = self._stop_distance(df)
        if stop is None:
            return
        buffer = BREAKOUT_BUFFER_ATR * stop / ATR_MULTIPLIER

        ema20_series = self.market_data.calculate_ema(df['close'], 20)
        ema20 = ema20_series.iloc[-2]
        if pd.isna(ema20):
            return
        ema20 = float(ema20)

        # --- Employee 1: London Breakout out of the Asian range (07:00-08:30 UTC)
        if hour == 7 or (hour == 8 and minute <= 30):
            today = dt.date()
            session = df['datetime'].dt.date
            asian = df[(session == today) & (df['datetime'].dt.hour < 7)]
            if len(asian) >= 12:
                asian_high = float(asian['high'].max())
                asian_low = float(asian['low'].min())
                if c > asian_high + buffer and float(prev['close']) <= asian_high and c > h1_ema200:
                    self.executor.execute_order(1, "BUY", stop, "LondonBuy")
                elif c < asian_low - buffer and float(prev['close']) >= asian_low and c < h1_ema200:
                    self.executor.execute_order(1, "SELL", stop, "LondonSell")

        # --- Employee 3: ICT Silver Bullet sweep-and-reclaim (14:00-15:00 UTC)
        if hour == 14:
            swing_low = float(df['low'].iloc[-13:-3].min())
            swing_high = float(df['high'].iloc[-13:-3].max())
            if l < swing_low and c > swing_low and c > o and c > ema20 and c > h1_ema200:
                self.executor.execute_order(3, "BUY", stop, "SweepBuy")
            elif h > swing_high and c < swing_high and c < o and c < ema20 and c < h1_ema200:
                self.executor.execute_order(3, "SELL", stop, "SweepSell")

        # --- Employee 7: NY Impulse Fade (13:30-15:30 UTC), never in strong trend
        in_window = (hour == 13 and minute >= 30) or hour == 14 or (hour == 15 and minute <= 30)
        if in_window:
            adx, _, _ = self.market_data.calculate_adx(df, ATR_PERIOD)
            adx_value = adx.iloc[-2]
            fade_limit = EMPLOYEES[7].max_adx_to_fade
            if not pd.isna(adx_value) and adx_value <= fade_limit:
                high_20 = float(df['high'].iloc[-23:-3].max())
                low_20 = float(df['low'].iloc[-23:-3].min())
                if h > high_20 and c < high_20 and c < o and c < h1_ema200:
                    self.executor.execute_order(7, "SELL", stop, "NYFadeSell")
                elif l < low_20 and c > low_20 and c > o and c > h1_ema200:
                    self.executor.execute_order(7, "BUY", stop, "NYFadeBuy")

    # ================================================================ M15 desk
    def _evaluate_m15_employees(self, df: pd.DataFrame, h1_ema200: float) -> None:
        last = self._bar(df, -2)
        prev = self._bar(df, -3)

        c, o = float(last['close']), float(last['open'])
        h, l = float(last['high']), float(last['low'])

        stop = self._stop_distance(df)
        if stop is None:
            return
        buffer = BREAKOUT_BUFFER_ATR * stop / ATR_MULTIPLIER

        ema20_series = self.market_data.calculate_ema(df['close'], 20)
        ema20 = ema20_series.iloc[-2]
        if pd.isna(ema20):
            return
        ema20 = float(ema20)

        # --- Employee 2: Break & Retest of the 20-bar extreme
        high_20 = float(df['high'].iloc[-24:-4].max())
        low_20 = float(df['low'].iloc[-24:-4].min())
        if float(prev['close']) > high_20 + buffer and l <= high_20 and c > high_20 and c > o and c > h1_ema200:
            self.executor.execute_order(2, "BUY", stop, "RetestBuy")
        elif float(prev['close']) < low_20 - buffer and h >= low_20 and c < low_20 and c < o and c < h1_ema200:
            self.executor.execute_order(2, "SELL", stop, "RetestSell")

        # --- Employee 4: NR7 compression on bar -3, expansion on bar -2
        if self.market_data.is_nr7(df, offset=-3):
            if c > float(prev['high']) + buffer and c > h1_ema200:
                self.executor.execute_order(4, "BUY", stop, "NR7Buy")
            elif c < float(prev['low']) - buffer and c < h1_ema200:
                self.executor.execute_order(4, "SELL", stop, "NR7Sell")

        # --- Employee 6: Inversion Fair Value Gap
        self._evaluate_fvg_inversion(df, c, o, stop, h1_ema200)

        # --- Employee 8: ADX trend continuation
        adx, plus_di, minus_di = self.market_data.calculate_adx(df, ATR_PERIOD)
        adx_v, pdi_v, mdi_v = adx.iloc[-2], plus_di.iloc[-2], minus_di.iloc[-2]
        pdi_prev, mdi_prev = plus_di.iloc[-3], minus_di.iloc[-3]
        threshold = EMPLOYEES[8].adx_threshold
        if any(pd.isna(x) for x in (adx_v, pdi_v, mdi_v, pdi_prev, mdi_prev)):
            return
        if float(adx_v) <= threshold:
            return
        long_spread = float(pdi_v) - float(mdi_v)
        prev_spread = float(pdi_prev) - float(mdi_prev)
        if long_spread > prev_spread:  # directional strength expanding to the upside
            if c > ema20 and c > h1_ema200:
                self.executor.execute_order(8, "BUY", stop, "ADXBuy")
        elif long_spread < prev_spread:  # expanding to the downside
            if c < ema20 and c < h1_ema200:
                self.executor.execute_order(8, "SELL", stop, "ADXSell")

    def _evaluate_fvg_inversion(self, df: pd.DataFrame, c: float, o: float, stop: float, h1_ema200: float) -> None:
        """
        Trade a Fair Value Gap that price has traded back through.

        A bullish (demand) FVG that gets closed back below has been invalidated;
        price usually returns to the gap midpoint. Mirror logic for supply.
        The structural midpoint is passed as an explicit take profit, and the
        executor still rejects it if the resulting R:R is too poor.
        """
        min_gap = BREAKOUT_BUFFER_ATR * stop / ATR_MULTIPLIER
        gaps = self.market_data.find_fvgs(df, lookback=40, end_offset=-2, min_gap=min_gap)
        if not gaps:
            return

        for gap in reversed(gaps):  # newest first
            if gap["type"] == "bullish" and c < gap["bottom"] and c < o and c < h1_ema200:
                self.executor.execute_order(6, "SELL", stop, "IFVGBear", tp_price=gap["mid"])
                return
            if gap["type"] == "bearish" and c > gap["top"] and c > o and c > h1_ema200:
                self.executor.execute_order(6, "BUY", stop, "IFVGBull", tp_price=gap["mid"])
                return

    # ================================================================ M30 desk
    def _evaluate_m30_employees(self, df: pd.DataFrame, h1_ema200: float) -> None:
        last = self._bar(df, -2)
        c, o = float(last['close']), float(last['open'])
        h, l = float(last['high']), float(last['low'])

        stop = self._stop_distance(df)
        if stop is None:
            return

        df_m5 = self.market_data.fetch_rates("M5", count=VWAP_M5_BARS)
        vwap = self.market_data.calculate_session_vwap(df_m5) if df_m5 is not None else c
        if vwap <= 0:
            return

        # --- Employee 5: VWAP rebound in the H1 direction
        if c > vwap and l <= vwap and c > o and c > h1_ema200:
            self.executor.execute_order(5, "BUY", stop, "VWAPBuy")
        elif c < vwap and h >= vwap and c < o and c < h1_ema200:
            self.executor.execute_order(5, "SELL", stop, "VWAPSell")
