import requests
import pandas as pd
import time
from datetime import datetime, timezone

# ── CONFIGURATION ────────────────────────────────────────────────────────────
API_KEY    = "PKQQ5FPEJJA3AOUFRL6EEPFPBB"
SECRET_KEY = "3Ky5MgW2avgqN43Hgux5Vw1GWPTdvpQpyJKEwL9sShEP"

# NOTE: Alpaca v1beta3 often requires "BTC/USD" (with the slash)
SYMBOL     = "BTC/USD" 
TIMEFRAME  = "5Min"
ATR_LEN    = 10
MULT       = 1.5
RISK_PCT   = 0.10  
POLL_SECS  = 30

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
    params = {
        "symbols": SYMBOL,
        "timeframe": TIMEFRAME,
        "limit": limit,
        "sort": "desc"
    }
    try:
        r = requests.get(f"{DATA_URL}/v1beta3/crypto/us/bars", params=params, headers=HEADERS)
        if r.status_code != 200:
            print(f"  [DEBUG] Status: {r.status_code} | Response: {r.text}")
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
    if df.empty: return df
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

def get_account():
    r = requests.get(ACCOUNT_URL, headers=HEADERS)
    return r.json()

def place_order(side, qty):
    qty = round(float(qty), 4) # Round for BTC
    if qty <= 0: return
    data = {"symbol": SYMBOL, "qty": str(qty), "side": side, "type": "market", "time_in_force": "gtc"}
    r = requests.post(ORDERS_URL, json=data, headers=HEADERS)
    print(f"  [ORDER] {side.upper()} {qty} BTC | Status: {r.status_code} | Res: {r.text}")

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def main():
    print("=== BOT STARTING: HEIKIN ASHI + SUPERTREND ===")
    last_candle_time = None
    while True:
        try:
            raw_df = get_candles(limit=300)
            if raw_df.empty:
                print("  [WAIT] Data not found. Retrying...")
                time.sleep(POLL_SECS)
                continue

            df = apply_heikin_ashi(raw_df)
            latest_candle_time = df.index[-2]   

            if latest_candle_time != last_candle_time:
                last_candle_time = latest_candle_time
                closed = df.iloc[:-1].copy()
                _, direction = calc_supertrend(closed, ATR_LEN, MULT)
                prev_dir, curr_dir = direction.iloc[-2], direction.iloc[-1]
                price = raw_df["close"].iloc[-1]
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] CANDLE: {latest_candle_time}")
                print(f"  Price: ${price:,.2f} | Dir: {'▲ (BUY)' if curr_dir==-1 else '▼ (SELL)'}")

                if curr_dir == -1 and prev_dir == 1:
                    print("  >>> TREND FLIPPED BULLISH")
                    acc = get_account()
                    qty = (float(acc["cash"]) * RISK_PCT) / price
                    place_order("buy", qty)
                elif curr_dir == 1 and prev_dir == -1:
                    print("  >>> TREND FLIPPED BEARISH")
                    acc = get_account()
                    qty = (float(acc["cash"]) * RISK_PCT) / price
                    place_order("sell", qty)
            else:
                print(f"  [HEARTBEAT] {datetime.now().strftime('%H:%M:%S')} | Monitoring...")
        except Exception as e:
            print(f"  [ERROR] {e}")
        time.sleep(POLL_SECS)

if __name__ == "__main__":
    main()
