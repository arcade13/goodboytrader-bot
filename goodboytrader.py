import pandas as pd
import ta
from datetime import datetime, timedelta
import logging
import numpy as np
import json
import os
import time
import asyncio
import telegram

# ✅ FIXED IMPORTS
from okx.api import Market, Trade, Account

# --- Load credentials from Environment Variables ---
API_KEY = os.getenv('OKX_API_KEY')
SECRET_KEY = os.getenv('OKX_SECRET_KEY')
PASSPHRASE = os.getenv('OKX_PASSPHRASE')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

# Validate environment variables
if not all([API_KEY, SECRET_KEY, PASSPHRASE, TELEGRAM_TOKEN, CHAT_ID]):
    raise ValueError("⚠️ Missing required environment variables. Please check your .env or Render settings.")

# ✅ Initialize OKX APIs
market_api = Market.MarketAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')
trade_api = Trade.TradeAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')
account_api = Account.AccountAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')

# ✅ Initialize Telegram Bot
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# ✅ Logging Setup
logging.basicConfig(filename='okx_trading_bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ✅ Trading Parameters
trade_size = 50
leverage = 5
symbol = "SOL-USDT-SWAP"
stop_loss_pct = 0.025
trailing_stop_factor = 1.8
fees = 0.00075
slippage = 0.002

# ✅ Notify on Start
startup_message = f"🚀 GoodBoyTrader Initialized | {symbol} | {leverage}x Leverage"
print(startup_message)
logging.info(startup_message)

# ✅ Utility Functions
async def send_telegram_alert(message):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message)
        logging.info(f"Telegram alert sent: {message}")
    except Exception as e:
        logging.error(f"Failed to send Telegram alert: {str(e)}")
        print(f"⚠️ Telegram alert failed: {str(e)}")

def fetch_with_retries(api_call, max_attempts=3):
    for attempt in range(max_attempts):
        try:
            response = api_call()
            if response['code'] != '0':
                raise Exception(f"API error: {response.get('msg', 'Unknown')}")
            return response
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {str(e)}")
            time.sleep(5 * (attempt + 1))
    return None

# ✅ Fetch Market Data
def fetch_recent_data(timeframe='4H', limit='400'):
    response = fetch_with_retries(lambda: market_api.get_candlesticks(instId=symbol, bar=timeframe, limit=limit))
    if not response:
        return pd.DataFrame()
    
    data = response['data'][::-1]
    df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'])
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
    df[['open', 'high', 'low', 'close', 'vol']] = df[['open', 'high', 'low', 'close', 'vol']].astype(float)
    return df

# ✅ Entry Logic
def check_entry(df_4h, df_15m):
    if len(df_4h) < 100 or len(df_15m) < 100:
        return None

    current_4h = df_4h.iloc[-1]
    current_15m = df_15m.iloc[-1]

    bullish_4h = (current_4h['close'] > current_4h['ema_5'] > current_4h['ema_20'] > current_4h['ema_100'])
    bearish_4h = (current_4h['close'] < current_4h['ema_5'] < current_4h['ema_20'] < current_4h['ema_100'])

    bullish_15m = (current_15m['close'] > current_15m['ema_5'] > current_15m['ema_20'] > current_15m['ema_100'])
    bearish_15m = (current_15m['close'] < current_15m['ema_5'] < current_15m['ema_20'] < current_15m['ema_100'])

    if bullish_4h and bullish_15m:
        return "long"
    elif bearish_4h and bearish_15m:
        return "short"
    return None

# ✅ Place Order
def place_order(side, price, size):
    response = trade_api.place_order(instId=symbol, tdMode='cross', side=side, ordType='market', sz=str(size))
    return response

# ✅ Monitor Position
def monitor_position(position, entry_price):
    while True:
        price = fetch_recent_data(timeframe='1m', limit='1')['close'].iloc[-1]
        print(f"Current Price: {price}")

        if position == "long" and price >= entry_price * 1.05:
            print("📈 Take Profit Hit! Exiting long position.")
            place_order("sell", price, trade_size)
            break
        elif position == "short" and price <= entry_price * 0.95:
            print("📉 Take Profit Hit! Exiting short position.")
            place_order("buy", price, trade_size)
            break

        time.sleep(10)

# ✅ Main Loop
while True:
    df_4h = fetch_recent_data(timeframe='4H', limit='100')
    df_15m = fetch_recent_data(timeframe='15m', limit='100')

    if df_4h.empty or df_15m.empty:
        print("Waiting for sufficient data...")
        time.sleep(60)
        continue

    signal = check_entry(df_4h, df_15m)
    if signal:
        entry_price = fetch_recent_data(timeframe='1m', limit='1')['close'].iloc[-1]
        place_order(signal, entry_price, trade_size)
        monitor_position(signal, entry_price)

    time.sleep(60)

