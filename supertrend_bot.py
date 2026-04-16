import requests
import pandas as pd
import time
from datetime import datetime, timezone

# ── CONFIGURATION ────────────────────────────────────────────────────────────
# Replace these with your actual Alpaca Paper keys
API_KEY    = "YOUR_ALPACA_KEY"
SECRET_KEY = "YOUR_ALPACA_SECRET"

SYMBOL     = "BTC/USD"
TIMEFRAME  = "5Min"
ATR_LEN    = 10
MULT       = 1.5
RISK_PCT   = 0.10  # Uses 10% of your cash per trade
POLL_SECS  = 30

# URLs for Alpaca API
BASE_URL    = "https://paper-api.alpaca.markets"
DATA_URL    = "https://data.alpaca.markets"
ACCOUNT_URL = f"{BASE_URL}/v2/account"
ORDERS_URL  = f"{BASE_URL}/v2/orders"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY
}

# ── DATA & MATH FUNCTIONS ─────────────────────────────────────────────────────

def get_candles(limit=300):
    """Fetches the latest candles from Alpaca with sorting fixed"""
    params = {
        "symbols": SYMBOL,
        "timeframe": TIMEFRAME,
        "limit": limit,
        "sort": "desc"
    }
    try:
        r = requests.get(f"{DATA_URL}/v1beta3/crypto/us/bars", params=params, headers=HEADERS)
        if r.status_code != 200:
            return pd.DataFrame()
        
        data = r.json()
        bars = data.get("bars", {}).get(SYMBOL, [])
        if not bars: return pd.DataFrame()

        df = pd.DataFrame(bars)
        df["t"] = pd.to_datetime(df["t"])
        df = df.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"})
        # Set index and sort oldest to newest for math
        return df.set_index("t").sort_index()
    except Exception as e:
        print(f"  [FETCH ERROR] {e}")
        return pd.DataFrame()

def apply_heikin_ashi(df):
    """Converts standard candles to smoothed Heikin Ashi candles"""
    if df.empty: return df
    df_ha = df.copy()
    
    # Close = (Open + High + Low + Close) / 4
    df_ha['close'] = (df['open'] + df['high'] + df['low'] + df['close']) / 4

    # Open = (Prev_HA_Open + Prev_HA_Close) / 2
    for i in range(1, len(df)):
        df_ha.iloc[i, df_ha.columns.get_loc('open')] = (df_ha.iloc[i-1]['open'] + df_ha.iloc[i-1]['close']) / 2

    # High/Low adjustments
    df_ha['high'] = df_ha[['high', 'open', 'close']].max(axis=1)
    df_ha['low'] = df_ha[['low', 'open', 'close']].min(axis=1)
    return df_ha

def calc_supertrend(df, atr_len, mult):
    """Calculates the Supertrend direction (-1 for Bullish, 1 for Bearish)"""
    high, low, close = df["high"], df["low"], df["close"]
    
    # ATR calculation
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

def get_account():
    r = requests.get(ACCOUNT_URL, headers=HEADERS)
    return r.json()

def place_order(side, qty):
    # Round to 4 decimals for BTC safety
    qty = round(float(qty), 4)
    if qty <= 0:
        print(f"  [SKIPPED] Qty {qty} too small.")
        return
        
    data = {
        "symbol": SYMBOL,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "gtc"
    }
    
    r = requests.post(ORDERS_URL, json=data, headers=HEADERS)
    if r.status_code == 200:
        print(f"  [SUCCESS] {side.upper()} order for {qty} BTC placed!")
    else:
        print(f"  [FAILED] Order Error: {r.text}")

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("  BOT STARTING: HEIKIN ASHI + SUPERTREND")
    print("=" * 50)
    
    last_candle_time = None

    while True:
        try:
            # 1. Fetch data
            raw_df = get_candles(limit=300)
            if raw_df.empty:
                print("  [WAIT] Data not found. Retrying...")
                time.sleep(POLL_SECS)
                continue

            # 2. Smooth with Heikin Ashi
            df = apply_heikin_ashi(raw_df)
            
            # 3. Check for new candle close
            latest_candle_time = df.index[-2]   

            if latest_candle_time != last_candle_time:
                last_candle_time = latest_candle_time
                
                # Analyze finished candles
                closed = df.iloc[:-1].copy()
                _, direction = calc_supertrend(closed, ATR_LEN, MULT)
                
                prev_dir = direction.iloc[-2]
                curr_dir = direction.iloc[-1]
                
                now_str = datetime.now().strftime('%H:%M:%S')
                price = raw_df["close"].iloc[-1]
                
                print(f"\n[{now_str}] NEW CANDLE: {latest_candle_time}")
                print(f"  Price: ${price:,.2f} | Dir: {'▲ (BUY)' if curr_dir == -1 else '▼ (SELL)'}")

                # 4. Signal logic
                if curr_dir == -1 and prev_dir == 1:
                    print("  ►►► TRIGGER: Trend Flipped BULLISH")
                    acc = get_account()
                    qty = (float(acc["cash"]) * RISK_PCT) / price
                    place_order("buy", qty)
                    
                elif curr_dir == 1 and prev_dir == -1:
                    print("  ►►► TRIGGER: Trend Flipped BEARISH")
                    acc = get_account()
                    qty = (float(acc["cash"]) * RISK_PCT) / price
                    place_order("sell", qty)
                else:
                    print("  Trend remains consistent. No trade needed.")
            else:
                # Still on the same candle, just heartbeat
                print(f"  [HEARTBEAT] {datetime.now().strftime('%H:%M:%S')} | Monitoring...")

        except Exception as e:
            print(f"  [ERROR] {e}")
        
        time.sleep(POLL_SECS)

if __name__ == "__main__":
    main()
