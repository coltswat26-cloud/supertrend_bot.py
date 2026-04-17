import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# ── CONFIG ───────────────────────────────────────────────────────────────────
API_KEY    = os.environ.get("ALPACA_API_KEY")
API_SECRET = os.environ.get("ALPACA_API_SECRET")

SYMBOL     = "BTC/USD"
TIMEFRAME  = "5Min"
ATR_LEN    = 10
MULT       = 1.5
BUY_AMOUNT_USD = 10000  # Set how many dollars you want to spend per buy
POLL_SECS  = 30

BASE_URL     = "https://paper-api.alpaca.markets"
DATA_URL     = "https://data.alpaca.markets/v1beta3/crypto/us"
ACCOUNT_URL  = f"{BASE_URL}/v2/account"
ORDERS_URL   = f"{BASE_URL}/v2/orders"
POSITIONS_URL = f"{BASE_URL}/v2/positions"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}

# ── DATA & MATH FUNCTIONS ─────────────────────────────────────────────────────

def get_candles(limit=300):
    params = {"symbols": SYMBOL, "timeframe": TIMEFRAME, "limit": limit, "sort": "desc"}
    try:
        r = requests.get(f"{DATA_URL}/bars", params=params, headers=HEADERS)
        if r.status_code != 200:
            return pd.DataFrame()
        data = r.json()
        bars = data.get("bars", {}).get(SYMBOL, [])
        if not bars: return pd.DataFrame()
        df = pd.DataFrame(bars)
        df["t"] = pd.to_datetime(df["t"])
        df = df.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"})
        return df.set_index("t").sort_index()
    except Exception as e:
        print(f"  [FETCH ERROR] {e}")
        return pd.DataFrame()

def apply_heikin_ashi(df):
    if len(df) < 2: return df
    df_ha = df.copy()
    df_ha['close'] = (df['open'] + df['high'] + df['low'] + df['close']) / 4
    for i in range(1, len(df)):
        df_ha.iloc[i, df_ha.columns.get_loc('open')] = (df_ha.iloc[i-1]['open'] + df_ha.iloc[i-1]['close']) / 2
    df_ha['high'] = df_ha[['high', 'open', 'close']].max(axis=1)
    df_ha['low'] = df_ha[['low', 'open', 'close']].min(axis=1)
    return df_ha

def calc_supertrend(df, atr_len, mult):
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/atr_len, adjust=False).mean()
    hl2 = (high + low) / 2
    upper_band, lower_band = hl2 + mult * atr, hl2 - mult * atr
    size = len(df)
    f_up, f_lo, st_dir, st_val = [0.0]*size, [0.0]*size, [1]*size, [0.0]*size
    for i in range(1, size):
        f_lo[i] = lower_band.iloc[i] if lower_band.iloc[i] > f_lo[i-1] or close.iloc[i-1] < f_lo[i-1] else f_lo[i-1]
        f_up[i] = upper_band.iloc[i] if upper_band.iloc[i] < f_up[i-1] or close.iloc[i-1] > f_up[i-1] else f_up[i-1]
        if close.iloc[i] > f_up[i-1]: st_dir[i] = -1
        elif close.iloc[i] < f_lo[i-1]: st_dir[i] = 1
        else: st_dir[i] = st_dir[i-1]
        st_val[i] = f_lo[i] if st_dir[i] == -1 else f_up[i]
    return pd.Series(st_val, index=df.index), pd.Series(st_dir, index=df.index)

# ── TRADING FUNCTIONS ─────────────────────────────────────────────────────────

def get_btc_position():
    """Checks exactly how much BTC we own right now"""
    r = requests.get(f"{POSITIONS_URL}/BTCUSD", headers=HEADERS)
    if r.status_code == 200:
        return float(r.json().get("qty", 0))
    return 0.0

def place_buy_order():
    """Buys using a specific dollar amount (Notional)"""
    data = {
        "symbol": SYMBOL,
        "notional": str(BUY_AMOUNT_USD),
        "side": "buy",
        "type": "market",
        "time_in_force": "gtc"
    }
    r = requests.post(ORDERS_URL, json=data, headers=HEADERS)
    print(f"  [BUY] Spent ${BUY_AMOUNT_USD} | Status: {r.status_code}")

def place_sell_order(qty):
    """Sells the exact amount we own"""
    if qty <= 0:
        print("  [SKIP] Nothing to sell.")
        return
    data = {
        "symbol": SYMBOL,
        "qty": str(qty),
        "side": "sell",
        "type": "market",
        "time_in_force": "gtc"
    }
    r = requests.post(ORDERS_URL, json=data, headers=HEADERS)
    print(f"  [SELL] Sold {qty} BTC | Status: {r.status_code}")

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def main():
    print("=" * 40)
    print("  BOT STARTING: POSITION-AWARE TRADER")
    print("=" * 40)
    last_candle_time = None

    while True:
        try:
            raw_df = get_candles(limit=300)
            if raw_df.empty or len(raw_df) < 5:
                print("  [WAIT] Fetching data...")
                time.sleep(POLL_SECS)
                continue

            df = apply_heikin_ashi(raw_df)
            latest_candle_time = df.index[-2]   

            if latest_candle_time != last_candle_time:
                last_candle_time = latest_candle_time
                closed = df.iloc[:-1].copy()
                _, direction = calc_supertrend(closed, ATR_LEN, MULT)
                prev_dir, curr_dir = direction.iloc[-2], direction.iloc[-1]
                
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] NEW CANDLE: {latest_candle_time}")
                
                # Signal Logic
                if curr_dir == -1 and prev_dir == 1:
                    print("  >>> TREND BULLISH: Buying BTC...")
                    place_buy_order()
                elif curr_dir == 1 and prev_dir == -1:
                    print("  >>> TREND BEARISH: Closing Position...")
                    current_qty = get_btc_position()
                    place_sell_order(current_qty)
                else:
                    print("  Trend steady. No trade.")
            else:
                print(f"  [HEARTBEAT] {datetime.now().strftime('%H:%M:%S')} | Monitoring...")

        except Exception as e:
            print(f"  [ERROR] {e}")
        
        time.sleep(POLL_SECS)

if __name__ == "__main__":
    main()
