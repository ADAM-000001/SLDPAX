"""
================================================================================
Order Executor Module
================================================================================
Interfaces with MetaTrader 5 to place institutional market orders on behalf of
the desk, with automated filling-mode selection, derived per-employee magic
numbers and RR-derived SL/TP.

Every order now passes a pre-flight gauntlet before it can reach the broker:
risk clearance -> symbol tradability -> live tick -> spread ceiling -> stop
distance vs broker stop level -> lot sizing vs hard risk ceiling -> effective
R:R floor -> margin/free-margin check. Anything that fails is refused and
logged, rather than being sent to be rejected asynchronously.
"""

import logging
from typing import Optional

import MetaTrader5 as mt5

from config import (
    DISABLED_EMPLOYEES,
    EMPLOYEES,
    MAX_DEVIATION_POINTS,
    MAX_SPREAD_USD,
    MIN_ACCEPTABLE_RR,
    MIN_STOP_DISTANCE_USD,
    SYMBOL,
    magic_for,
    max_stop_distance_usd,
)
from event_log import EventLog
from risk_governor import RiskGovernor

logger = logging.getLogger("SLDPAX_Executor")


class OrderExecutor:
    def __init__(self, risk_governor: RiskGovernor, event_log: EventLog):
        self.risk_governor = risk_governor
        self.events = event_log

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def get_filling_mode(symbol_info) -> int:
        """Pick a filling mode the broker symbol actually supports."""
        mode = symbol_info.filling_mode
        if mode & 1:   # FOK
            return mt5.ORDER_FILLING_FOK
        if mode & 2:   # IOC
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def _resolve_symbol(self):
        sym_info = mt5.symbol_info(SYMBOL)
        if sym_info is None or not sym_info.visible:
            mt5.symbol_select(SYMBOL, True)
            sym_info = mt5.symbol_info(SYMBOL)
        return sym_info

    # ---------------------------------------------------------------- execution
    def execute_order(
        self,
        emp_id: int,
        order_type_str: str,
        sl_distance_usd: float,
        comment_prefix: str = "",
        tp_price: Optional[float] = None,
    ) -> Optional[dict]:
        """
        Place one market order for `emp_id`.

        `sl_distance_usd` is the ATR-derived stop distance in gold price terms.
        `tp_price` optionally overrides the RR-derived take profit so structural
        strategies (e.g. FVG inversion) can target a real level; it is still
        gated by `MIN_ACCEPTABLE_RR`.
        """
        emp_profile = EMPLOYEES.get(emp_id)
        if emp_profile is None:
            logger.error("Unknown employee id %s", emp_id)
            return None

        if emp_id in DISABLED_EMPLOYEES:
            self.events.block(f"Emp {emp_id}: employee is disabled in config.DISABLED_EMPLOYEES")
            return None

        is_buy = order_type_str.upper() == "BUY"
        side = "BUY" if is_buy else "SELL"

        # 1. Risk clearance (direction-aware, so the correlation cap only
        #    rejects trades that would increase the already-full side)
        allowed, reason = self.risk_governor.is_trade_allowed(emp_id, side)
        if not allowed:
            logger.info("[BLOCKED] Emp %s: %s", emp_id, reason)
            return None

        # 2. Symbol tradability
        sym_info = self._resolve_symbol()
        if sym_info is None:
            self.events.error(f"Emp {emp_id}: cannot resolve symbol {SYMBOL}")
            return None
        if sym_info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
            self.events.block(f"Emp {emp_id}: {SYMBOL} is not fully tradable (mode {sym_info.trade_mode})")
            return None

        # 3. Live tick
        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None or tick.ask <= 0 or tick.bid <= 0:
            self.events.error(f"Emp {emp_id}: no valid tick for {SYMBOL}")
            return None

        # 4. Spread ceiling - a blown spread is an instant bad fill
        spread = tick.ask - tick.bid
        if spread > MAX_SPREAD_USD:
            self.events.block(f"Emp {emp_id}: spread ${spread:.2f} exceeds ${MAX_SPREAD_USD:.2f} ceiling")
            return None

        # 5. Stop distance sanity - the ceiling is per timeframe, because the stop
        #    is 1.2 x ATR and ATR scales with the bar size. A single absolute cap
        #    silently disabled most M15/M30 signals.
        max_stop = max_stop_distance_usd(emp_profile.timeframe)
        if not (MIN_STOP_DISTANCE_USD <= sl_distance_usd <= max_stop):
            self.events.block(
                f"Emp {emp_id}: ATR stop ${sl_distance_usd:.2f} outside "
                f"[${MIN_STOP_DISTANCE_USD:.2f}, ${max_stop:.2f}] on "
                f"{emp_profile.timeframe} - volatility regime rejected"
            )
            return None

        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        entry_price = tick.ask if is_buy else tick.bid
        digits = sym_info.digits
        point = sym_info.point or 0.01

        # 6. Broker minimum stop distance
        min_stop = (sym_info.trade_stops_level or 0) * point
        if sl_distance_usd < min_stop:
            self.events.block(
                f"Emp {emp_id}: stop ${sl_distance_usd:.2f} below broker minimum ${min_stop:.2f}"
            )
            return None

        # 7. SL / TP
        sl = round(entry_price - sl_distance_usd, digits) if is_buy else round(entry_price + sl_distance_usd, digits)
        if tp_price is not None:
            tp = round(tp_price, digits)
        else:
            rr = emp_profile.rr_ratio
            tp = round(entry_price + (sl_distance_usd * rr), digits) if is_buy \
                else round(entry_price - (sl_distance_usd * rr), digits)

        reward = abs(tp - entry_price)
        risk = abs(entry_price - sl)
        if risk <= 0 or reward <= 0:
            self.events.block(f"Emp {emp_id}: degenerate SL/TP geometry")
            return None
        # The R:R floor below uses abs(), so it cannot see a take profit that sits
        # on the WRONG side of entry. A structural target (e.g. Employee 6's FVG
        # midpoint) is routinely already behind price by the time the next bar
        # opens, and such an order is guaranteed to be rejected by the broker for
        # invalid stops - after the desk has already paid the spread and burned a
        # daily-trade slot. Reject the geometry here instead.
        tp_on_correct_side = (tp > entry_price) if is_buy else (tp < entry_price)
        sl_on_correct_side = (sl < entry_price) if is_buy else (sl > entry_price)
        if not (tp_on_correct_side and sl_on_correct_side):
            self.events.block(
                f"Emp {emp_id}: inverted bracket for a {side} at {entry_price:.2f} "
                f"(SL {sl:.2f}, TP {tp:.2f}) - target already behind price"
            )
            return None
        effective_rr = reward / risk
        if effective_rr < MIN_ACCEPTABLE_RR:
            self.events.block(
                f"Emp {emp_id}: effective RR 1:{effective_rr:.2f} below 1:{MIN_ACCEPTABLE_RR:.2f} floor"
            )
            return None

        # 8. Position sizing against the hard risk ceiling
        lot, actual_risk = self.risk_governor.calculate_lot_size(sl_distance_usd)
        if lot is None:
            return None

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": SYMBOL,
            "volume": lot,
            "type": order_type,
            "price": entry_price,
            "sl": sl,
            "tp": tp,
            "deviation": MAX_DEVIATION_POINTS,
            "magic": magic_for(emp_id),
            "comment": f"Emp{emp_id}_{comment_prefix[:10]}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.get_filling_mode(sym_info),
        }

        # 9. Margin pre-flight
        check = mt5.order_check(request)
        if check is None:
            self.events.error(f"Emp {emp_id}: order_check returned None (MT5 error {mt5.last_error()})")
            return None
        if check.retcode != mt5.TRADE_RETCODE_DONE:
            self.events.block(f"Emp {emp_id}: margin check refused - {check.comment}")
            return None

        # 10. Send
        result = mt5.order_send(request)
        if result is None:
            self.events.error(f"Emp {emp_id}: order_send returned None (MT5 error {mt5.last_error()})")
            return None

        if result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL):
            self._log_rejection(emp_id, result)
            return None

        side = "BUY" if is_buy else "SELL"
        self.risk_governor.record_trade_placed(emp_id, result.position_id, side, result.order)

        msg = (
            f"Emp {emp_id} {emp_profile.name}: {side} {lot:.2f} @ {entry_price:.2f} | "
            f"SL {sl:.2f} | TP {tp:.2f} | RR 1:{effective_rr:.1f} | risk ${actual_risk:.2f} "
            f"| spread ${spread:.2f} | ticket {result.order}"
        )
        self.events.trade(msg)
        logger.info("[EXECUTED] %s", msg)

        return {
            "ticket": result.order,
            "position_id": result.position_id,
            "emp_id": emp_id,
            "type": side,
            "lot": lot,
            "price": entry_price,
            "sl": sl,
            "tp": tp,
            "rr": effective_rr,
            "risk_usd": actual_risk,
        }

    def _log_rejection(self, emp_id: int, result) -> None:
        retcode = result.retcode
        if retcode in (mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_CHANGED, mt5.TRADE_RETCODE_PRICE_OFF):
            level, prefix = self.events.block, "[REQUOTE]"
        elif retcode in (mt5.TRADE_RETCODE_INVALID_STOPS, mt5.TRADE_RETCODE_INVALID_VOLUME):
            level, prefix = self.events.error, "[INVALID]"
        else:
            level, prefix = self.events.error, "[REJECTED]"
        level(f"Emp {emp_id}: order {prefix} code {retcode} - {result.comment}")
        logger.error("[FAILED] Emp %s retcode=%s comment=%s", emp_id, retcode, result.comment)
