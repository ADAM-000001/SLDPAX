# SLDPAX — Funded Master Account Trading Desk

MetaTrader 5 automation for XAUUSD running eight independent strategy
"employees" against a single funded account, under a shared risk governor.

**This trades real money. Read [Safety model](#safety-model) and validate on a
demo account first.**

---

## Requirements

* Windows (the launcher targets `cmd.exe`)
* MetaTrader 5 terminal, running and logged in to the funded account
* Python 3.10+ (developed against 3.11)

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Running

Double-click `START_SLDPAX.bat`: it (re)opens MetaTrader 5, launches the engine
in a **minimized background console**, waits a few seconds, then opens the
**browser dashboard** at `http://127.0.0.1:8080/`. Or run directly:

```bat
python main.py
```

The engine refuses to start twice on the same account (`sldpax.lock`), so
re-double-clicking just reopens the dashboard. Close the launcher window freely;
the engine keeps running minimized (Ctrl+C or Task Manager to stop).

Exit codes: `0` clean, `1` MT5 init/loop failure, `2` account validation
failure, `3` terminal connection lost repeatedly, `4` another engine instance
already running (guarded by `sldpax.lock`).

## Verifying

```bat
python tests\test_core.py
python tests\test_strategies.py
```

Both stub MetaTrader5 and run fully offline. See `tests/README.md`.

## Backtesting

```bat
python backtest\fetch_data.py 365     :: cache ~1 year of M5/M15/M30/H1
python backtest\fetch_data.py 365 --terminal "C:\Program Files\MetaTrader 5"
python backtest\data_quality.py       :: sanity-check the feed first
python backtest\runner.py --tag base  :: full-year replay (~10 min)
python backtest\compare.py            :: scenarios + fragility checks
```

The backtester runs this exact engine. `backtest/simulator.py` substitutes
`MetaTrader5` before the engine is imported, so strategy, sizing, risk caps and
execution pre-flight are the production code, not a re-implementation. The
replay clock is injected into `time.time()` and `datetime.now()`, forming bars
are flattened to their open to block lookahead, and a bar touching both stop and
target is scored as a stop.

**Read `backtest/README.md` before trusting any result** — in particular the
data caveat: the bundled history is a MetaQuotes *demo* feed, not a real broker's
gold, and its volatility is 2–3× reality. Point `fetch_data.py` at your own
terminal for figures that mean anything.

---

## Architecture

| Module | Responsibility |
| --- | --- |
| `config.py` | Every tunable. Magic numbers are **derived** (`MAGIC_OFFSET + emp_id`), never hardcoded. Self-validating at import. |
| `main.py` | Connect, validate, run the loop, MT5 watchdog, shutdown safety. |
| `market_data.py` | MT5 → pandas feeds, Wilder RMA ATR/ADX, EMA, NR7, FVG, session VWAP. |
| `risk_governor.py` | Compliance, circuit breaker, exposure caps, sizing, PnL reconciliation. |
| `order_executor.py` | Ten-stage pre-flight, then `order_send`. |
| `team_office.py` | The 8 signal generators. |
| `web_dashboard.py` | Local browser dashboard (http://127.0.0.1:8080) on a background thread; engine runs headless. |
| `risk_state.py` | Crash-safe JSON persistence of all risk-critical state. |
| `event_log.py` | In-memory ring buffer feeding the web dashboard's activity log. |
| `tests/` | Offline regression suite with a stubbed MT5. |

`risk_state.json` is written atomically (temp file + `os.replace` + `fsync`)
after every state change.

---

## Safety model

Enforced in `RiskGovernor.is_trade_allowed()` and `OrderExecutor.execute_order()`:

| Guard | Default |
| --- | --- |
| Risk per trade (normal) | 0.35% of equity (band 0.25–0.50%) |
| Hard single-trade ceiling (min-lot cannot breach) | 0.50% |
| Risk tier after −3% DD from equity peak | 0.25% |
| Risk tier after −5% DD | 0.125% |
| Daily loss circuit breaker | 2.00%, **latches** until UTC rollover |
| Hard daily safety cutoff | 3.00% (second latch) |
| Overall DD protection | 6.00% from peak: **STOP** new entries |
| Absolute emergency cutoff | 7.00% from peak: **flatten + halt** until risk_state is reset |
| Office open positions | 1 |
| Positions per employee | 1 |
| Same-direction positions | 1 (subsumed by the 1-position cap) |
| Trades per employee per day | 6 |
| Losses per employee per day | 1 (benches them) |
| Entry cooldown per employee | 30 min |
| ATR stop distance | M5 $1.20–$18, M15 $1.20–$30, M30 $1.20–$45 (`MAX_STOP_DISTANCE_BY_TF`) |
| Max spread | $0.75 (XAUUSD averages $0.30–$0.50 on this desk) |
| Minimum effective R:R | 1:1.20 |

No averaging down, no martingale, no risk increase after wins: sizing is
fixed-fractional of equity and is never raised after a win. After a stop-loss the
employee is benched for the day (`MAX_DAILY_LOSSES_PER_EMP = 1`) plus a 30-minute
cooldown — there is no automatic immediate re-entry.

"DD" is drawdown from the account's **historical equity peak**, which is
persisted (crash-safe) so a mid-session restart cannot hide a drawdown that
already happened. The emergency cutoff closes every office position as a market
deal and refuses to trade until the operator deliberately clears `risk_state`.

Order pre-flight, in order: risk clearance → symbol tradability → live tick →
spread ceiling → stop-distance sanity → broker `trade_stops_level` → lot sizing
vs hard ceiling → effective R:R floor → `order_check` margin validation →
`order_send`.

### Account mode matters

**The account must be in HEDGING mode.** On a netting account MetaTrader merges
same-symbol positions and keeps only the first position's magic number, which
destroys both the position cap and per-employee PnL attribution. The desk
refuses to start unless `REQUIRE_HEDGING_ACCOUNT = False` is set deliberately in
`config.py`.

### Restart safety

Daily equity anchor, circuit-breaker state, per-employee counters, cooldowns,
reconciled deal tickets and the position→employee map all persist to
`risk_state.json`. A mid-session restart **restores** the day anchor rather than
re-anchoring to current equity, so realised losses cannot be erased by
relaunching the process.

---

## The 8 employees

| # | Name | TF | RR | Setup |
| --- | --- | --- | --- | --- |
| 1 | London Breakout | M5 | 1:2.0 | Buffered break of the 00:00–07:00 UTC Asian range, 07:00–08:30 UTC |
| 2 | Break & Retest | M15 | 1:2.0 | Break of the 20-bar extreme, then retest holding the level |
| 3 | ICT Silver Bullet | M5 | 1:2.0 | 10-bar liquidity sweep and reclaim, 14:00–15:00 UTC |
| 4 | NR7 Breakout | M15 | 1:3.0 | 7-bar compression, then buffered expansion of its range |
| 5 | VWAP Rebound | M30 | 1:3.5 | Reclaim of the 00:00 UTC session VWAP in the H1 direction |
| 6 | FVG Inversion | M15 | 1:1.5 | Price closes back through a volatility-scaled Fair Value Gap, target the gap midpoint |
| 7 | NY Impulse Fade | M5 | 1:2.0 | Fade the NY open sweep back inside range; **disabled when M5 ADX > 30** |
| 8 | ADX Trend Rider | M15 | 1:3.0 | ADX > 25 with a *widening* +DI/−DI spread |

All eight share a single H1 EMA200 regime filter. All sessions are UTC, matching
MT5's epoch bar timestamps.

---

## Notes on indicator fidelity

ATR and ADX use Wilder's RMA (`ewm(alpha=1/period)`), matching MT5's built-ins.
An earlier version used a simple moving average, which produced materially
different values and made thresholds calibrated in MT5 useless.

`ATR_PERIOD`, `BREAKOUT_BUFFER_ATR`, `MIN_STOP_DISTANCE_USD` and the per-employee
`adx_threshold` / `max_adx_to_fade` values live in `config.py`. Recalibrate
against your broker's XAUUSD feed before going live — these were not
backtested.

---

## Known limitations

* The one-year backtest runs on a MetaQuotes *demo* feed whose volatility is 2–3×
  a real gold series. The harness and the ranking of the strategies are sound;
  the absolute P&L is not transferable. Re-run `fetch_data.py --terminal <path>`
  against your own broker before sizing real capital.
* The measured edge is **much narrower than 8 employees**. On one year of replay
  at the desk's real $0.30–0.50 spread, Employees 2 and 8 contribute ~47% of net;
  Employees 4 (NR7) and 7 (NY Impulse Fade) lost money (−$888 / −$460, PF < 1) and
  Employee 6 (FVG inversion) is effectively dead: ~6,000 of its signals fail the
  pre-flight "inverted bracket (target behind price)" guard each year and only ~4
  reach a live trade, because the gap midpoint is normally behind entry by the
  next bar. **All 8 are enabled live** by operator decision; the optimisation
  phase starts from these three: fix 7's trend filter, rebuild 6's target
  geometry, or re-add 4 and 7 to `DISABLED_EMPLOYEES` (one line).
* `MAX_STOP_DISTANCE_BY_TF` is a volatility-regime filter calibrated on the demo
  feed (just above each timeframe's 95th-percentile ATR stop). **Re-calibrate it
  with `python backtest\diagnose.py` against your own broker** before trusting it:
  widening it raised absolute profit but cut profit factor 1.29 → 1.19, expectancy
  0.205R → 0.139R, positive months 8/12 → 6/12 and max drawdown 9.9% → 12.6%.
* No news/calendar filter. Entries can be attempted around high-impact releases.
* No session-liquidity guard for the rollover and the daily open.
* Exit only by broker-side SL/TP. There is no trailing stop or time stop.
* The desk does not flatten positions on shutdown; manage open trades in MT5.
* `risk_state.json` is not reconciled against broker-side history on startup
  beyond a 7-day PnL rebuild.
* The backtester does not exercise the dashboard or the MT5 reconnect watchdog.
