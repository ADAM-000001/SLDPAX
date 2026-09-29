"""
================================================================================
Risk Governor Module (Chief Risk Officer Engine)
================================================================================
Enforces prop-firm compliance, daily drawdown circuit breakers, position caps,
per-employee loss quotas and dynamic position sizing.

Bug fixes applied
-----------------
1. Circuit breaker survives restarts. Day-start equity and all counters are
   persisted via RiskStateStore. Previously a restart re-anchored
   `day_start_equity` to *current* equity, silently erasing the day's losses.
2. Deal reconciliation is incremental. `reconcile_closed_deals` used to re-scan
   the whole session every loop tick (~1x/sec) and blindly `+= 1` per deal, so a
   single loss was counted thousands of times per hour. Processed deal tickets
   are now tracked and a position is scored once, on full closure, using
   profit + swap + commission.
3. Hedging account is verified at startup. On a netting account MT5 merges
   same-symbol positions and keeps only the first magic, which destroys both
   the position cap and PnL attribution.
4. New guards: per-employee position cap, same-direction correlation cap, daily
   trade ceiling, and a hard single-trade risk ceiling that min-lot cannot
   breach.
"""

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import MetaTrader5 as mt5

from config import (
    ACCOUNT_RISK_PERCENT,
    DD_RISK_TIERS,
    EMERGENCY_DD_PCT,
    EMPLOYEES,
    HARD_DAILY_LOSS_PCT,
    MAGIC_TO_EMP,
    MAX_DAILY_LOSSES_PER_EMP,
    MAX_DAILY_LOSS_PCT,
    MAX_DAILY_TRADES_PER_EMP,
    MAX_DEVIATION_POINTS,
    MAX_OPEN_POSITIONS,
    MAX_POSITIONS_PER_EMP,
    MAX_SAME_DIRECTION_POSITIONS,
    MAX_SINGLE_TRADE_RISK_PCT,
    MIN_ENTRY_INTERVAL_SEC,
    OVERALL_DD_STOP_PCT,
    RECONCILE_INTERVAL_SEC,
    REQUIRE_HEDGING_ACCOUNT,
    RISK_REFRESH_INTERVAL_SEC,
    RISK_STATE_FILE,
    SYMBOL,
)
from event_log import EventLog
from risk_state import RiskStateStore

logger = logging.getLogger("SLDPAX_Risk")

_PNL_REBUILD_LOOKBACK_DAYS = 7


class RiskGovernor:
    def __init__(self, event_log: EventLog, state_path: str = RISK_STATE_FILE):
        self.events = event_log
        self.store = RiskStateStore(state_path)

        self.current_day_str: str = ""
        self.day_start_equity: float = 0.0
        self.current_equity: float = 0.0
        self.day_pnl: float = 0.0
        self.circuit_breaker_triggered: bool = False
        self.hard_cutoff_triggered: bool = False
        self.peak_equity: float = 0.0       # account-lifetime equity peak (DD basis)
        self.overall_dd_stopped: bool = False
        self.emergency_stop: bool = False
        self._flushed_peak: float = 0.0     # last peak persisted (avoids fsync spam)
        self.hedging_mode: bool = True

        # Per-employee tracking state
        self.last_trade_time: Dict[int, float] = {e: 0.0 for e in EMPLOYEES}
        self.daily_emp_trades: Dict[int, int] = {e: 0 for e in EMPLOYEES}
        self.daily_emp_losses: Dict[int, int] = {e: 0 for e in EMPLOYEES}
        self.daily_emp_wins: Dict[int, int] = {e: 0 for e in EMPLOYEES}

        # position_id -> {emp_id, side, pnl, ticket}
        self._tracked: Dict[int, dict] = {}
        # position_id -> emp_id, persisted so attribution survives restarts
        self._pos_emp_map: Dict[int, int] = {}
        self._processed_deals: set[int] = set()

        self._last_risk_refresh: float = 0.0
        self._last_reconcile: float = 0.0
        self._warned_history = False

        self._load_state()
        self.update_daily_state(force=True)

    # ================================================================= lifecycle
    def start(self) -> None:
        """Post-connect validation and position adoption. Raises on fatal misconfig."""
        acc = mt5.account_info()
        if acc is None:
            raise RuntimeError("MT5 account_info() unavailable. Is the terminal running and logged in?")

        HEDGING = getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", None)
        if HEDGING is None:  # defensive: real package value is 2
            HEDGING = 2
        self.hedging_mode = acc.margin_mode == HEDGING
        if not self.hedging_mode:
            msg = (
                "Account is in NETTING mode. MT5 merges same-symbol positions and discards "
                "per-employee magic numbers, which breaks the position cap and PnL attribution. "
                "Switch the account to hedging, or set REQUIRE_HEDGING_ACCOUNT = False in config.py "
                "to accept degraded risk accounting."
            )
            if REQUIRE_HEDGING_ACCOUNT:
                logger.error(msg)
                raise RuntimeError(msg)
            self.events.error("NETTING ACCOUNT - degraded position accounting: " + msg[:60])
        else:
            self.events.info(f"Hedging account confirmed ({acc.company}, login {acc.login})")

        if acc.trade_allowed == 0:
            self.events.error("ACCOUNT IS TRADE DISABLED (no funds / no trading rights). Desk will scan only.")
        if acc.trade_expert == 0:
            self.events.error("ACCOUNT HAS 'ALLOW LIVE TRADING' DISABLED for Expert Advisors. Orders will be rejected.")

        self._rebuild_position_pnl()
        self._adopt_open_positions()
        self.update_daily_state(force=True)
        self._save()

        self.events.info(
            f"Risk state restored | Day {self.current_day_str} anchor ${self.day_start_equity:,.2f} | "
            f"breaker={'ARMED' if self.circuit_breaker_triggered else 'clear'}"
        )

    # ================================================================ persistence
    def _load_state(self) -> None:
        data = self.store.load()
        if not data:
            return
        try:
            self.current_day_str = str(data.get("day_str", ""))
            self.day_start_equity = float(data.get("day_start_equity", 0.0))
            self.circuit_breaker_triggered = bool(data.get("circuit_breaker_triggered", False))
            self.hard_cutoff_triggered = bool(data.get("hard_cutoff_triggered", False))
            self.peak_equity = float(data.get("peak_equity", 0.0))
            self.overall_dd_stopped = bool(data.get("overall_dd_stopped", False))
            self.emergency_stop = bool(data.get("emergency_stop", False))
            self._flushed_peak = self.peak_equity
            self.last_trade_time = {int(k): float(v) for k, v in data.get("last_trade_time", {}).items()}
            self.daily_emp_trades = {int(k): int(v) for k, v in data.get("daily_emp_trades", {}).items()}
            self.daily_emp_wins = {int(k): int(v) for k, v in data.get("daily_emp_wins", {}).items()}
            self.daily_emp_losses = {int(k): int(v) for k, v in data.get("daily_emp_losses", {}).items()}
            self._pos_emp_map = {int(k): int(v) for k, v in data.get("pos_emp_map", {}).items()}
            self._processed_deals = {int(t) for t in data.get("processed_deals", [])}
            self._tracked = {int(k): v for k, v in data.get("tracked", {}).items()}
        except (TypeError, ValueError) as exc:
            logger.error("Risk state file is malformed (%s). Starting a fresh day.", exc)
            self.current_day_str = ""
            self.day_start_equity = 0.0
            self.circuit_breaker_triggered = False
            self.hard_cutoff_triggered = False
            self.peak_equity = 0.0
            self.overall_dd_stopped = False
            self.emergency_stop = False
            self._flushed_peak = 0.0
            self._tracked = {}
            self._pos_emp_map = {}
            self._processed_deals = set()

        for emp_id in EMPLOYEES:
            self.last_trade_time.setdefault(emp_id, 0.0)
            self.daily_emp_trades.setdefault(emp_id, 0)
            self.daily_emp_wins.setdefault(emp_id, 0)
            self.daily_emp_losses.setdefault(emp_id, 0)

    def _save(self) -> None:
        self.store.save(
            {
                "day_str": self.current_day_str,
                "day_start_equity": self.day_start_equity,
                "circuit_breaker_triggered": self.circuit_breaker_triggered,
                "hard_cutoff_triggered": self.hard_cutoff_triggered,
                "peak_equity": self.peak_equity,
                "overall_dd_stopped": self.overall_dd_stopped,
                "emergency_stop": self.emergency_stop,
                "last_trade_time": self.last_trade_time,
                "daily_emp_trades": self.daily_emp_trades,
                "daily_emp_wins": self.daily_emp_wins,
                "daily_emp_losses": self.daily_emp_losses,
                "pos_emp_map": self._pos_emp_map,
                "processed_deals": sorted(self._processed_deals),
                "tracked": self._tracked,
            }
        )

    # ================================================================ day state
    def update_daily_state(self, force: bool = False) -> None:
        """
        Refresh equity and enforce the daily circuit breaker.
        Throttled because `is_trade_allowed` also calls this on every signal.
        """
        now = time.time()
        if not force and (now - self._last_risk_refresh) < RISK_REFRESH_INTERVAL_SEC:
            return
        self._last_risk_refresh = now

        acc = mt5.account_info()
        if acc is None:
            return

        self.current_equity = acc.equity
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if today_str != self.current_day_str:
            self._rollover_day(today_str, acc.equity)

        self.day_pnl = self.current_equity - self.day_start_equity

        changed = self._refresh_peak()
        changed |= self._update_latches()

        if changed:
            self._save()

    def overall_dd_pct(self) -> float:
        """Drawdown from the account-lifetime equity peak, in percent."""
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.current_equity) / self.peak_equity * 100.0)

    def _refresh_peak(self) -> bool:
        """Raise the equity peak, persisting only when it crosses whole dollars
        so safety continuity survives a crash without fsyncing every tick."""
        if self.current_equity <= self.peak_equity:
            return False
        self.peak_equity = self.current_equity
        if int(self.peak_equity) != int(self._flushed_peak):
            self._flushed_peak = self.peak_equity
            return True
        return False

    def _update_latches(self) -> bool:
        """Evaluate the cascading risk latches in order. Returns True if any
        latch state changed and persistence is warranted."""
        changed = False

        # 1. Personal daily loss stop (proportional, latching until UTC rollover).
        max_allowed_loss = self.day_start_equity * (MAX_DAILY_LOSS_PCT / 100.0)
        if not self.circuit_breaker_triggered and max_allowed_loss > 0 and self.day_pnl <= -max_allowed_loss:
            self.circuit_breaker_triggered = True
            changed = True
            self.events.error(
                f"DAILY LOSS CIRCUIT BREAKER TRIPPED at -${abs(self.day_pnl):,.2f} "
                f"({MAX_DAILY_LOSS_PCT}% of ${self.day_start_equity:,.2f}). No new entries today."
            )

        # 2. Hard daily safety cutoff - superset of the personal stop; kept as a
        #    distinct latch so a reconfigured personal stop cannot silently weaken it.
        hard_loss = self.day_start_equity * (HARD_DAILY_LOSS_PCT / 100.0)
        if not self.hard_cutoff_triggered and hard_loss > 0 and self.day_pnl <= -hard_loss:
            self.hard_cutoff_triggered = True
            self.circuit_breaker_triggered = True
            changed = True
            self.events.error(
                f"HARD DAILY SAFETY CUTOFF at -${abs(self.day_pnl):,.2f} "
                f"({HARD_DAILY_LOSS_PCT}% of ${self.day_start_equity:,.2f}). Desk halted for the day."
            )

        # 3. Overall drawdown protection - persistent (not daily), latches until
        #    equity recovers above the protection line AND a new UTC day has rolled.
        dd = self.overall_dd_pct()
        if not self.overall_dd_stopped and dd >= OVERALL_DD_STOP_PCT:
            self.overall_dd_stopped = True
            changed = True
            self.events.error(
                f"OVERALL DD PROTECTION ACTIVE: -{dd:.2f}% from peak ${self.peak_equity:,.2f}. "
                f"No new entries while drawdown is at/over {OVERALL_DD_STOP_PCT:.1f}%."
            )

        # 4. Absolute emergency cutoff - flatten everything and halt until the
        #    operator deliberately clears the persisted risk state.
        if not self.emergency_stop and dd >= EMERGENCY_DD_PCT:
            self.emergency_stop = True
            changed = True
            self.events.error(
                f"ABSOLUTE EMERGENCY CUTOFF at -{dd:.2f}% from peak ${self.peak_equity:,.2f} "
                f"({EMERGENCY_DD_PCT:.1f}% threshold). Flattening all positions; desk will not resume "
                f"until risk_state is reset."
            )
            self._flatten_all("EMERGENCY_DD_CUTOFF")

        return changed

    def current_risk_percent(self) -> float:
        """Risk per trade after drawdown-tier reduction. Returns percent."""
        dd = self.overall_dd_pct()
        for dd_level, risk in sorted(DD_RISK_TIERS, reverse=True):
            if dd >= dd_level:
                return risk
        return ACCOUNT_RISK_PERCENT

    def _rollover_day(self, today_str: str, equity: float) -> None:
        """UTC day change: re-anchor equity, clear daily counters.

        Open positions are deliberately NOT discarded - they keep accumulating
        PnL and are scored when they finally close. The overall-drawdown stop
        survives if equity is still at/over the protection line; the emergency
        cutoff requires a manual risk_state reset and is never auto-cleared.
        """
        self.current_day_str = today_str
        self.day_start_equity = equity
        self.day_pnl = 0.0
        self.circuit_breaker_triggered = False
        self.hard_cutoff_triggered = False
        if self.overall_dd_stopped and self.overall_dd_pct() < OVERALL_DD_STOP_PCT:
            self.overall_dd_stopped = False
        self._processed_deals.clear()
        for emp_id in EMPLOYEES:
            self.daily_emp_trades[emp_id] = 0
            self.daily_emp_losses[emp_id] = 0
            self.daily_emp_wins[emp_id] = 0
        self.events.info(f"New UTC trading day {today_str}. Equity anchor ${equity:,.2f}.")
        self._save()

    @property
    def max_daily_loss_usd(self) -> float:
        return self.day_start_equity * (MAX_DAILY_LOSS_PCT / 100.0)

    # ============================================================ position book
    def _adopt_open_positions(self) -> None:
        """Re-attach to positions opened before this process started."""
        positions = mt5.positions_get(symbol=SYMBOL)
        if positions is None:
            logger.warning("positions_get() returned None during adoption; will retry next cycle.")
            return

        adopted = 0
        for p in positions:
            emp_id = MAGIC_TO_EMP.get(p.magic) or self._pos_emp_map.get(p.identifier)
            if emp_id is None:
                self.events.error(
                    f"Foreign position #{p.ticket} magic {p.magic} on {SYMBOL} is not managed by this desk."
                )
                continue
            self._pos_emp_map[p.identifier] = emp_id
            entry = self._tracked.setdefault(
                p.identifier,
                {"emp_id": emp_id, "side": "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
                 "pnl": 0.0, "ticket": p.ticket},
            )
            entry["emp_id"] = emp_id
            adopted += 1

        if adopted:
            self.events.info(f"Adopted {adopted} pre-existing office position(s).")

    def get_open_positions(self) -> List[Tuple[dict, int]]:
        """Return (mt5 position, employee_id) pairs owned by this desk."""
        positions = mt5.positions_get(symbol=SYMBOL)
        if positions is None:
            return []
        result = []
        for p in positions:
            emp_id = MAGIC_TO_EMP.get(p.magic) or self._pos_emp_map.get(p.identifier)
            if emp_id is not None:
                result.append((p, emp_id))
        return result

    def _open_position_counts(self) -> Tuple[int, Dict[int, int], Dict[str, int]]:
        pairs = self.get_open_positions()
        per_emp: Dict[int, int] = {}
        per_side: Dict[str, int] = {"BUY": 0, "SELL": 0}
        for p, emp_id in pairs:
            per_emp[emp_id] = per_emp.get(emp_id, 0) + 1
            side = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
            per_side[side] = per_side.get(side, 0) + 1
        return len(pairs), per_emp, per_side

    def net_exposure(self) -> Tuple[int, int]:
        """(buy count, sell count) across the office."""
        _, _, per_side = self._open_position_counts()
        return per_side.get("BUY", 0), per_side.get("SELL", 0)

    # ============================================================== emergency
    def _flatten_all(self, reason: str) -> None:
        """Best-effort market close of every office position (emergency cutoff).

        In MT5 hedging mode a position is closed by sending a DEAL with the
        position's `identifier` and the opposite direction. Nothing is counted as
        a scheduled exit - reconcile() scores the resulting deals normally.
        """
        pairs = self.get_open_positions()
        if not pairs:
            return
        sym_info = mt5.symbol_info(SYMBOL)
        tick = mt5.symbol_info_tick(SYMBOL)
        if sym_info is None or tick is None:
            self.events.error("Emergency flatten deferred: no symbol info / tick yet.")
            return

        fill_mode = sym_info.filling_mode
        fill = mt5.ORDER_FILLING_FOK if fill_mode & 1 \
            else mt5.ORDER_FILLING_IOC if fill_mode & 2 else mt5.ORDER_FILLING_RETURN

        closed, failed = 0, 0
        for p, emp_id in pairs:
            is_buy_pos = p.type == mt5.ORDER_TYPE_BUY
            close_type = mt5.ORDER_TYPE_SELL if is_buy_pos else mt5.ORDER_TYPE_BUY
            price = tick.bid if close_type == mt5.ORDER_TYPE_BUY else tick.ask
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": SYMBOL,
                "volume": p.volume,
                "type": close_type,
                "position": p.identifier,
                "price": price,
                "deviation": MAX_DEVIATION_POINTS,
                "magic": p.magic,
                "comment": reason[:20],
                "type_filling": fill,
                "sl": 0.0,
                "tp": 0.0,
            }
            result = mt5.order_send(request)
            if result is None or result.retcode not in (mt5.TRADE_RETCODE_DONE,
                                                        mt5.TRADE_RETCODE_DONE_PARTIAL):
                failed += 1
                self.events.error(
                    f"Emergency close failed for Emp {emp_id} position {p.identifier}: "
                    f"{getattr(result, 'comment', 'order_send returned None')}"
                )
            else:
                closed += 1
                self.events.info(f"Emergency close: Emp {emp_id} position {p.identifier} @ {price:.2f}")
        if failed:
            self.events.error(f"Emergency flatten incomplete ({failed} failed); will retry each cycle.")
        else:
            self.events.info(f"Emergency flatten complete: {closed} position(s) closed.")

    # ============================================================== permissions
    def is_trade_allowed(self, emp_id: int, side: Optional[str] = None) -> Tuple[bool, str]:
        """
        Verify whether an employee has clearance to execute a new trade.

        `side` is the intended direction of the incoming order ("BUY"/"SELL").
        The same-direction correlation cap is only meaningful for the side the
        new trade would *increase*; without it, a book already holding the
        maximum longs would wrongly block an unrelated short.
        """
        self.update_daily_state()

        if self.emergency_stop:
            return False, "ABSOLUTE EMERGENCY CUTOFF: account is under EMERGENCY_DD_PCT from peak equity. Reset risk_state to resume."
        if self.overall_dd_stopped:
            return False, (
                f"OVERALL DD PROTECTION: -{self.overall_dd_pct():.2f}% from peak. "
                f"No entries while drawdown is at/over {OVERALL_DD_STOP_PCT:.1f}%."
            )
        if self.hard_cutoff_triggered:
            return False, (
                f"OFFICE HALTED: hard daily safety cutoff hit "
                f"(-${abs(self.day_pnl):,.2f}, {HARD_DAILY_LOSS_PCT}% of day start)"
            )
        if self.circuit_breaker_triggered:
            return False, f"OFFICE HALTED: daily loss breaker hit (-${abs(self.day_pnl):,.2f})"

        total, per_emp, per_side = self._open_position_counts()

        if total >= MAX_OPEN_POSITIONS:
            return False, f"Position cap reached ({total}/{MAX_OPEN_POSITIONS})"

        if per_emp.get(emp_id, 0) >= MAX_POSITIONS_PER_EMP:
            return False, f"Employee {emp_id} already has {per_emp[emp_id]} open position(s)"

        if side is not None:
            side = side.upper()
            side_label = "Long" if side == "BUY" else "Short"
            if per_side.get(side, 0) >= MAX_SAME_DIRECTION_POSITIONS:
                return False, (
                    f"{side_label} exposure cap reached "
                    f"({per_side[side]}/{MAX_SAME_DIRECTION_POSITIONS})"
                )
        else:
            # Direction unknown: apply the stricter of the two books.
            worst = max(per_side.get("BUY", 0), per_side.get("SELL", 0))
            if worst >= MAX_SAME_DIRECTION_POSITIONS:
                return False, f"Directional exposure cap reached ({worst}/{MAX_SAME_DIRECTION_POSITIONS})"

        if self.daily_emp_losses[emp_id] >= MAX_DAILY_LOSSES_PER_EMP:
            return False, f"Employee {emp_id} benched for the day ({self.daily_emp_losses[emp_id]} loss)"

        if self.daily_emp_trades[emp_id] >= MAX_DAILY_TRADES_PER_EMP:
            return False, f"Employee {emp_id} hit daily trade cap ({MAX_DAILY_TRADES_PER_EMP})"

        elapsed = time.time() - self.last_trade_time[emp_id]
        if elapsed < MIN_ENTRY_INTERVAL_SEC:
            rem = int(MIN_ENTRY_INTERVAL_SEC - elapsed)
            return False, f"Employee {emp_id} cooling down ({rem}s)"

        return True, "CLEAR"

    # ============================================================== sizing
    @staticmethod
    def _snap_to_step(value: float, step: float, min_lot: float, max_lot: float) -> float:
        step = step if step and step > 0 else 0.01
        lots = math.floor(value / step + 1e-9) * step
        decimals = max(0, int(round(-math.log10(step)))) if step < 1 else 0
        lots = round(lots, decimals)
        return min(max_lot, max(min_lot, lots))

    def calculate_lot_size(self, sl_distance_usd: float) -> Tuple[Optional[float], float]:
        """
        Size the position so a stop-out costs `ACCOUNT_RISK_PERCENT` of equity.

        Returns (lot_size, actual_risk_usd). `lot_size` is None when even the
        broker minimum would breach `MAX_SINGLE_TRADE_RISK_PCT`; the caller must
        refuse the trade. Previously `max(min_lot, ...)` silently rounded UP
        past the risk budget with no warning.
        """
        acc_info = mt5.account_info()
        equity = acc_info.equity if acc_info else 0.0
        if equity <= 0:
            return None, 0.0

        sym_info = mt5.symbol_info(SYMBOL)
        if sym_info is None:
            return None, 0.0

        contract_size = sym_info.trade_contract_size if sym_info.trade_contract_size > 0 else 100.0
        min_lot = sym_info.volume_min or 0.01
        max_lot = sym_info.volume_max or 100.0
        lot_step = sym_info.volume_step or 0.01

        denom = contract_size * sl_distance_usd
        if denom <= 0:
            return None, 0.0

        target_risk = equity * (self.current_risk_percent() / 100.0)
        hard_ceiling = equity * (MAX_SINGLE_TRADE_RISK_PCT / 100.0)

        lot = self._snap_to_step(target_risk / denom, lot_step, min_lot, max_lot)
        actual_risk = lot * denom

        if lot <= min_lot + 1e-12 and actual_risk > target_risk:
            if actual_risk > hard_ceiling:
                self.events.block(
                    f"Size rejected: min lot {min_lot} risks ${actual_risk:,.2f} "
                    f"(>{MAX_SINGLE_TRADE_RISK_PCT}% ceiling) on a ${sl_distance_usd:.2f} stop."
                )
                return None, actual_risk
            self.events.block(
                f"Min-lot floor engaged: risk ${actual_risk:,.2f} exceeds target "
                f"${target_risk:,.2f} (within {MAX_SINGLE_TRADE_RISK_PCT}% ceiling)."
            )

        return lot, actual_risk

    # ============================================================ bookkeeping
    def record_trade_placed(self, emp_id: int, position_id: int, side: str, ticket: int) -> None:
        """Register a successfully sent order. Persisted immediately for crash safety."""
        self.last_trade_time[emp_id] = time.time()
        self.daily_emp_trades[emp_id] += 1
        self._pos_emp_map[position_id] = emp_id
        self._tracked[position_id] = {
            "emp_id": emp_id,
            "side": side,
            "pnl": 0.0,
            "ticket": ticket,
        }
        self._save()

    def _rebuild_position_pnl(self) -> None:
        """
        Reconstruct accumulated PnL (incl. swap/commission) for tracked positions
        from history, so a mid-trade restart still scores the position correctly.
        """
        if not self._tracked:
            return
        wanted = set(self._tracked)
        start = datetime.now(timezone.utc) - timedelta(days=_PNL_REBUILD_LOOKBACK_DAYS)
        deals = mt5.history_deals_get(start, datetime.now(timezone.utc))
        if deals is None:
            if not self._warned_history:
                self._warned_history = True
                logger.warning("history_deals_get returned None; restored PnL will be incomplete.")
            return

        for deal in deals:
            pid = deal.position_id
            if pid in wanted:
                self._processed_deals.add(deal.ticket)
                if deal.entry == mt5.DEAL_ENTRY_OUT:
                    self._tracked[pid]["pnl"] += deal.profit + deal.swap + deal.commission

    def reconcile(self, force: bool = False) -> None:
        """
        Incrementally score closed positions.

        Deals are de-duplicated by ticket, so re-running this is idempotent. A
        position is scored once, when it is no longer present in the open book,
        which also fixes partial closes being counted as separate results.
        """
        now = time.time()
        if not force and (now - self._last_reconcile) < RECONCILE_INTERVAL_SEC:
            return
        self._last_reconcile = now

        try:
            day_start_dt = datetime.strptime(self.current_day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            day_start_dt = datetime.now(timezone.utc) - timedelta(days=1)

        deals = mt5.history_deals_get(day_start_dt, datetime.now(timezone.utc))
        if deals is None:
            return

        changed = False
        for deal in deals:
            if deal.ticket in self._processed_deals:
                continue
            self._processed_deals.add(deal.ticket)
            changed = True
            if deal.entry != mt5.DEAL_ENTRY_OUT:
                continue
            pid = deal.position_id
            entry = self._tracked.get(pid)
            if entry is None:
                emp_id = MAGIC_TO_EMP.get(deal.magic) or self._pos_emp_map.get(pid)
                if emp_id is None:
                    continue
                entry = {"emp_id": emp_id, "side": "?", "pnl": 0.0, "ticket": pid}
                self._tracked[pid] = entry
                self._pos_emp_map[pid] = emp_id
            entry["pnl"] += deal.profit + deal.swap + deal.commission

        if not changed and not self._tracked:
            return

        # Score positions that have left the open book. A None result from
        # positions_get() means "unknown", NOT "closed" - never finalize on that.
        open_ids = {p.identifier for p, _ in self.get_open_positions()}
        still_open: Dict[int, dict] = {}
        for pid, entry in self._tracked.items():
            if pid in open_ids or self._probe_open(pid):
                still_open[pid] = entry
            else:
                self._finalize(pid, entry)

        if still_open != self._tracked:
            self._tracked = still_open
            changed = True
        if changed:
            self._save()

    def _probe_open(self, position_id: int) -> bool:
        """Defensive check used only when the open book came back empty."""
        positions = mt5.positions_get(symbol=SYMBOL)
        if positions is None:
            return True  # Unknown state: assume open, score it next cycle.
        return any(p.identifier == position_id for p in positions)

    def _finalize(self, pid: int, entry: dict) -> None:
        emp_id = entry["emp_id"]
        pnl = float(entry["pnl"])
        name = EMPLOYEES[emp_id].name if emp_id in EMPLOYEES else f"Emp{emp_id}"
        if pnl > 0:
            self.daily_emp_wins[emp_id] += 1
            self.events.info(f"Emp {emp_id} {name} CLOSED +${pnl:,.2f} (win)")
        elif pnl < 0:
            self.daily_emp_losses[emp_id] += 1
            self.events.block(f"Emp {emp_id} {name} CLOSED -${abs(pnl):,.2f} (loss)")
        else:
            self.events.info(f"Emp {emp_id} {name} CLOSED flat (${pnl:,.2f})")
        self._pos_emp_map.pop(pid, None)
        logger.info("Finalized position %s emp=%s pnl=%.2f", pid, emp_id, pnl)

    def flush(self) -> None:
        self._save()
