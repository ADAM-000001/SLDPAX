# SLDPAX Backtester

Replays the **production** engine over historical bars. Nothing in the strategy,
sizing, risk caps or execution pre-flight is re-implemented for the backtest —
the only thing swapped out is the `MetaTrader5` module, which is replaced by
`backtest/simulator.py` *before* any engine module is imported. What gets
measured is what would trade.

## Usage

```
python backtest\fetch_data.py 365      # cache ~1 year of M5/M15/M30/H1 into data\
python backtest\data_quality.py        # sanity-check the feed before trusting results
python backtest\runner.py --tag base   # full-year replay, ~10 minutes
python backtest\compare.py             # side-by-side + fragility checks
```

To pull history from your **broker's** terminal instead of whichever one is
running, point it at the install directory (or the executable):

```
python backtest\fetch_data.py 365 --terminal "C:\Program Files\MetaTrader 5"
python backtest\fetch_data.py 365 --terminal "...\terminal64.exe" --force
```

`fetch_data.py` always prints the terminal company, build and data path it read
from — check that line before trusting any result. Use `--force` to discard the
existing cache; without it the CSVs are reused as-is.

Useful flags: `--spread 0.50` (pessimistic cost scenario vs the 0.40 default),
`--max-bars 6000` (quick slice), `--balance 10000`, `--min-h1 200`.

## How the replay works

1. `simulator.install()` publishes a fake `MetaTrader5` into `sys.modules`.
2. `runner.patch_clocks()` redirects `time.time()` and `datetime.now(utc)` inside
   `market_data`, `risk_governor` and `event_log` to the replay clock, so
   cooldowns, the daily circuit breaker and bar-age grace windows all advance
   with the backtest instead of wall-clock time.
3. Per M5 bar, in this order:
   - bar *i-1* has closed → resolve stop/targets against its true high/low
   - bar *i* has just opened → clock jumps to *i*, then
     `update_daily_state()` → `reconcile()` → `evaluate_all()` — the exact
     `main.py` loop body
   - orders fill at bar *i*'s open, the same price the engine derived from the
     tick it read

## Anti-lookahead

- The visible forming bar is **flattened to `O=H=L=C=open`**. Any code that
  accidentally reads the forming bar sees only its open, so a future leak cannot
  silently flatter the result.
- All earlier bars are delivered with true OHLC.
- `symbol_info_tick` quotes the forming bar's open ± modelled spread.
- `RiskGovernor` runs against a real simulated account (balance, equity, margin,
  open positions, deal history), so sizing, exposure caps and the daily breaker
  see the same state they would live.

## Fills and costs

- Market orders fill at exactly the submitted price (no favourable slippage).
- Stops/targets are absolute, so the spread is paid once on entry.
- A bar that gaps through a stop exits at the worse of the open and the stop.
- A bar that touches both stop and target assumes the **stop** filled first.
- Spread is modelled, never taken from the feed's `spread` column — on the
  MetaQuotes demo feed that column is meaningless (max $595 on a ~$4,400
  instrument). Model = base ± session multiplier × a volatility term.
- Commission and swap are configurable; defaults are commission-free CFD and
  −$8.00/lot/day with triple Wednesday rollover.

## What the backtest does NOT cover

- The dashboard and the MT5 watchdog in `main.py` (cosmetic / reconnect only).
- Real broker behaviour: requotes, slippage, partial fills, swap rates that
  differ from the model, and any symbol-specific contract quirks.
- The data itself. See below.

## Data caveat — read this before trusting any number

The bundled history came from a **MetaQuotes demo terminal**, not a real broker.
That feed is not a faithful XAUUSD series:

- yearly range $3,764–$5,589 (≈48%): real gold did not do this
- 5-minute bars with up to 2.9% moves and $212 intrabar ranges
- 127 bars with >$5 open gaps
- the `spread` column ranges up to $595 and must not be used

Absolute P&L is therefore **not transferable** to a live broker account. What
*is* meaningful: signal frequency, expectancy in R, per-employee edge ranking,
drawdown behaviour, and the identification of structural defects. For a
trustworthy P&L figure, re-run `fetch_data.py` against your own broker's
terminal.

One consequence to keep in mind: the demo feed's volatility is roughly 2–3× a
real gold feed, so a single global stop cap (the old `MAX_STOP_DISTANCE_USD =
12.00`, calibrated on M5 where only 9% of bars exceed it) discarded 40% of M15
and 85% of M30 signals. This is why `MAX_STOP_DISTANCE_BY_TF` exists:
`{M5: 18, M15: 30, M30: 45}`, each just above that timeframe's 95th-percentile
ATR stop on this feed. **Re-calibrate those figures on your own broker's data**
before taking the absolute numbers seriously.

## Latest reference run

Full-year replay, engine unmodified, all 8 employees live, spread $0.40 base
(matches the desk's real 0.30–0.50), with the governing risk rules active
(trades are impossible once an overall drawdown latch trips).

| Metric | all8 |
| --- | --- |
| Net P&L (from $10k) | −$397 (−4.0%) |
| Max drawdown | $624 (6.1%) |
| Profit factor | 0.86 |
| Expectancy | −0.116R |
| Trades | 148 |
| Daily breaker trips | 0 |
| Simulator rejects | 0 |
| Risk latches held | overall DD protection |

The demo feed tripped the **6.0% overall DD protection** on 2025-12-09 (−6.1%
from the $10,227 peak) and equity never recovered above the 6% line afterwards,
so the desk correctly parked for the remaining ~9.5 months — the controls
working exactly as designed. On a real (2–3× calmer) gold feed the deck should
survive far longer; these are the operator's exact thresholds, not a verdict on
real gold.

Per-employee for `all8` (`trades / win% / PF / net / expR`): Emp 1 5/20%/0.58/−46/−0.400,
Emp 2 7/28.6%/0.73/−43/−0.143, Emp 3 1/0%/0/−24/−1.000, Emp 4 38/13.2%/0.47/−406/−0.475,
Emp 5 39/25.6%/1.28/+179/0.153, Emp 6 0, Emp 7 11/27.3%/0.71/−71/−0.182,
Emp 8 47/25.5%/1.02/+15/0.021.

The 5,999 `inverted bracket (target behind price)` blocks in `report_base.json`
are Employee 6's geometry: after the desk paid the spread, the FVG-gap midpoint
is behind the entry ~99% of the time. The pre-flight guard is doing its job; the
strategy is not. Rebuild 6 or disable it.

The spread ceiling is `MAX_SPREAD_USD = 0.75` because XAUUSD averages
$0.30–0.50 and opens widen ~1.6×; at the old $0.60, normal session spreads were
above the ceiling and Employees 1/3/7 never traded. See `spread_check.py`.

Since 2026-09-29 the risk model matches the operator's governing rules exactly:
`MAX_OPEN_POSITIONS = 1`, risk tiers `{3%: 0.25, 5%: 0.125}` off the equity
peak, daily stop 2% + hard daily cutoff 3%, overall DD protection at 6%
(no entries) and an absolute emergency cutoff at 7% that flattens the desk and
latches until `risk_state` is cleared. The governor runs the same code live, so
the backtest exercises these lashes too — a replay that breaches a threshold
will show the desk stopping/flattening at exactly that point.
