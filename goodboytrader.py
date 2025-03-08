import pandas as pd
import ta
import logging
import json
import os
import time
import asyncio
import telegram
from datetime import datetime
from okx.api import Market, Trade, Account  # ✅ Fixed OKX imports

# --- Security: Load credentials ---
API_KEY = os.getenv('OKX_API_KEY', 'your_okx_api_key')
SECRET_KEY = os.getenv('OKX_SECRET_KEY', 'your_okx_secret_key')
PASSPHRASE = os.getenv('OKX_PASSPHRASE', 'your_okx_passphrase')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'your_telegram_bot_token')
CHAT_ID = os.getenv('CHAT_ID', 'your_chat_id')

# --- Validate credentials ---
required_vars = {'OKX_API_KEY': API_KEY, 'OKX_SECRET_KEY': SECRET_KEY, 'OKX_PASSPHRASE': PASSPHRASE, 
                'TELEGRAM_TOKEN': TELEGRAM_TOKEN, 'CHAT_ID': CHAT_ID}
for var_name, var_value in required_vars.items():
    if var_value == f'your_{var_name.lower()}':
        print(f"⚠️ Error: {var_name} not set in environment variables.")
        exit(1)

# --- Initialize Telegram Bot ---
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# --- Logging Setup ---
logging.basicConfig(filename='goodboytrader.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- OKX API Initialization ---
market_api = Market(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE)
trade_api = Trade(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE)
account_api = Account(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE)

# --- Trading Parameters ---
symbol = "SOL-USDT-SWAP"
leverage = 5  # ✅ Ensure leverage is set to 5x
trade_size_usdt = 50  # Fixed trade size of 50 USDT
stop_loss_pct = 0.025  # 2.5% SL
trailing_stop_factor = 1.8  # Trailing Stop
ema_short_period, ema_mid_period, ema_long_period = 5, 20, 100
rsi_long_threshold, rsi_short_threshold = 55, 45
adx_4h_threshold, adx_15m_threshold = 12, 15

# --- Function: Fetch Market Data ---
def fetch_recent_data(timeframe='4H', limit='400'):
    response = market_api.get_candles(instId=symbol, bar=timeframe, limit=limit)
    if 'data' not in response:
        return pd.DataFrame()
    
    data = response['data'][::-1]
    df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'vol'])
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
    df[['open', 'high', 'low', 'close']] = df[['open', 'high', 'low', 'close']].astype(float)
    return df

# --- Function: Calculate Indicators ---
def calculate_indicators(df):
    if len(df) < ema_long_period:
        return df
    df['ema_short'] = ta.trend.ema_indicator(df['close'], window=ema_short_period)
    df['ema_mid'] = ta.trend.ema_indicator(df['close'], window=ema_mid_period)
    df['ema_long'] = ta.trend.ema_indicator(df['close'], window=ema_long_period)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    df['adx'] = ta.trend.adx(df['high'], df['low'], df['close'], window=14)
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
    return df

# --- Function: Check Entry Signal ---
def check_entry(df_4h, df_15m):
    if len(df_4h) < ema_long_period or len(df_15m) < ema_long_period:
        return None
    
    current_4h, current_15m = df_4h.iloc[-1], df_15m.iloc[-1]

    bullish_4h = (current_4h['close'] > current_4h['ema_short'] > current_4h['ema_mid'] > current_4h['ema_long'] and
                  current_4h['rsi'] > rsi_long_threshold and current_4h['adx'] >= adx_4h_threshold)
    bearish_4h = (current_4h['close'] < current_4h['ema_short'] < current_4h['ema_mid'] < current_4h['ema_long'] and
                  current_4h['rsi'] < rsi_short_threshold and current_4h['adx'] >= adx_4h_threshold)

    bullish_15m = (current_15m['ema_short'] > current_15m['ema_mid'] > current_15m['ema_long'] and
                   current_15m['rsi'] > rsi_long_threshold and current_15m['adx'] >= adx_15m_threshold)
    bearish_15m = (current_15m['ema_short'] < current_15m['ema_mid'] < current_15m['ema_long'] and
                   current_15m['rsi'] < rsi_short_threshold and current_15m['adx'] >= adx_15m_threshold)

    if bullish_4h and bullish_15m:
        return 'long'
    elif bearish_4h and bearish_15m:
        return 'short'
    return None

# --- Function: Execute Trade ---
def place_order(side):
    response = trade_api.place_order(instId=symbol, tdMode='cross', side='buy' if side == 'long' else 'sell', ordType='market', sz=str(trade_size_usdt))
    if response.get('code') == '0':
        alert = f"✅ Trade Executed: {side.upper()} - 50 USDT on {symbol}"
        asyncio.run(bot.send_message(chat_id=CHAT_ID, text=alert))
        return response['data'][0]['ordId']
    else:
        logging.error(f"Order failed: {response.get('msg', 'Unknown error')}")
        return None

# --- Function: Monitor Position ---
def monitor_trade(side, entry_price):
    stop_loss = entry_price * (1 - stop_loss_pct) if side == 'long' else entry_price * (1 + stop_loss_pct)
    while True:
        current_price = float(market_api.get_ticker(instId=symbol)['data'][0]['last'])
        if (side == 'long' and current_price <= stop_loss) or (side == 'short' and current_price >= stop_loss):
            trade_api.close_position(instId=symbol, posSide='long' if side == 'long' else 'short', mgnMode='cross')
            alert = f"❌ Stop Loss Hit: {side.upper()} exited at {current_price}"
            asyncio.run(bot.send_message(chat_id=CHAT_ID, text=alert))
            return
        time.sleep(30)

# --- Main Trading Loop ---
while True:
    df_4h = calculate_indicators(fetch_recent_data(timeframe='4H', limit='400'))
    df_15m = calculate_indicators(fetch_recent_data(timeframe='15m', limit='100'))
    
    trade_signal = check_entry(df_4h, df_15m)
    if trade_signal:
        entry_price = float(market_api.get_ticker(instId=symbol)['data'][0]['last'])
        order_id = place_order(trade_signal)
        if order_id:
            monitor_trade(trade_signal, entry_price)
    
    time.sleep(60)

