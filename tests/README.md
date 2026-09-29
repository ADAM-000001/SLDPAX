# Verification suite

Offline regression tests for the rectified engine. `mt5_stub.py` replaces
MetaTrader5 with a controllable fake, so nothing touches a real terminal or
account.

```bat
python tests\test_core.py
python tests\test_strategies.py
```

Each script exits non-zero on failure and prints `ALL CHECKS PASSED` on success.

## What is covered

`test_core.py` — risk and execution layer

* ATR matches an independent Wilder RMA recursion, and is provably no longer the
  old SMA variant
* ADX is 0/NaN-free in degenerate data and > 40 in a clean trend
* `is_nr7(offset=-3)` flags the compression bar; `offset=-2` does not
* FVG detection: correct void bounds, midpoint, direction, noise filtering, and
  that a gap never lands on the signal bar
* Position sizing floors to the broker lot grid with no float drift; the min-lot
  overshoot is surfaced; a stop that would breach `MAX_SINGLE_TRADE_RISK_PCT`
  is **refused** rather than silently up-sized
* Persistence: day anchor, counters, cooldowns and the latched circuit breaker
  all survive a restart; a corrupt state file is quarantined
* Reconciliation: one loss counted once after five passes, PnL includes swap and
  commission, partial closes do not score, and `positions_get() -> None` does not
  finalize a position
* Exposure caps: office cap, per-employee cap, **direction-aware** correlation
  cap, daily trade cap, cooldown, benching
* A netting account is refused at startup
* Executor pre-flight: spread ceiling, stop-distance bands, broker
  `trade_stops_level`, structural R:R floor, `order_check` margin gate, and that
  a broker rejection neither counts as a trade nor starts a cooldown

`test_strategies.py` — signal layer

* `is_new_bar` consumes each bar exactly once, survives a stalled loop inside the
  grace window, drops it outside, and degrades quietly on a dead feed
* The H1 filter uses the last **closed** bar, and thin history forces SCANNING ONLY
* Employee 6 fires on a real FVG inversion in both directions with the gap
  midpoint as target, stays out on regime disagreement and on a non-inverted gap
* The old `abs(close - ema20) < 0.40` condition is demonstrated to be
  near-always true, confirming the reported bug
* Employee 8's ADX and widening DI spread are exercised in trend and chop
* 50 `evaluate_all` cycles on live-shaped data emit no exceptions and no
  malformed signals

## Not covered

No backtest, no performance or PnL validation. The tests assert *correctness of
logic*, not *profitability of strategy*.
