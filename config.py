"""
================================================================================
Configuration Module: Funded Master Account Portfolio
8-Employee Trading Desk with tailored Risk:Reward (RR) profiles.
================================================================================
Single source of truth for every tunable. Nothing here is hardcoded elsewhere.
"""

from dataclasses import dataclass

# --- Platform / Symbol -------------------------------------------------------
SYMBOL = "XAUUSD"
# Magic numbers are DERIVED, never hardcoded, so they can never collide with
# another EA on the same MT5 account and stay auditable at a glance.
MAGIC_OFFSET = 101000

# --- Master Portfolio Risk Governor -----------------------------------------
# Operating risk framework (operator policy, 2026-09-29):
#   Risk per trade         0.50% absolute max (min-lot may not breach it)
#   Normal operating risk  0.25-0.50%  -> ACCOUNT_RISK_PERCENT is the default
#   Risk after -3% DD         0.25%    -> DD_RISK_TIERS
#   Risk after -5% DD         0.125%
#   Risk after -6% DD         STOP      -> OVERALL_DD_STOP_PCT
#   Absolute emergency        -7.0%     -> EMERGENCY_DD_PCT: flatten + halt
# "DD" is drawdown from the account's historical equity peak and is persisted
# across restarts, so a mid-session crash cannot hide a drawdown that already
# happened.
ACCOUNT_RISK_PERCENT = 0.35         # Default risk per trade (% of equity)
MAX_SINGLE_TRADE_RISK_PCT = 0.50    # Absolute ceiling; min-lot may not exceed this
MAX_DAILY_LOSS_PCT = 2.0            # Personal daily loss stop (benches the desk)
HARD_DAILY_LOSS_PCT = 3.0           # Hard daily safety cutoff (belt-and-braces latch)
MAX_OPEN_POSITIONS = 1              # Concurrent office-wide cap (max 1 position)
MAX_POSITIONS_PER_EMP = 1           # One open position per employee
MAX_SAME_DIRECTION_POSITIONS = 1    # Correlated stacking is disallowed by the 1-cap
MAX_DAILY_TRADES_PER_EMP = 6        # Anti-thrash daily ceiling
MAX_DAILY_LOSSES_PER_EMP = 1        # One loss benches an employee for the day
MIN_ENTRY_INTERVAL_SEC = 1800       # 30 min cooldown between an employee's entries
# No averaging down, no martingale, no risk increase after wins by design: sizing
# is fixed-fractional of current equity and is never raised after a win.

# Overall drawdown from equity peak is tiered (drawdown %, risk % of equity).
DD_RISK_TIERS = ((3.0, 0.25), (5.0, 0.125))
OVERALL_DD_STOP_PCT = 6.0           # At -6% from peak: STOP new entries entirely
EMERGENCY_DD_PCT = 7.0              # At -7%: flatten everything + halt until the
                                    # operator clears risk_state (absolute cutoff)

# --- Volatility & Structure Filters -----------------------------------------
ATR_MULTIPLIER = 1.2
ATR_PERIOD = 14
MIN_STOP_DISTANCE_USD = 1.20        # Gold USD noise buffer

# The stop is 1.2 x ATR, and ATR scales with the timeframe, so a single absolute
# ceiling starves the slower desks: measured on one year of XAUUSD, a flat $12
# cap discarded 9% of M5 signals but 40% of M15 and 85% of M30. The ceiling is
# therefore per timeframe, set just above each frame's 95th-percentile stop so
# it still refuses genuinely explosive volatility without blinding a whole desk.
# Re-check with `python backtest\diagnose.py` after changing brokers.
MAX_STOP_DISTANCE_BY_TF = {"M5": 18.00, "M15": 30.00, "M30": 45.00}
MAX_STOP_DISTANCE_USD = 18.00       # Fallback for an unknown timeframe (M5 band)
BREAKOUT_BUFFER_ATR = 0.10          # Close must clear a level by this much ATR

# --- Execution Safety -------------------------------------------------------
MAX_SPREAD_USD = 0.75               # XAUUSD averages $0.30-0.50 here; see note
MAX_DEVIATION_POINTS = 50           # Max slippage tolerance passed to broker
MIN_ACCEPTABLE_RR = 1.20            # Reject any setup whose effective R:R is worse
REQUIRE_HEDGING_ACCOUNT = True      # See README: netting merges employee positions

# The spread ceiling must sit ABOVE the instrument's normal range or it stops
# being an abnormal-spread filter and becomes a near-best-spread-only filter.
# XAUUSD on this desk averages $0.30-0.50 and widens ~1.6x at the London/NY
# opens, so $0.60 sat inside the normal band: a one-year replay refused 2,775
# signals and left Employees 1, 3 and 7 completely dormant. $0.75 admits normal
# and session conditions and still refuses the thin-book rollover. Re-check with
# `python backtest\spread_check.py` if the broker changes.

# Employees that must not open new positions. The profile stays in EMPLOYEES so
# magic numbers, persisted risk state and the dashboard are unaffected; the
# executor refuses them at the order boundary.
# OPERATOR DECISION 2026-09-29: run ALL 8 employees live first, then optimise.
# Backtest evidence that cost money and can be re-applied with one line:
#   - Employee 7 (NY Impulse Fade): lost on live replay - 109 trades, 27.5% win
#     rate, PF 0.73, expectancy -0.174R. It had been dormant only because the
#     spread ceiling happened to block its 13:30-15:30 window.
#   - Employee 4 (NR7 Breakout): 250+ trades at PF 0.87-0.89, expectancy
#     -0.07 to -0.09R at the desk's real $0.30-0.50 spread. It pays more in
#     spread and losses than its winners recover.
#   - Employee 6 (FVG Inversion): ~6,000/year of its signals fail the pre-flight
#     "inverted bracket (target behind price)" guard; ~4 trade. Geometry bug.
# Flip any of them back off by re-adding the id to this set.
DISABLED_EMPLOYEES: frozenset[int] = frozenset()

# --- Employee Strategy Profiles & Tailored Risk:Reward ----------------------
@dataclass(frozen=True)
class EmployeeProfile:
    emp_id: int
    name: str
    timeframe: str
    rr_ratio: float
    description: str
    adx_threshold: float = 0.0       # Only used by momentum strategies
    max_adx_to_fade: float = 0.0     # Only used by mean-reversion strategies

EMPLOYEES = {
    1: EmployeeProfile(
        emp_id=1,
        name="London Breakout",
        timeframe="M5",
        rr_ratio=2.0,
        description="European session volume surge out of the Asian range (07:00-08:30 UTC)",
    ),
    2: EmployeeProfile(
        emp_id=2,
        name="Break & Retest",
        timeframe="M15",
        rr_ratio=2.0,
        description="Structural support/resistance breakout and confirmation retest",
    ),
    3: EmployeeProfile(
        emp_id=3,
        name="ICT Silver Bullet",
        timeframe="M5",
        rr_ratio=2.0,
        description="Algorithmic liquidity sweep and reclaim in NY AM macro (14:00-15:00 UTC)",
    ),
    4: EmployeeProfile(
        emp_id=4,
        name="NR7 Volatility Breakout",
        timeframe="M15",
        rr_ratio=3.0,
        description="Asymmetric runner on 7-bar range compression then expansion",
    ),
    5: EmployeeProfile(
        emp_id=5,
        name="VWAP Trend Rebound",
        timeframe="M30",
        rr_ratio=3.5,
        description="Deep value mean-reversion at session VWAP, aligned with H1 200 EMA",
    ),
    6: EmployeeProfile(
        emp_id=6,
        name="FVG Inversion",
        timeframe="M15",
        rr_ratio=1.5,
        description="Inversion Fair Value Gap rebalance targeting the gap midpoint",
    ),
    7: EmployeeProfile(
        emp_id=7,
        name="NY Impulse Fade",
        timeframe="M5",
        rr_ratio=2.0,
        description="Fades the NY open liquidity sweep back to session median, never in strong trend",
        max_adx_to_fade=30.0,
    ),
    8: EmployeeProfile(
        emp_id=8,
        name="ADX Trend Rider",
        timeframe="M15",
        rr_ratio=3.0,
        description="Momentum continuation on ADX expansion with widening directional index spread",
        adx_threshold=25.0,
    ),
}


# --- Derived lookup tables --------------------------------------------------
def magic_for(emp_id: int) -> int:
    """Deterministic magic number for an employee."""
    return MAGIC_OFFSET + emp_id


MAGIC_TO_EMP: dict[int, int] = {magic_for(e): e for e in EMPLOYEES}
EMP_TO_MAGIC: dict[int, int] = {e: magic_for(e) for e in EMPLOYEES}
OFFICE_MAGICS: frozenset[int] = frozenset(MAGIC_TO_EMP)


def max_stop_distance_usd(timeframe: str) -> float:
    """Volatility ceiling for the timeframe the signal was generated on."""
    return MAX_STOP_DISTANCE_BY_TF.get(timeframe, MAX_STOP_DISTANCE_USD)


# Internal consistency guard
for _emp_id, _profile in EMPLOYEES.items():
    assert _profile.emp_id == _emp_id, f"EMPLOYEES key {_emp_id} != emp_id {_profile.emp_id}"
    assert _profile.rr_ratio >= MIN_ACCEPTABLE_RR, (
        f"Employee {_emp_id} RR { _profile.rr_ratio } is below MIN_ACCEPTABLE_RR"
    )
    assert len({magic_for(e) for e in EMPLOYEES}) == len(EMPLOYEES), "Duplicate magic number"

assert DISABLED_EMPLOYEES <= set(EMPLOYEES), "DISABLED_EMPLOYEES names an unknown employee"
assert set(MAX_STOP_DISTANCE_BY_TF) >= {p.timeframe for p in EMPLOYEES.values()}, (
    "MAX_STOP_DISTANCE_BY_TF is missing a timeframe used by an employee"
)
for _tf, _cap in MAX_STOP_DISTANCE_BY_TF.items():
    assert _cap > MIN_STOP_DISTANCE_USD, f"{_tf} stop ceiling below the noise floor"

# --- Engine Runtime ---------------------------------------------------------
LOOP_INTERVAL_SEC = 1.0
RECONCILE_INTERVAL_SEC = 5.0
RISK_REFRESH_INTERVAL_SEC = 0.25     # Throttle repeated account_info() calls
BAR_SIGNAL_GRACE_SEC = 90            # Catch a bar if the loop stalled briefly
MT5_RECONNECT_MAX_ATTEMPTS = 10
MT5_RECONNECT_DELAY_SEC = 5.0

# --- Web Dashboard ----------------------------------------------------------
WEB_DASHBOARD_HOST = "127.0.0.1"     # Local only - never expose this port
WEB_DASHBOARD_PORT = 8080

# --- Data & Logging ---------------------------------------------------------
BAR_COUNTS = {"M5": 200, "M15": 200, "M30": 200, "H1": 250}
VWAP_M5_BARS = 300                   # Full 24h UTC session needs 288 M5 bars
MIN_HISTORY_BARS = 200               # Required for a trustworthy H1 EMA200
RISK_STATE_FILE = "risk_state.json"
LOG_FILE = "trading_engine.log"
LOG_MAX_BYTES = 2_000_000
LOG_BACKUP_COUNT = 5
EVENT_LOG_SIZE = 200
