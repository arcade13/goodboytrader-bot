import pandas as pd
import ta
from datetime import datetime, timedelta
import logging
import numpy as np
import json
import os
import asyncio
import time
import okx.MarketData as MarketData
import okx.Trade as Trade
import okx.Account as Account
import telegram  # For notifications

# --- Security: Load credentials ---
API_KEY = os.getenv('OKX_API_KEY')
SECRET_KEY = os.getenv('OKX_SECRET_KEY')
PASSPHRASE = os.getenv('OKX_PASSPHRASE')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

# --- Initialize APIs ---
market_api = MarketData.MarketAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')
trade_api = Trade.TradeAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')
account_api = Account.AccountAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')
account_api.set_position_mode(posMode="long_short_mode")
account_api.set_leverage(instId="SOL-USDT-SWAP", lever="5", mgnMode="cross")

# --- Trading Parameters ---
trade_size_usdt = 50  # Fixed 50 USDT per trade
leverage = 5  # 5x leverage
symbol = "SOL-USDT-SWAP"
lot_size = 0.1  # OKX contract size
SLIPPAGE = 0.002
FEES = 0.00075
stop_loss_pct = 0.025
trailing_stop_factor = 1.8

# --- Indicators Configuration ---
ema_short_period = 5
ema_mid_period = 20
ema_long_period = 100
rsi_long_threshold = 55
rsi_short_threshold = 45
adx_4h_threshold = 12
adx_15m_threshold = 15

# --- Logging ---
logging.basicConfig(filename='goodboytrader.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Telegram Bot ---
bot = telegram.Bot(token=TELEGRAM_TOKEN)
async def send_telegram_alert(message):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message)
    except Exception as e:
        logging.error(f"Telegram Error: {str(e)}")

# --- Data Fetching ---
def fetch_data(timeframe='4H', limit='400'):
    response = market_api.get_candlesticks(instId=symbol, bar=timeframe, limit=limit)
    if response['code'] != '0':
        return pd.DataFrame()
    df = pd.DataFrame(response['data'][::-1], columns=['timestamp', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'])
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
    df[['open', 'high', 'low', 'close']] = df[['open', 'high', 'low', 'close']].astype(float)
    return df

# --- Indicator Calculation ---
def calculate_indicators(df):
    if len(df) < ema_long_period:
        return df
    df['ema_short'] = ta.trend.ema_indicator(df['close'], window=ema_short_period)
    df['ema_mid'] = ta.trend.ema_indicator(df['close'], window=ema_mid_period)
    df['ema_long'] = ta.trend.ema_indicator(df['close'], window=ema_long_period)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    df['adx'] = ta.trend.adx(df['high'], df['low'], df['close'], window=14)
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
    df['atr_mean'] = df['atr'].rolling(14).mean()
    return df

# --- Trading Logic ---
def check_entry(df_4h, df_15m):
    if len(df_4h) < ema_long_period or len(df_15m) < ema_long_period:
        return None

    bullish_4h = (df_4h.iloc[-1]['close'] > df_4h.iloc[-1]['ema_short'] > df_4h.iloc[-1]['ema_mid'] > df_4h.iloc[-1]['ema_long'] and df_4h.iloc[-1]['rsi'] > rsi_long_threshold and df_4h.iloc[-1]['adx'] >= adx_4h_threshold)
    bearish_4h = (df_4h.iloc[-1]['close'] < df_4h.iloc[-1]['ema_short'] < df_4h.iloc[-1]['ema_mid'] < df_4h.iloc[-1]['ema_long'] and df_4h.iloc[-1]['rsi'] < rsi_short_threshold and df_4h.iloc[-1]['adx'] >= adx_4h_threshold)
    
    bullish_15m = (df_15m.iloc[-1]['ema_short'] > df_15m.iloc[-1]['ema_mid'] > df_15m.iloc[-1]['ema_long'] and df_15m.iloc[-1]['close'] > df_15m.iloc[-1]['ema_long'] and df_15m.iloc[-1]['rsi'] > rsi_long_threshold and df_15m.iloc[-1]['adx'] >= adx_15m_threshold)
    bearish_15m = (df_15m.iloc[-1]['ema_short'] < df_15m.iloc[-1]['ema_mid'] < df_15m.iloc[-1]['ema_long'] and df_15m.iloc[-1]['close'] < df_15m.iloc[-1]['ema_long'] and df_15m.iloc[-1]['rsi'] < rsi_short_threshold and df_15m.iloc[-1]['adx'] >= adx_15m_threshold)
    
    if bullish_4h and bullish_15m:
        return 'long'
    if bearish_4h and bearish_15m:
        return 'short'
    return None

# --- Main Loop ---
while True:
    try:
        df_4h = calculate_indicators(fetch_data('4H', '400'))
        df_15m = calculate_indicators(fetch_data('15m', '100'))
        
        if df_4h.empty or df_15m.empty:
            time.sleep(60)
            continue

        entry_signal = check_entry(df_4h, df_15m)
        if entry_signal:
            asyncio.run(send_telegram_alert(f'📢 New {entry_signal.upper()} signal detected!'))

        time.sleep(60)  # Run every minute
    except Exception as e:
        logging.error(f"Error: {str(e)}")
        time.sleep(60)

