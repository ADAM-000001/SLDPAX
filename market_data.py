"""
================================================================================
Market Data & Indicator Module
================================================================================
Clean OHLCV feeds plus indicator primitives for M5, M15, M30 and H1.

Correctness notes
-----------------
* ATR and ADX use Wilder's RMA (`ewm(alpha=1/period)`), matching MT5's built-in
  indicators. The previous `rolling(period).mean()` (SMA) produced materially
  different values, so thresholds calibrated in MT5 never transferred.
* Bar timestamps come from MT5 as UNIX epoch seconds, i.e. UTC. They are
  materialised with `utc=True` so the naive/UTC distinction is explicit and the
  session-hour gates in team_office cannot silently drift.
* `is_new_bar` consumes each bar exactly once and honours a grace window, so a
  stalled loop no longer drops a signal bar forever.
"""

import time
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import MetaTrader5 as mt5

from config import ATR_PERIOD, BAR_COUNTS, BAR_SIGNAL_GRACE_SEC, VWAP_M5_BARS

TIMEFRAME_MAP = {
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
}


class MarketDataManager:
    def __init__(self, symbol: str = "XAUUSD"):
        self.symbol = symbol
        self._consumed_bar_time: Dict[str, int] = {tf: 0 for tf in TIMEFRAME_MAP}
        self._last_error_logged = 0.0

    # ------------------------------------------------------------------ feeds
    def fetch_rates(self, timeframe_str: str, count: Optional[int] = None) -> Optional[pd.DataFrame]:
        """Fetch bars into a DataFrame with tz-aware UTC timestamps."""
        tf = TIMEFRAME_MAP.get(timeframe_str)
        if tf is None:
            return None

        if count is None:
            count = BAR_COUNTS.get(timeframe_str, 200)

        rates = mt5.copy_rates_from_pos(self.symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            self._log_fetch_failure(timeframe_str)
            return None

        df = pd.DataFrame(rates)
        # MT5 `time` is bar open time as a UNIX epoch -> UTC by definition.
        df['datetime'] = pd.to_datetime(df['time'], unit='s', utc=True)
        return df

    def _log_fetch_failure(self, timeframe_str: str) -> None:
        now = time.time()
        if now - self._last_error_logged > 30.0:
            self._last_error_logged = now
            import logging

            logging.getLogger("SLDPAX_Data").error(
                "No %s bars for %s (MT5 error %s). Check terminal connection.",
                timeframe_str,
                self.symbol,
                mt5.last_error(),
            )

    def is_new_bar(
        self, timeframe_str: str, grace_sec: Optional[float] = BAR_SIGNAL_GRACE_SEC
    ) -> Tuple[bool, Optional[pd.DataFrame]]:
        """
        Return True exactly once per forming bar, indicating a bar just opened.

        `df.iloc[-1]` is the forming bar and `df.iloc[-2]` is the last *closed*
        bar; strategies operate on `iloc[-2]`. If the loop stalls for longer than
        one bar, the next check still delivers the missed bar provided it is
        within `grace_sec` of its open. Without this, any stall silently and
        permanently discarded that bar's signals.

        `grace_sec=0` drops any bar not seen within the same second;
        `grace_sec=None` disables the age check entirely.
        """
        df = self.fetch_rates(timeframe_str)
        if df is None or len(df) < 2:
            return False, None

        forming_open = int(df.iloc[-1]['time'])
        if forming_open <= self._consumed_bar_time[timeframe_str]:
            return False, df

        # Mark as consumed either way so a bar is never evaluated twice.
        self._consumed_bar_time[timeframe_str] = forming_open

        if grace_sec is not None and (time.time() - forming_open) > grace_sec:
            return False, df

        return True, df

    # -------------------------------------------------------------- indicators
    @staticmethod
    def calculate_ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False, min_periods=period).mean()

    @staticmethod
    def wilder_rma(series: pd.Series, period: int) -> pd.Series:
        """Wilder's smoothing (RMA) - the standard used by MT5 for ATR/ADX."""
        return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    @staticmethod
    def true_range(df: pd.DataFrame) -> pd.Series:
        high, low = df['high'], df['low']
        prev_close = df['close'].shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    @staticmethod
    def calculate_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
        tr = MarketDataManager.true_range(df)
        return MarketDataManager.wilder_rma(tr, period)

    @staticmethod
    def calculate_adx(df: pd.DataFrame, period: int = ATR_PERIOD) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """Wilder ADX, +DI and -DI."""
        high, low, close = df['high'], df['low'], df['close']

        up_move = high.diff()
        down_move = -low.diff()

        plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
        minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

        atr = MarketDataManager.wilder_rma(MarketDataManager.true_range(df), period).replace(0.0, np.nan)

        plus_di = 100.0 * MarketDataManager.wilder_rma(plus_dm, period) / atr
        minus_di = 100.0 * MarketDataManager.wilder_rma(minus_dm, period) / atr

        denom = (plus_di + minus_di).replace(0.0, np.nan)
        dx = 100.0 * (plus_di - minus_di).abs() / denom
        adx = MarketDataManager.wilder_rma(dx, period)
        return adx, plus_di, minus_di

    @staticmethod
    def calculate_session_vwap(df: pd.DataFrame) -> float:
        """Session VWAP anchored at 00:00 UTC. Requires a full trading day of bars."""
        if len(df) == 0:
            return 0.0
        last = df.iloc[-1]
        today_date = last['datetime'].date()
        today_df = df[df['datetime'].dt.date == today_date]
        if len(today_df) < VWAP_M5_BARS * 0.5:
            # Not enough history to cover the session - the VWAP would be wrong.
            return float(last['close'])

        volume = today_df['tick_volume']
        if volume.sum() == 0:
            return float(last['close'])

        typical_price = (today_df['high'] + today_df['low'] + today_df['close']) / 3.0
        return float((typical_price * volume).sum() / volume.sum())

    # ------------------------------------------------------------------ patterns
    @staticmethod
    def is_nr7(df: pd.DataFrame, offset: int = -3) -> bool:
        """
        Was the bar at `offset` the narrowest of the 7 closed bars ending there?

        Default `offset=-3` is the causally correct choice: Employee 4 needs the
        compression bar to have *closed* (index -3) and the *next* closed bar
        (-2) to break it. The previous code flagged the compression at -2 and
        then required -2 itself to already have broken -3's range, which is a
        different and far rarer setup than the intended NR7 expansion.
        """
        end = len(df) + offset
        if end < 6:
            return False
        ranges = (df['high'] - df['low']).iloc[end - 6: end + 1]
        if len(ranges) < 7:
            return False
        completed = float(ranges.iloc[-1])
        smallest = float(ranges.min())
        return completed <= smallest * 1.0005

    @staticmethod
    def find_fvgs(df: pd.DataFrame, lookback: int = 40, end_offset: int = -2, min_gap: float = 0.0) -> List[dict]:
        """
        Locate Fair Value Gaps: a 3-candle imbalance where candle 1 and candle 3
        do not overlap, leaving an untraded price void.

        A bullish FVG at index i requires high[i-2] < low[i] (demand void).
        A bearish FVG at index i requires low[i-2] > high[i] (supply void).

        `end_offset=-2` keeps the newest gap at index -3, so the signal bar at -2
        is the *inversion* candle trading back through the gap rather than the
        bar that created it.
        """
        gaps: List[dict] = []
        n = len(df)
        highs = df['high'].to_numpy()
        lows = df['low'].to_numpy()

        start = max(2, n - lookback)
        stop = n + end_offset
        for i in range(start, stop):
            if highs[i - 2] < lows[i] - min_gap:
                gaps.append(
                    {
                        "pos": i,
                        "type": "bullish",
                        "top": float(lows[i]),
                        "bottom": float(highs[i - 2]),
                        "mid": (float(lows[i]) + float(highs[i - 2])) / 2.0,
                    }
                )
            elif lows[i - 2] > highs[i] + min_gap:
                gaps.append(
                    {
                        "pos": i,
                        "type": "bearish",
                        "top": float(lows[i - 2]),
                        "bottom": float(highs[i]),
                        "mid": (float(lows[i - 2]) + float(highs[i])) / 2.0,
                    }
                )
        return gaps
