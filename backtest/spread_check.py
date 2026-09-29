"""What does the spread ceiling actually admit at the desk's 0.30-0.50 spread?

Uses the live simulator cost model so this stays in sync with the replay.
Normal = the operator's average (0.30-0.50); session = rollover multipliers
are the model's. Ceiling verdicts are shown for the current MAX_SPREAD_USD and
what a looser one would admit.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config  # noqa: E402
from backtest import simulator as sim  # noqa: E402

ACTUAL = config.MAX_SPREAD_USD
for base in (0.30, 0.40, 0.50):
    print(f"base ${base:.2f}   (your stated 0.30-0.50 average):")
    for label, mult in (("normal", 1.0),
                        ("session", sim.SPREAD_SESSION_MULT),
                        ("rollover", sim.SPREAD_ROLLOVER_MULT)):
        s = base * mult
        verdict = f"ceiling ${ACTUAL:.2f}->{'TRADE' if s <= ACTUAL else 'BLOCK'}"
        print(f"   {label:<10} mult {mult:>4.1f}   spread ~${s:5.2f}   {verdict}")
    print("   note: a volatility term (SPREAD_ATR_COEF * normalised ATR) is omitted here")
    print()