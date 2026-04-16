"""
Supertrend Flip Bot — Alpaca Paper Trading
BTCUSD | 5-minute candles | 3% notional risk per trade
Logic: Always in market, flips long<->short on signal
"""

import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# ── CONFIG ────────────────────────────────────────────────────────────────────
API_KEY    = os.environ["ALPACA_API_KEY"]
API_SECRET = os.environ["ALPACA_API_SECRET"]
BASE_URL   = "https://paper-api.alpaca.markets"       # paper trading endpoint
DATA_URL   = "https://data.alpaca.markets"

SYMBOL     = "BTC/USD"
TIMEFRAME  = "5Min"
ATR_LEN    = 10
MULT       = 1.5
RISK_PCT   = 0.03    # 3% notional
POLL_SECS  = 30      # how often to check for new closed candle (seconds)

HEADERS = {
    "APCA-API-KEY-ID":     API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}

def apply_heikin_ashi(df: pd.DataFrame):
    df_ha = df.copy()
    
    # HA_Close is the average of the current bar
    df_ha['close'] = (df['open'] + df['high'] + df['low'] + df['close']) / 4

    # HA_Open is the midpoint of the previous HA candle
    # We have to loop here because each open depends on the one before it
    for i in range(1, len(df)):
        df_ha.iloc[i, df_ha.columns.get_loc('open')] = (df_ha.iloc[i-1]['open'] + df_ha.iloc[i-1]['close']) / 2

    # HA_High is the max of (High, HA_Open, HA_Close)
    df_ha['high'] = df_ha[['high', 'open', 'close']].max(axis=1)
    
    # HA_Low is the min of (Low, HA_Open, HA_Close)
    df_ha['low'] = df_ha[['low', 'open', 'close']].min(axis=1)
    
    return df_ha



# ── SUPERTREND ────────────────────────────────────────────────────────────────
def calc_supertrend(df: pd.DataFrame, atr_len: int, mult: float):
    # Extract data to series for easier handling
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    # ATR calculation
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/atr_len, adjust=False).mean()

    hl2 = (high + low) / 2
    upper_band = hl2 + mult * atr
    lower_band = hl2 - mult * atr

    # Initialize lists to avoid "SettingWithCopy" pandas crashes
    size = len(df)
    final_upper = [0.0] * size
    final_lower = [0.0] * size
    st_dir      = [1] * size
    supertrend  = [0.0] * size

    # Calculation loop
    for i in range(1, size):
        # Lower band logic: only moves up
        if lower_band.iloc[i] > final_lower[i-1] or close.iloc[i-1] < final_lower[i-1]:
            final_lower[i] = lower_band.iloc[i]
        else:
            final_lower[i] = final_lower[i-1]

        # Upper band logic: only moves down
        if upper_band.iloc[i] < final_upper[i-1] or close.iloc[i-1] > final_upper[i-1]:
            final_upper[i] = upper_band.iloc[i]
        else:
            final_upper[i] = final_upper[i-1]

        # Determine trend direction
        if close.iloc[i] > final_upper[i-1]:
            st_dir[i] = -1 # Bullish Flip
        elif close.iloc[i] < final_lower[i-1]:
            st_dir[i] = 1  # Bearish Flip
        else:
            st_dir[i] = st_dir[i-1]
            
        supertrend[i] = final_lower[i] if st_dir[i] == -1 else final_upper[i]

    # Convert back to pandas Series so the rest of your bot can read them
    return pd.Series(supertrend, index=df.index), pd.Series(st_dir, index=df.index)



# ── ALPACA HELPERS ────────────────────────────────────────────────────────────
def get_account():
    r = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS)
    r.raise_for_status()
    return r.json()

def get_position():
    """Returns current BTC position qty (positive=long, negative=short, 0=flat)"""
    r = requests.get(f"{BASE_URL}/v2/positions/BTCUSD", headers=HEADERS)
    if r.status_code == 404:
        return 0.0
    r.raise_for_status()
    return float(r.json()["qty_available"])

def close_position():
    r = requests.delete(f"{BASE_URL}/v2/positions/BTCUSD", headers=HEADERS)
    if r.status_code in (200, 207):
        print(f"  [CLOSE] Position closed")
    elif r.status_code == 404:
        print(f"  [CLOSE] No position to close")
    else:
        print(f"  [CLOSE] Unexpected status {r.status_code}: {r.text}")

def place_order(side, qty):
    # Alpaca Crypto usually requires rounding to 4-6 decimals
    qty = round(float(qty), 4)
    
    if qty <= 0:
        print(f"  [SKIPPED] Quantity {qty} is too low to trade.")
        return

    print(f"  >>> Sending {side.upper()} order for {qty} BTC...")
    
    data = {
        "symbol": SYMBOL,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "gtc"
    }
    
    r = requests.post(ORDERS_URL, json=data, headers=HEADERS)
    
    if r.status_code == 200:
        print(f"  [SUCCESS] {side.upper()} order placed successfully!")
    else:
        print(f"  [FAILED] Alpaca error: {r.text}")


def get_candles(limit=100):
    # This tells Alpaca: "Give me the most recent bars available"
    params = {
        "symbols":   SYMBOL,    # "BTC/USD"
        "timeframe": TIMEFRAME, # "5Min"
        "limit":     limit,
        "sort":      "desc"     # IMPORTANT: Get newest candles first
    }
    
    r = requests.get(
        f"{DATA_URL}/v1beta3/crypto/us/bars",
        params=params,
        headers=HEADERS,
    )
    
    if r.status_code != 200:
        print(f"  [DEBUG] Status: {r.status_code} | Response: {r.text}")
        r.raise_for_status()

    data = r.json()
    bars = data.get("bars", {}).get(SYMBOL, [])
    
    if not bars:
        print(f"  [WARN] No bars returned for {SYMBOL}")
        return pd.DataFrame()

    df = pd.DataFrame(bars)
    df["t"] = pd.to_datetime(df["t"])
    df = df.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"})
    
    # Sort it so the bot reads it from oldest to newest for the Supertrend
    df = df.set_index("t").sort_index()
    return df




# ── MAIN LOOP ─────────────────────────────────────────────────────────────────
# ── MAIN LOOP ─────────────────────────────────────────────────────────────────
# ── MAIN LOOP ─────────────────────────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  SUPERTREND FLIP BOT — Heikin Ashi + Alpaca Paper")
    print("=" * 55)

    last_candle_time = None
    current_side     = None

    while True:
        try:
            # 1. Get raw candles
            raw_df = get_candles(limit=100)
            if raw_df.empty:
                print("  [WARN] No data. Waiting...")
                time.sleep(POLL_SECS)
                continue

            # 2. Convert to Heikin Ashi
            df = apply_heikin_ashi(raw_df)

            # 3. Check if we have a new 5m candle
            latest_candle_time = df.index[-2]   
            if latest_candle_time == last_candle_time:
                print(f"  [HEARTBEAT] {datetime.now(timezone.utc).strftime('%H:%M:%S')} | Waiting for new candle...")
                time.sleep(POLL_SECS)
                continue

            last_candle_time = latest_candle_time

            # 4. Calc supertrend logic on HA candles
            closed = df.iloc[:-1].copy()
            _, direction = calc_supertrend(closed, ATR_LEN, MULT)

            prev_dir = direction.iloc[-2]
            curr_dir = direction.iloc[-1]

            buy_signal  = curr_dir == -1 and prev_dir == 1   
            sell_signal = curr_dir ==  1 and prev_dir == -1  

            now   = datetime.now(timezone.utc).strftime("%H:%M:%S")
            # We use raw_df for the price print so you see the REAL market price
            price = raw_df["close"].iloc[-1]
            
            print(f"\n[{now}] NEW HA CANDLE: {latest_candle_time} | Market Price: ${price:,.2f} | Dir: {'▲' if curr_dir==-1 else '▼'}")

            if buy_signal:
                print("  ► HA BUY SIGNAL")
                account = get_account()
                qty = (float(account["cash"]) * RISK_PCT) / price
                place_order("buy", qty)
                current_side = "long"

            elif sell_signal:
                print("  ► HA SELL SIGNAL")
                account = get_account()
                qty = (float(account["cash"]) * RISK_PCT) / price
                place_order("sell", qty)
                current_side = "short"
            else:
                print("  HA Trend consistent. Monitoring...")

        except Exception as e:
            print(f"  [ERROR] {e}")
        
        print(f"  [WAIT] Sleeping {POLL_SECS}s...")
        time.sleep(POLL_SECS)

if __name__ == "__main__":
    main()



if __name__ == "__main__":
    main()
