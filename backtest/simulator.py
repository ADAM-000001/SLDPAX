"""
Replay simulator for the SLDPAX backtester.

Installs a fake `MetaTrader5` module into sys.modules *before* the engine is
imported, so `market_data`, `risk_governor`, `order_executor` and `team_office`
all bind to it and run completely unmodified. Nothing about the production code
path is re-implemented or bypassed.

Lookahead policy
----------------
The master clock is the M5 series. At the moment the engine acts (a bar has just
opened) only that bar's OPEN is knowable, so:
  * the visible forming bar is flattened to O=H=L=C=open;
  * all earlier bars are delivered with their true OHLC;
  * `symbol_info_tick` quotes the forming bar's open +/- modelled spread.

Slippage / fills
---------------
Market orders fill at the exact price the engine submitted (which it derived from
the tick it read), so there is no fill cheat. Stops and targets are absolute
prices set at entry, so the spread is paid once, on entry. If a bar gaps through
a stop the exit fills at the worse of the open and the stop price. If a bar
touches both the stop and the target, the STOP is assumed first - deliberately
pessimistic, since intrabar sequence is unknowable from OHLC.
"""

import bisect
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import numpy as np

# ---------------------------------------------------------------- cost model
# Calibrated to the operator's broker: XAUUSD spread averages $0.30-0.50, so the
# base is the midpoint. Session widening is deliberately modest (1.6x, not the
# 2.5x first assumed) because a $0.40 average does not become $1.00 at the London
# open on a normal day; the rollover is treated as the genuinely bad pocket.
BASE_SPREAD_USD = 0.40
SPREAD_SESSION_MULT = 1.6   # London / NY opens
SPREAD_ROLLOVER_MULT = 2.2  # daily rollover, thin book
SPREAD_ATR_COEF = 0.35      # extra spread per unit of normalised ATR
SPREAD_MAX_USD = 1.50
COMMISSION_PER_LOT = 0.0    # MetaQuotes CFDs are commission-free
SWAP_USD_PER_LOT_DAY = -8.0  # charged on triple rollover (Wed)
LEVERAGE = 100.0

_DT = np.dtype([
    ("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
    ("close", "f8"), ("tick_volume", "i8"), ("spread", "i8"), ("real_volume", "i8"),
])

_EMP_BY_MAGIC: dict = {}


def _emp_from_magic(magic: int) -> int:
    if not _EMP_BY_MAGIC:
        from config import MAGIC_TO_EMP  # config is MT5-free, safe to import here
        _EMP_BY_MAGIC.update(MAGIC_TO_EMP)
    return _EMP_BY_MAGIC.get(magic, 0)


@dataclass
class Position:
    ticket: int
    identifier: int
    magic: int
    type: int
    volume: float
    price_open: float
    sl: float
    tp: float
    time: int
    comment: str = ""

    def profit_at(self, price: float) -> float:
        direction = 1.0 if self.type == 0 else -1.0
        return (price - self.price_open) * direction * 100.0 * self.volume


@dataclass
class Trade:
    ticket: int
    position_id: int
    magic: int
    entry: int          # 0 = IN, 1 = OUT
    profit: float = 0.0
    swap: float = 0.0
    commission: float = 0.0
    time: int = 0
    volume: float = 0.0
    price: float = 0.0
    reason: str = ""
    emp_id: int = 0
    side: str = "?"


class ReplayClock:
    def __init__(self, start: float = 0.0):
        self.t = start

    def set(self, epoch: float) -> None:
        self.t = epoch

    def now(self) -> float:
        return self.t

    def now_dt(self) -> datetime:
        return datetime.fromtimestamp(self.t, timezone.utc)


class ReplayDateTime(datetime):
    """datetime whose .now() follows the replay clock."""
    clock: ReplayClock = None

    @classmethod
    def now(cls, tz=None):
        if cls.clock is None:
            return datetime.now(tz)
        return datetime.fromtimestamp(cls.clock.now(), tz)


@dataclass
class Feed:
    tf: str
    times: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype="i8"))
    bars: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=_DT))
    _cache: dict = field(default_factory=dict)

    def visible(self, now_epoch: float, count: int) -> np.ndarray:
        """Bars visible at `now_epoch`, last one flattened to remove lookahead."""
        end = int(bisect.bisect_right(self.times, now_epoch))
        if end <= 0:
            return np.zeros(0, dtype=_DT)
        start = max(0, end - count)
        key = (start, end)
        cached = self._cache.get(key)
        if cached is None:
            cached = self.bars[start:end].copy()
            if len(cached):
                o = float(cached[-1]["open"])
                cached[-1]["high"] = o
                cached[-1]["low"] = o
                cached[-1]["close"] = o
            self._cache = {key: cached}   # single-slot: clock only moves forward
        return cached

    def index_at(self, epoch: float) -> int:
        return int(bisect.bisect_right(self.times, epoch)) - 1


class Simulator:
    """Holds replay state and implements the MT5 surface the engine consumes."""

    def __init__(self, symbol: str, symbol_info, feeds: dict, start_epoch: float,
                 initial_balance: float, base_spread: float = BASE_SPREAD_USD,
                 commission: float = COMMISSION_PER_LOT, swap: float = SWAP_USD_PER_LOT_DAY):
        self.symbol = symbol
        self._symbol_info_src = symbol_info
        self.feeds = feeds
        self.clock = ReplayClock(start_epoch)
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.base_spread = base_spread
        self.commission_per_lot = commission
        self.swap_per_lot_day = swap

        self.positions: List[Position] = []
        self.deals: List[Trade] = []
        self.closed: List[Trade] = []
        self.round_trips: List[dict] = []
        self.equity_curve: List[tuple] = []
        self.rejections: dict = {}
        self.peak_equity = initial_balance
        self.max_dd = 0.0

        self._next_ticket = 1_000_000
        self._current_bar = None
        self._atr_cache: tuple = (0.0, 0.0)
        self._rejected_lots = 0.0

    # -- helpers ---------------------------------------------------------
    def _tick_id(self) -> int:
        self._next_ticket += 1
        return self._next_ticket

    def current_price(self) -> float:
        return float(self._current_bar["open"]) if self._current_bar is not None else 0.0

    def atr_now(self) -> float:
        return self._atr_cache[1]

    def _refresh_atr(self) -> None:
        m5 = self.feeds["M5"]
        end = m5.index_at(self.clock.now()) + 1
        if end < 20:
            return
        window = m5.bars[max(0, end - 20):end]
        prev_close = window["close"][:-1]
        tr = np.maximum.reduce([
            window["high"][1:] - window["low"][1:],
            np.abs(window["high"][1:] - prev_close),
            np.abs(window["low"][1:] - prev_close),
        ])
        if len(tr):
            self._atr_cache = (float(window["time"][-1]), float(tr[-14:].mean()))

    def spread(self) -> float:
        now = self.clock.now_dt()
        h = now.hour + now.minute / 60.0
        mult = 1.0
        if 7.0 <= h < 8.5 or 13.5 <= h < 16.0:
            mult = SPREAD_SESSION_MULT
        elif h >= 22.0 or h < 1.0:
            mult = SPREAD_ROLLOVER_MULT
        atr = self.atr_now()
        vol_mult = 1.0 + SPREAD_ATR_COEF * min(1.0, max(0.0, atr / 6.0 - 1.0))
        return min(SPREAD_MAX_USD, self.base_spread * mult * vol_mult)

    def _swap_for(self, position: Position) -> float:
        """Charged once per day, tripled on Wednesday."""
        opened = datetime.fromtimestamp(position.time, timezone.utc)
        now = self.clock.now_dt()
        if opened.date() == now.date():
            return 0.0
        days = (now.date() - opened.date()).days
        if days <= 0:
            return 0.0
        weight = 0
        for d in range(1, days + 1):
            day = opened.date() + timedelta(days=d)
            weight += 3 if day.weekday() == 2 else 1
        return self.swap_per_lot_day * position.volume * weight

    def equity(self) -> float:
        price = self.current_price()
        return self.balance + sum(p.profit_at(price) for p in self.positions)

    def used_margin(self) -> float:
        info = self._symbol_info_src
        contract = info.trade_contract_size or 100.0
        return sum(p.volume * (self.current_price() * contract) / LEVERAGE for p in self.positions)

    # -- clock -----------------------------------------------------------
    def advance_to_bar(self, bar) -> None:
        """Called when an M5 bar has just opened."""
        self.clock.set(float(bar["time"]))
        self._current_bar = bar
        self._refresh_atr()

    # -- exits -----------------------------------------------------------
    def process_exits(self, bar) -> List[Trade]:
        """Resolve stop/targets against a fully closed bar."""
        if not self.positions:
            return []
        o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
        when = int(bar["time"])
        closed_now = []
        for pos in list(self.positions):
            exit_price = None
            reason = ""
            if pos.type == 0:  # BUY
                sl_hit = l <= pos.sl
                tp_hit = h >= pos.tp
                if sl_hit:
                    exit_price = min(o, pos.sl)
                    reason = "SL"
                elif tp_hit:
                    exit_price = max(o, pos.tp)
                    reason = "TP"
            else:             # SELL
                sl_hit = h >= pos.sl
                tp_hit = l <= pos.tp
                if sl_hit:
                    exit_price = max(o, pos.sl)
                    reason = "SL"
                elif tp_hit:
                    exit_price = min(o, pos.tp)
                    reason = "TP"
            if exit_price is None:
                continue
            closed_now.append(self._close(pos, exit_price, when, reason))
        return closed_now

    def force_close_all(self, bar) -> List[Trade]:
        """Mark-to-market anything still open at the end of the window."""
        if not self.positions:
            return []
        when = int(bar["time"])
        price = float(bar["close"])
        out = []
        for pos in list(self.positions):
            out.append(self._close(pos, price, when, "EOD"))
        return out

    def _close(self, pos: Position, exit_price: float, when: int, reason: str) -> Trade:
            direction = 1.0 if pos.type == 0 else -1.0
            gross = (exit_price - pos.price_open) * direction * 100.0 * pos.volume
            commission = self.commission_per_lot * pos.volume
            swap = self._swap_for(pos)
            net = gross + swap - commission
            deal = Trade(ticket=self._tick_id(), position_id=pos.identifier, magic=pos.magic,
                         entry=1, profit=gross, swap=swap, commission=commission, time=when,
                         volume=pos.volume, price=exit_price, reason=reason)
            self.balance += gross + swap - commission
            self.deals.append(deal)
            self.closed.append(deal)
            self.round_trips.append({
                "position_id": pos.identifier,
                "magic": pos.magic,
                "emp": _emp_from_magic(pos.magic),
                "signal": pos.comment,
                "side": "BUY" if pos.type == 0 else "SELL",
                "lot": pos.volume,
                "entry_time": pos.time,
                "entry": pos.price_open,
                "exit_time": when,
                "exit": exit_price,
                "sl": pos.sl,
                "tp": pos.tp,
                "reason": reason,
                "gross": gross,
                "swap": swap,
                "commission": commission,
                "net": net,
                "risk_usd": pos.volume * 100.0 * abs(pos.price_open - pos.sl),
                "bars_held": max(1, (when - pos.time) // 300),
            })
            self.positions.remove(pos)
            return deal

    def mark(self) -> None:
        eq = self.equity()
        self.peak_equity = max(self.peak_equity, eq)
        self.max_dd = max(self.max_dd, self.peak_equity - eq)
        self.equity_curve.append((self.clock.now(), eq, self.balance, len(self.positions)))

    # -- MT5 surface -----------------------------------------------------
    def copy_rates_from_pos(self, symbol, tf, pos, count):
        name = {5: "M5", 15: "M15", 30: "M30", 16385: "H1"}.get(tf)
        feed = self.feeds.get(name)
        if feed is None:
            return None
        out = feed.visible(self.clock.now(), int(count))
        return out if len(out) else None

    def symbol_info(self, sym):
        src = self._symbol_info_src
        return types.SimpleNamespace(
            name=self.symbol, digits=src.digits, point=src.point,
            trade_contract_size=src.trade_contract_size,
            volume_min=src.volume_min, volume_max=src.volume_max, volume_step=src.volume_step,
            trade_stops_level=src.trade_stops_level, filling_mode=src.filling_mode,
            trade_mode=src.trade_mode, visible=True, spread=int(round(self.spread() / src.point)),
        )

    def symbol_select(self, sym, visible=True):
        return True

    def symbol_info_tick(self, sym):
        price = self.current_price()
        if price <= 0:
            return None
        half = self.spread() / 2.0
        return types.SimpleNamespace(time=int(self.clock.now()), bid=price - half,
                                     ask=price + half, last=0.0, volume=1.0)

    def account_info(self):
        eq = self.equity()
        used = self.used_margin()
        return types.SimpleNamespace(
            login=999999, company="BACKTEST", currency="USD", balance=self.balance,
            equity=eq, margin=used, margin_free=eq - used, margin_level=(eq / used * 100.0) if used else 0.0,
            margin_mode=2, trade_allowed=1, trade_expert=1, limit_orders=0,
        )

    def terminal_info(self):
        return types.SimpleNamespace(connected=True, build=6231, path="backtest")

    def positions_get(self, symbol=None):
        if not self.positions:
            return tuple()
        price = self.current_price()
        out = []
        for p in self.positions:
            out.append(types.SimpleNamespace(
                ticket=p.ticket, identifier=p.identifier, magic=p.magic, type=p.type,
                volume=p.volume, price_open=p.price_open, sl=p.sl, tp=p.tp,
                profit=p.profit_at(price), time=p.time, comment=p.comment,
                symbol=self.symbol, swap=0.0,
            ))
        return tuple(out)

    def history_deals_get(self, frm, to):
        f = frm.timestamp() if hasattr(frm, "timestamp") else float(frm)
        t = to.timestamp() if hasattr(to, "timestamp") else float(to)
        return tuple(d for d in self.deals if f <= d.time <= t)

    def order_check(self, request):
        needed = request["volume"] * request["price"] * 100.0 / LEVERAGE
        free = self.equity() - self.used_margin()
        if needed > free:
            return types.SimpleNamespace(retcode=10019, margin=needed, margin_free=free,
                                         comment="Not enough money")
        return types.SimpleNamespace(retcode=10009, margin=needed, margin_free=free, comment="OK")

    def order_send(self, request):
        if request.get("position"):
            return self._close_send(request)
        rc = self._validate(request)
        if rc != 10009:
            self.rejections[rc] = self.rejections.get(rc, 0) + 1
            return types.SimpleNamespace(retcode=rc, order=0, position_id=0,
                                         comment="rejected by simulator", request=request)
        ticket = self._tick_id()
        pos = Position(ticket=ticket, identifier=ticket, magic=request["magic"],
                       type=request["type"], volume=request["volume"],
                       price_open=request["price"], sl=request["sl"], tp=request["tp"],
                       time=int(self.clock.now()), comment=request.get("comment", ""))
        self.positions.append(pos)
        commission = self.commission_per_lot * pos.volume
        if commission:
            self.balance -= commission
        self.deals.append(Trade(ticket=self._tick_id(), position_id=ticket, magic=pos.magic,
                                entry=0, profit=0.0, commission=commission,
                                time=int(self.clock.now()), volume=pos.volume,
                                price=pos.price_open, side="BUY" if pos.type == 0 else "SELL"))
        return types.SimpleNamespace(retcode=10009, order=ticket, position_id=ticket, comment="done")

    def _close_send(self, request):
        """Close-by-position (emergency cutoff): fills at the request price."""
        pid = request.get("position")
        pos = next((p for p in self.positions if p.identifier == pid), None)
        if pos is None:
            self.rejections[10013] = self.rejections.get(10013, 0) + 1
            return types.SimpleNamespace(retcode=10013, order=0, position_id=pid,
                                         comment="closing an unknown position", request=request)
        price = request["price"]
        if abs(price - self.current_price()) > 5.0:
            return types.SimpleNamespace(retcode=10004, order=0, position_id=pid,
                                         comment="requote", request=request)
        trade = self._close(pos, price, int(self.clock.now()),
                            str(request.get("comment") or "EMERG"))
        return types.SimpleNamespace(retcode=10009, order=trade.ticket, position_id=pid,
                                     comment="done")

    def _validate(self, request) -> int:
        if request.get("symbol") != self.symbol:
            return 10013
        info = self.symbol_info(self.symbol)
        price = request["price"]
        if abs(price - self.current_price()) > 5.0:
            return 10004          # REQUOTE
        if request["sl"] <= 0 or request["tp"] <= 0:
            return 10015          # INVALID_STOPS
        if request["type"] == 0 and not (request["sl"] < price < request["tp"]):
            return 10015
        if request["type"] == 1 and not (request["tp"] < price < request["sl"]):
            return 10015
        vol = request["volume"]
        if vol < info.volume_min or vol > info.volume_max:
            return 10014          # INVALID_VOLUME
        return 10009

    def last_error(self):
        return (0, "no error")

    def initialize(self, *a, **k):
        return True

    def shutdown(self, *a, **k):
        return True


def install(sim: Simulator) -> types.ModuleType:
    """Publish the simulator as the `MetaTrader5` module."""
    module = types.ModuleType("MetaTrader5")
    for name in ("TIMEFRAME_M5", "TIMEFRAME_M15", "TIMEFRAME_M30", "TIMEFRAME_H1"):
        setattr(module, name, {"TIMEFRAME_M5": 5, "TIMEFRAME_M15": 15,
                               "TIMEFRAME_M30": 30, "TIMEFRAME_H1": 16385}[name])
    module.TIMEFRAME_M1 = 1
    module.TIMEFRAME_H2, module.TIMEFRAME_H4 = 16386, 16388
    module.TIMEFRAME_D1, module.TIMEFRAME_W1, module.TIMEFRAME_MN1 = 16408, 32769, 49153
    module.ORDER_TYPE_BUY, module.ORDER_TYPE_SELL = 0, 1
    module.ORDER_TYPE_BUY_LIMIT, module.ORDER_TYPE_SELL_LIMIT = 2, 3
    module.ORDER_TYPE_BUY_STOP, module.ORDER_TYPE_SELL_STOP = 4, 5
    module.TRADE_ACTION_DEAL, module.TRADE_ACTION_PENDING = 1, 5
    module.ORDER_TIME_GTC, module.ORDER_TIME_DAY = 0, 1
    module.ORDER_FILLING_FOK, module.ORDER_FILLING_IOC, module.ORDER_FILLING_RETURN = 0, 1, 2
    module.SYMBOL_TRADE_MODE_FULL, module.SYMBOL_TRADE_MODE_CLOSEONLY = 4, 2
    module.ACCOUNT_MARGIN_MODE_RETAIL_NETTING, module.ACCOUNT_MARGIN_MODE_EXCHANGE, module.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 0, 1, 2
    module.DEAL_ENTRY_IN, module.DEAL_ENTRY_OUT, module.DEAL_ENTRY_INOUT = 0, 1, 2
    module.TRADE_RETCODE_DONE, module.TRADE_RETCODE_DONE_PARTIAL = 10009, 10010
    module.TRADE_RETCODE_REQUOTE, module.TRADE_RETCODE_PRICE_CHANGED = 10004, 10006
    module.TRADE_RETCODE_PRICE_OFF, module.TRADE_RETCODE_INVALID_STOPS = 10007, 10015
    module.TRADE_RETCODE_INVALID_VOLUME, module.TRADE_RETCODE_NO_MONEY = 10014, 10019
    module.TRADE_RETCODE_INVALID, module.TRADE_RETCODE_REJECT = 10013, 10006
    module.TRADE_RETCODE_MARKET_CLOSED, module.TRADE_RETCODE_PLACED = 10018, 10008
    module.TRADE_RETCODE_INVALID_REQUEST = 10013

    for fn in ("copy_rates_from_pos", "copy_rates_range", "symbol_info", "symbol_select",
               "symbol_info_tick", "account_info", "terminal_info", "positions_get",
               "history_deals_get", "order_check", "order_send", "last_error",
               "initialize", "shutdown", "symbols_get", "time_current"):
        setattr(module, fn, getattr(sim, fn, None))
    module.copy_rates_range = lambda *a, **k: sim.copy_rates_from_pos(a[1], a[2], 0, 100000)
    module.symbols_get = lambda *a, **k: (sim.symbol,)
    module.time_current = lambda: int(sim.clock.now())

    import sys as _sys

    _sys.modules["MetaTrader5"] = module
    return module
