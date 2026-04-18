import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── CONFIG ───────────────────────────────────────────────────────────────────
API_KEY    = os.environ.get("ALPACA_API_KEY")
API_SECRET = os.environ.get("ALPACA_API_SECRET")

SYMBOL     = "BTC/USD"
TIMEFRAME  = "1Min"      
ATR_LEN    = 10
MULT       = 1.5
BUY_AMOUNT_USD = 15000   # Your requested base entry
POLL_SECS  = 15          

# ── RISK SETTINGS ─────────────────────────────────────────────────────────────
TAKE_PROFIT_PCT      = 0.02    
TRAILING_STOP_AMOUNT = 150.00  

# ── ENDPOINTS ─────────────────────────────────────────────────────────────────
BASE_URL      = "https://paper-api.alpaca.markets"
DATA_URL      = "https://data.alpaca.markets/v1beta3/crypto/us"
ORDERS_URL    = f"{BASE_URL}/v2/orders"
POSITIONS_URL = f"{BASE_URL}/v2/positions"

HEADERS = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET}

entry_price = 0.0
peak_price  = 0.0

# ── RAILWAY STABILITY SERVER ─────────────────────────────────────────────────
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Active")

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(('0.0.0.0', port), HealthCheckHandler).serve_forever()

# ── CORE FUNCTIONS ───────────────────────────────────────────────────────────

def get_candles(limit=100):
    params = {"symbols": SYMBOL, "timeframe": TIMEFRAME, "limit": limit, "sort": "desc"}
    try:
        r = requests.get(f"{DATA_URL}/bars", params=params, headers=HEADERS, timeout=10)
        if r.status_code != 200: return pd.DataFrame()
        bars = r.json().get("bars", {}).get(SYMBOL, [])
        if not bars: return pd.DataFrame()
        df = pd.DataFrame(bars)
        df["t"] = pd.to_datetime(df["t"])
        df = df.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"})
        return df.set_index("t").sort_index()
    except: return pd.DataFrame()

def apply_heikin_ashi(df):
    df_ha = df.copy()
    df_ha['close'] = (df['open'] + df['high'] + df['low'] + df['close']) / 4
    for i in range(1, len(df)):
        df_ha.iloc[i, df_ha.columns.get_loc('open')] = (df_ha.iloc[i-1]['open'] + df_ha.iloc[i-1]['close']) / 2
    return df_ha

def calc_supertrend(df, atr_len, mult):
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/atr_len, adjust=False).mean()
    hl2 = (high + low) / 2
    upper_band, lower_band = hl2 + mult * atr, hl2 - mult * atr
    size = len(df)
    f_up, f_lo, st_dir = [0.0]*size, [0.0]*size, [1]*size
    for i in range(1, size):
        f_lo[i] = lower_band.iloc[i] if lower_band.iloc[i] > f_lo[i-1] or close.iloc[i-1] < f_lo[i-1] else f_lo[i-1]
        f_up[i] = upper_band.iloc[i] if upper_band.iloc[i] < f_up[i-1] or close.iloc[i-1] > f_up[i-1] else f_up[i-1]
        st_dir[i] = -1 if close.iloc[i] > f_up[i-1] else (1 if close.iloc[i] < f_lo[i-1] else st_dir[i-1])
    return pd.Series(st_dir, index=df.index)

def get_btc_position():
    try:
        r = requests.get(f"{POSITIONS_URL}/BTCUSD", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            pos = r.json()
            return float(pos.get("qty", 0)), float(pos.get("avg_entry_price", 0))
    except: pass
    return 0.0, 0.0

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def trade_loop():
    global entry_price, peak_price
    print("\n" + "="*40 + "\n  DETAILED SCALPER STARTING\n" + "="*40)
    last_candle_time = None

    while True:
        try:
            raw_df = get_candles(limit=100)
            if raw_df.empty:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [WAIT] API Data pending...")
                time.sleep(POLL_SECS)
                continue

            df_ha = apply_heikin_ashi(raw_df)
            current_price = raw_df.iloc[-1]['close'] 
            ha_open = df_ha.iloc[-1]['open']
            ha_close = df_ha.iloc[-1]['close']
            latest_candle_time = df_ha.index[-2]   

            qty_owned, avg_entry = get_btc_position()
            
            # --- DETAILED HEARTBEAT LOG ---
            direction_str = "BULLISH" if ha_close > ha_open else "BEARISH"
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Price: ${current_price:,.2f} | HA: {direction_str} (O:${ha_open:,.0f} C:${ha_close:,.0f})")

            if qty_owned > 0:
                if peak_price == 0 or avg_entry != entry_price:
                    entry_price, peak_price = avg_entry, current_price
                peak_price = max(peak_price, current_price)
                
                # Risk Check
                if current_price >= (entry_price * (1 + TAKE_PROFIT_PCT)):
                    requests.post(ORDERS_URL, json={"symbol": SYMBOL, "qty": str(qty_owned), "side": "sell", "type": "market", "time_in_force": "gtc"}, headers=HEADERS)
                    print(f"  >>> [TP EXIT] Sold at 2% Profit")
                    peak_price = 0
                elif current_price <= (peak_price - TRAILING_STOP_AMOUNT):
                    requests.post(ORDERS_URL, json={"symbol": SYMBOL, "qty": str(qty_owned), "side": "sell", "type": "market", "time_in_force": "gtc"}, headers=HEADERS)
                    print(f"  >>> [TS EXIT] Sold at -${TRAILING_STOP_AMOUNT} from peak")
                    peak_price = 0

            # Signal Check on Candle Close
            if latest_candle_time != last_candle_time:
                last_candle_time = latest_candle_time
                st_direction = calc_supertrend(df_ha.iloc[:-1], ATR_LEN, MULT)
                prev_dir, curr_dir = st_direction.iloc[-2], st_direction.iloc[-1]
                
                if curr_dir == -1 and prev_dir == 1 and qty_owned == 0:
                    requests.post(ORDERS_URL, json={"symbol": SYMBOL, "notional": str(BUY_AMOUNT_USD), "side": "buy", "type": "market", "time_in_force": "gtc"}, headers=HEADERS)
                    print(f"  >>> [SIGNAL BUY] Trend flipped BULLISH @ ${current_price:,.2f}")
                elif curr_dir == 1 and prev_dir == -1 and qty_owned > 0:
                    requests.post(ORDERS_URL, json={"symbol": SYMBOL, "qty": str(qty_owned), "side": "sell", "type": "market", "time_in_force": "gtc"}, headers=HEADERS)
                    print(f"  >>> [SIGNAL SELL] Trend flipped BEARISH")
                    peak_price = 0
            
        except Exception as e: print(f"  [ERROR] {e}")
        time.sleep(POLL_SECS)

if __name__ == "__main__":
    Thread(target=run_health_server, daemon=True).start()
    trade_loop()
