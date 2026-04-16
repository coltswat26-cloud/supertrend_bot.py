“””
Supertrend Flip Bot — Alpaca Paper Trading
BTC/USD | 5-minute candles | 3% notional risk per trade
Logic: Always in market, flips long<->short on signal
“””

import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# ── CONFIG ────────────────────────────────────────────────────────────────────

API_KEY    = os.environ[“ALPACA_API_KEY”]
API_SECRET = os.environ[“ALPACA_API_SECRET”]
BASE_URL   = “https://paper-api.alpaca.markets”       # paper trading endpoint
DATA_URL   = “https://data.alpaca.markets”

SYMBOL     = “BTC/USD”
TIMEFRAME  = “5Min”
ATR_LEN    = 10
MULT       = 1.5
RISK_PCT   = 0.03    # 3% notional
POLL_SECS  = 30      # how often to check for new closed candle (seconds)

HEADERS = {
“APCA-API-KEY-ID”:     API_KEY,
“APCA-API-SECRET-KEY”: API_SECRET,
}

# ── SUPERTREND ────────────────────────────────────────────────────────────────

def calc_supertrend(df: pd.DataFrame, atr_len: int, mult: float):
high  = df[“high”]
low   = df[“low”]
close = df[“close”]

```
# ATR
tr = pd.concat([
    high - low,
    (high - close.shift()).abs(),
    (low  - close.shift()).abs(),
], axis=1).max(axis=1)
atr = tr.ewm(alpha=1/atr_len, adjust=False).mean()

hl2 = (high + low) / 2
upper = hl2 + mult * atr
lower = hl2 - mult * atr

supertrend = pd.Series(index=df.index, dtype=float)
direction  = pd.Series(index=df.index, dtype=int)

for i in range(1, len(df)):
    prev_upper = upper.iloc[i-1]
    prev_lower = lower.iloc[i-1]
    prev_close = close.iloc[i-1]

    # Lower band: only moves up, never down
    if lower.iloc[i] < prev_lower or prev_close < prev_lower:
        lower.iloc[i] = lower.iloc[i]
    else:
        lower.iloc[i] = prev_lower

    # Upper band: only moves down, never up
    if upper.iloc[i] > prev_upper or prev_close > prev_upper:
        upper.iloc[i] = upper.iloc[i]
    else:
        upper.iloc[i] = prev_upper

    # Direction
    if i == 1:
        direction.iloc[i] = 1
    elif supertrend.iloc[i-1] == prev_upper:
        direction.iloc[i] = -1 if close.iloc[i] > upper.iloc[i] else 1
    else:
        direction.iloc[i] =  1 if close.iloc[i] < lower.iloc[i] else -1

    supertrend.iloc[i] = lower.iloc[i] if direction.iloc[i] == -1 else upper.iloc[i]

return supertrend, direction
```

# ── ALPACA HELPERS ────────────────────────────────────────────────────────────

def get_account():
r = requests.get(f”{BASE_URL}/v2/account”, headers=HEADERS)
r.raise_for_status()
return r.json()

def get_position():
“”“Returns current BTC position qty (positive=long, negative=short, 0=flat)”””
r = requests.get(f”{BASE_URL}/v2/positions/BTCUSD”, headers=HEADERS)
if r.status_code == 404:
return 0.0
r.raise_for_status()
return float(r.json()[“qty_available”])

def close_position():
r = requests.delete(f”{BASE_URL}/v2/positions/BTCUSD”, headers=HEADERS)
if r.status_code in (200, 207):
print(f”  [CLOSE] Position closed”)
elif r.status_code == 404:
print(f”  [CLOSE] No position to close”)
else:
print(f”  [CLOSE] Unexpected status {r.status_code}: {r.text}”)

def place_order(side: str, qty: float):
payload = {
“symbol”:        “BTCUSD”,
“qty”:           str(round(qty, 6)),
“side”:          side,           # “buy” or “sell”
“type”:          “market”,
“time_in_force”: “gtc”,
}
r = requests.post(f”{BASE_URL}/v2/orders”, json=payload, headers=HEADERS)
r.raise_for_status()
order = r.json()
print(f”  [ORDER] {side.upper()} {qty:.6f} BTC — id: {order[‘id’]}”)
return order

def get_candles(limit=50):
params = {
“timeframe”: TIMEFRAME,
“limit”:     limit,
“feed”:      “iex”,
}
r = requests.get(
f”{DATA_URL}/v1beta3/crypto/us/bars”,
params={“symbols”: SYMBOL, **params},
headers=HEADERS,
)
r.raise_for_status()
bars = r.json()[“bars”].get(SYMBOL, [])
df = pd.DataFrame(bars)
df[“t”] = pd.to_datetime(df[“t”])
df = df.rename(columns={“o”:“open”,“h”:“high”,“l”:“low”,“c”:“close”,“v”:“volume”})
df = df.set_index(“t”).sort_index()
return df

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def main():
print(”=” * 55)
print(”  SUPERTREND FLIP BOT — Alpaca Paper | BTC/USD 5m”)
print(”=” * 55)

```
last_candle_time = None
current_side     = None   # "long" | "short" | None

while True:
    try:
        df = get_candles(limit=100)

        # Only act on a newly closed candle
        latest_candle_time = df.index[-2]   # -1 is still forming
        if latest_candle_time == last_candle_time:
            time.sleep(POLL_SECS)
            continue

        last_candle_time = latest_candle_time

        # Calc supertrend on closed candles (drop last forming bar)
        closed = df.iloc[:-1].copy()
        _, direction = calc_supertrend(closed, ATR_LEN, MULT)

        prev_dir = direction.iloc[-2]
        curr_dir = direction.iloc[-1]

        buy_signal  = curr_dir == -1 and prev_dir == 1   # flip up
        sell_signal = curr_dir ==  1 and prev_dir == -1  # flip down

        now   = datetime.now(timezone.utc).strftime("%H:%M:%S")
        price = closed["close"].iloc[-1]
        print(f"\n[{now}] Candle: {latest_candle_time} | Close: ${price:,.2f} | Dir: {'▲' if curr_dir==-1 else '▼'}")

        if not buy_signal and not sell_signal:
            print("  No signal.")
            time.sleep(POLL_SECS)
            continue

        # Position sizing — 3% notional
        account  = get_account()
        balance  = float(account["cash"])
        notional = balance * RISK_PCT
        qty      = notional / price
        print(f"  Balance: ${balance:,.2f} | Notional: ${notional:,.2f} | Qty: {qty:.6f} BTC")

        if buy_signal:
            print("  ► BUY SIGNAL")
            if current_side == "short":
                print("  Closing short...")
                close_position()
                time.sleep(1)
            place_order("buy", qty)
            current_side = "long"

        elif sell_signal:
            print("  ► SELL SIGNAL")
            if current_side == "long":
                print("  Closing long...")
                close_position()
                time.sleep(1)
            place_order("sell", qty)
            current_side = "short"

    except KeyboardInterrupt:
        print("\nBot stopped.")
        break
    except Exception as e:
        print(f"  [ERROR] {e}")

    time.sleep(POLL_SECS)
```

if **name** == “**main**”:
main()
