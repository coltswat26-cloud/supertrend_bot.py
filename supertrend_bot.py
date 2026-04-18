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
BUY_AMOUNT_USD = 15000   # Updated to $15,000
POLL_SECS  = 15          # Slightly slower to prevent rate limiting

# ── RISK SETTINGS ─────────────────────────────────────────────────────────────
TAKE_PROFIT_PCT      = 0.02    # 2% Target
TRAILING_STOP_AMOUNT = 150.00  # $150 Dollar-based drop from peak

# ── ENDPOINTS ─────────────────────────────────────────────────────────────────
BASE_URL      = "https://paper-api.alpaca.markets"
DATA_URL      = "https://data.alpaca.markets/v1beta3/crypto/us"
ORDERS_URL    = f"{BASE_URL}/v2/orders"
POSITIONS_URL = f"{BASE_URL}/v2/positions"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}

# Tracking variables
entry_price = 0.0
peak_price  = 0.0

# ── RAILWAY STABILITY: MINIMAL WEB SERVER ────────────────────────────────────
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive")

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

# ── DATA & MATH FUNCTIONS ─────────────────────────────────────────────────────

def get_candles(limit=200):
    params = {"symbols": SYMBOL, "timeframe": TIMEFRAME, "limit": limit, "sort": "desc"}
    try:
        # Added 10s timeout to prevent hanging
        r = requests.get(f"{DATA_URL}/bars", params=params, headers=HEADERS, timeout=10)
        if r.status_code != 200: return pd.DataFrame()
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
    try:
        r = requests.get(f"{POSITIONS_URL}/BTCUSD", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            pos = r.json()
            return float(pos.get("qty", 0)), float(pos.get("avg_entry_price", 0))
    except:
        pass
    return 0.0, 0.0

def place_buy_order():
    data = {"symbol": SYMBOL, "notional": str(BUY_AMOUNT_USD), "side": "buy", "type": "market", "time_in_force": "gtc"}
    requests.post(ORDERS_URL, json=data, headers=HEADERS, timeout=10)
    print(f"  [BUY] Spent ${BUY_AMOUNT_USD}")

def place_sell_order(qty, reason="Signal"):
    if qty <= 0: return
    data = {"symbol": SYMBOL, "qty": str(qty), "side": "sell", "type": "market", "time_in_force": "gtc"}
    requests.post(ORDERS_URL, json=data, headers=HEADERS, timeout=10)
    print(f"  [SELL] {reason} | Qty: {qty}")

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────

def trade_loop():
    global entry_price, peak_price
    print("  >>> TRADING LOOP STARTED")
    last_candle_time = None

    while True:
        try:
            raw_df = get_candles(limit=100)
            if raw_df.empty or len(raw_df) < 5:
                time.sleep(POLL_SECS)
                continue

            df = apply_heikin_ashi(raw_df)
            current_price = raw_df.iloc[-1]['close'] 
            latest_candle_time = df.index[-2]   

            qty_owned, avg_entry = get_btc_position()
            
            if qty_owned > 0:
                if peak_price == 0 or avg_entry != entry_price:
                    entry_price = avg_entry
                    peak_price = current_price
                
                peak_price = max(peak_price, current_price)
                
                # Risk Exits
                if current_price >= (entry_price * (1 + TAKE_PROFIT_PCT)):
                    place_sell_order(qty_owned, reason="TAKE PROFIT HIT")
                    peak_price = 0
                    continue

                if current_price <= (peak_price - TRAILING_STOP_AMOUNT):
                    place_sell_order(qty_owned, reason=f"TRAILING STOP (${TRAILING_STOP_AMOUNT})")
                    peak_price = 0
                    continue

            # Signal Check
            if latest_candle_time != last_candle_time:
                last_candle_time = latest_candle_time
                closed = df.iloc[:-1].copy()
                _, direction = calc_supertrend(closed, ATR_LEN, MULT)
                prev_dir, curr_dir = direction.iloc[-2], direction.iloc[-1]
                
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Price: ${current_price:,.2f} | Dir: {'UP' if curr_dir==-1 else 'DOWN'}")

                if curr_dir == -1 and prev_dir == 1 and qty_owned == 0:
                    place_buy_order()
                elif curr_dir == 1 and prev_dir == -1 and qty_owned > 0:
                    place_sell_order(qty_owned, reason="TREND FLIP")
                    peak_price = 0
            
        except Exception as e:
            print(f"  [LOOP ERROR] {e}")
        
        time.sleep(POLL_SECS)

if __name__ == "__main__":
    # Start health server in background thread
    Thread(target=run_health_server, daemon=True).start()
    # Start trading loop
    trade_loop()
