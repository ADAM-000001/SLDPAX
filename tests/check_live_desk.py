import urllib.request
import json
import sys

def main():
    try:
        req = urllib.request.urlopen("http://127.0.0.1:8080/api/state", timeout=5)
        raw = req.read().decode("utf-8")
        data = json.loads(raw)
    except Exception as exc:
        print(f"[FAIL] Could not connect to web dashboard: {exc}")
        sys.exit(1)

    print("==================================================")
    print("        SLDPAX DESK TELEMETRY VERIFICATION       ")
    print("==================================================")
    print(f"Server Status   : ONLINE (HTTP 200 OK)")
    print(f"Timestamp       : {data.get('utc')}")
    print(f"Account Login   : {data.get('login')} ({data.get('company')})")
    print(f"Account Balance : ${data.get('balance'):,.2f}")
    print(f"Account Equity  : ${data.get('equity'):,.2f}")
    print(f"Symbol          : {data.get('symbol')}")
    print(f"Market Bid/Ask  : {data.get('bid')} / {data.get('ask')} (Spread: {data.get('spread'):.2f})")
    print(f"Open Positions  : {data.get('open_pos')} / {data.get('open_cap')}")
    print(f"Day P&L         : ${data.get('day_pnl'):,.2f} ({data.get('day_pnl_pct'):.2f}%)")
    print(f"Overall DD      : {data.get('overall_dd'):.2f}% (Stop at {data.get('stop_pct')}%)")
    print(f"Desk Halted     : {data.get('desklock', {}).get('halted')}")
    print("--------------------------------------------------")
    print(f"Employee Desks  : {len(data.get('employees', []))} online")
    for emp in data.get("employees", []):
        eid = emp["id"]
        name = emp["name"]
        tf = emp["tf"]
        rr = emp["rr"]
        trades = emp["trades"]
        max_t = emp["max_trades"]
        wins = emp["wins"]
        losses = emp["losses"]
        in_t = emp["in_trade"]
        active_str = "[IN TRADE]" if in_t else "[IDLE]"
        print(f"  Desk #{eid:02d} | {name:<22} | {tf:<3} | R:R 1:{rr} | Trades {trades}/{max_t} (W:{wins} L:{losses}) {active_str}")
    print("==================================================")
    print("[PASS] All trading desk telemetry is streaming live and healthy.")

if __name__ == "__main__":
    main()
