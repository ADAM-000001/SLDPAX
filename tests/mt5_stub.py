"""MetaTrader5 stub so SLDPAX can be exercised offline, with no terminal."""

import sys
import time
import types

FAILS = []


def obj(**kw):
    return types.SimpleNamespace(**kw)


STATE = {
    "account": obj(balance=10000.0, equity=10000.0, margin_free=9800.0, margin=200.0,
                   login=12345, company="TEST-FUNDED", margin_mode=2,
                   trade_allowed=1, trade_expert=1),
    "symbol": obj(filling_mode=1, visible=True, digits=2, point=0.01,
                  trade_contract_size=100.0, volume_min=0.01, volume_max=10.0,
                  volume_step=0.01, trade_stops_level=10, trade_mode=4),
    "tick": obj(bid=2400.50, ask=2400.70),
    "bars": {},
    "positions": [],
    "deals": [],
    "sent": [],
    "checks": [],
    "next_position_id": 5000,
    "next_order": 9000,
    "history_calls": 0,
    "order_check_retcode": 10009,
    "order_send_retcode": 10009,
}

_TF_BY_ID = {5: "M5", 15: "M15", 30: "M30", 16385: "H1"}
_ID_BY_TF = {v: k for k, v in _TF_BY_ID.items()}

mt5 = types.ModuleType("MetaTrader5")
mt5.TIMEFRAME_M5, mt5.TIMEFRAME_M15, mt5.TIMEFRAME_M30, mt5.TIMEFRAME_H1 = 5, 15, 30, 16385
mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL = 0, 1
mt5.TRADE_ACTION_DEAL, mt5.ORDER_TIME_GTC = 1, 0
mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN = 0, 1, 2
mt5.SYMBOL_TRADE_MODE_FULL = 4
mt5.ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
mt5.ACCOUNT_MARGIN_MODE_EXCHANGE = 1
mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
mt5.DEAL_ENTRY_IN, mt5.DEAL_ENTRY_OUT = 0, 1
mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL = 10009, 10010
mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_CHANGED = 10004, 10006
mt5.TRADE_RETCODE_PRICE_OFF, mt5.TRADE_RETCODE_INVALID_STOPS = 10007, 10015
mt5.TRADE_RETCODE_INVALID_VOLUME = 10014


def initialize(*a, **k):
    return True


def shutdown(*a, **k):
    return True


def terminal_info(*a, **k):
    return obj(connected=True)


def last_error():
    return (0, "no error")


def account_info():
    return STATE["account"]


def symbol_info(sym):
    return STATE["symbol"]


def symbol_info_tick(sym):
    return STATE["tick"]


def symbol_select(sym, vis=True):
    return True


def copy_rates_from_pos(symbol, tf, pos, count):
    bars = STATE["bars"].get(_TF_BY_ID.get(tf))
    if not bars:
        return None
    return bars[-count:]


def positions_get(symbol=None):
    # Real MT5 returns an empty tuple when the call succeeds but matches nothing,
    # and None only when the call itself fails (terminal down / bad request).
    # Conflating the two is exactly the bug this stub exists to catch.
    if STATE["positions"] is None:
        return None
    return tuple(STATE["positions"])


def history_deals_get(frm, to):
    STATE["history_calls"] += 1
    return list(STATE["deals"])


def order_check(req):
    STATE["checks"].append(req)
    return obj(retcode=STATE["order_check_retcode"], margin=200.0, comment="OK")


def order_send(req):
    STATE["sent"].append(req)
    STATE["next_position_id"] += 1
    STATE["next_order"] += 1
    # Close-by-position (emergency cutoff / hedging-account close semantics):
    # a DEAL carrying `position` removes that position from the open book.
    pid = req.get("position")
    if pid is not None:
        STATE["positions"] = [p for p in STATE["positions"]
                              if getattr(p, "identifier", None) != pid]
    return obj(retcode=STATE["order_send_retcode"], order=STATE["next_order"],
               position_id=STATE["next_position_id"], comment="done")


for _name in (
    "initialize shutdown terminal_info last_error account_info symbol_info "
    "symbol_info_tick symbol_select copy_rates_from_pos positions_get "
    "history_deals_get order_check order_send"
).split():
    setattr(mt5, _name, locals()[_name])

sys.modules["MetaTrader5"] = mt5


def reset_orders():
    STATE["sent"].clear()
    STATE["checks"].clear()
    STATE["order_check_retcode"] = mt5.TRADE_RETCODE_DONE
    STATE["order_send_retcode"] = mt5.TRADE_RETCODE_DONE
    STATE["positions"] = []
    STATE["deals"] = []


def fresh_account(equity=10000.0, margin_mode=2):
    STATE["account"] = obj(balance=equity, equity=equity, margin_free=equity - 200.0,
                           margin=200.0, login=12345, company="TEST-FUNDED",
                           margin_mode=margin_mode, trade_allowed=1, trade_expert=1)


def bar(t, o, h, l, c, v=500):
    return {"time": int(t), "open": o, "high": h, "low": l, "close": c, "tick_volume": v}


def synthetic(tf_min, n, end_price, base=2400.0, spread=1.0, seed=11, end_at_now=True):
    """
    Deterministic synthetic OHLCV ending with a FORMING bar at `now`.
    Bar -2 (the signal bar) is pinned to `end_price` so assertions are stable.
    """
    rnd = seed
    step = tf_min * 60
    closes = []
    level = base
    for _ in range(n + 1):
        rnd = (1103515245 * rnd + 12345) % (2 ** 31)
        level += (rnd / (2 ** 31) - 0.5) * 3.0
        closes.append(level)
    closes[-2] = end_price
    last_open = int(time.time()) - (0 if end_at_now else step)
    first = last_open - (n - 1) * step
    out = []
    for i in range(n):
        c = closes[i]
        o = c - 0.4
        h, l = max(o, c) + spread, min(o, c) - spread
        if i == n - 2:
            o, h, l = end_price - 0.5, end_price + spread, end_price - spread
        out.append(bar(first + i * step, o, h, l, c))
    return out


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}   {extra}")
        FAILS.append(name)
    return bool(cond)


def summary():
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
        return 1
    print("ALL CHECKS PASSED")
    return 0
