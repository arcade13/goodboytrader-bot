import sys
import subprocess
import os
import pkgutil
import logging
import time
import asyncio
import pandas as pd
import numpy as np
import ta
from datetime import datetime, timedelta
import json
import telegram
from dotenv import load_dotenv  # ✅ Import dotenv to load .env file

# ✅ Load environment variables from .env
load_dotenv()

# ✅ Check Installed Modules
installed_modules = [module.name for module in pkgutil.iter_modules()]
print(f"Installed modules: {installed_modules}")

# ✅ Try Installing OKX Module if Missing
try:
    import okx
except ModuleNotFoundError:
    print("⚠️ OKX module not found. Installing...")
    subprocess.run([sys.executable, "-m", "pip", "install", "okx"])
    import okx

# ✅ Verify OKX API Availability
try:
    from okx import api
    print("✅ OKX API found:", dir(api))
    MarketData = api.Market()
    Trade = api.Trade()
    Account = api.Account()
except Exception as e:
    print("⚠️ ERROR: Failed to import OKX API:", str(e))
    exit(1)

# ✅ Load API Keys & Credentials from .env
API_KEY = os.getenv('OKX_API_KEY')
SECRET_KEY = os.getenv('OKX_SECRET_KEY')
PASSPHRASE = os.getenv('OKX_PASSPHRASE')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

# ✅ DEBUG: Print which credentials are loaded
print(f"OKX_API_KEY: {'✔' if API_KEY else '❌ MISSING'}")
print(f"OKX_SECRET_KEY: {'✔' if SECRET_KEY else '❌ MISSING'}")
print(f"OKX_PASSPHRASE: {'✔' if PASSPHRASE else '❌ MISSING'}")
print(f"TELEGRAM_TOKEN: {'✔' if TELEGRAM_TOKEN else '❌ MISSING'}")
print(f"CHAT_ID: {CHAT_ID if CHAT_ID and CHAT_ID.isdigit() else '❌ INVALID CHAT ID'}")

# ✅ VALIDATE CREDENTIALS
missing_vars = [var for var, value in {
    'OKX_API_KEY': API_KEY, 
    'OKX_SECRET_KEY': SECRET_KEY, 
    'OKX_PASSPHRASE': PASSPHRASE, 
    'TELEGRAM_TOKEN': TELEGRAM_TOKEN, 
    'CHAT_ID': CHAT_ID
}.items() if not value]

if missing_vars:
    print(f"⚠️ ERROR: Missing environment variables: {', '.join(missing_vars)}")
    exit(1)

# ✅ Initialize Telegram Bot
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# ✅ Logging Setup
logging.basicConfig(filename='okx_trading_bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ✅ Fetch Data Function
def fetch_recent_data(timeframe='4H', limit='400'):
    try:
        response = MarketData.get_candles(instId="SOL-USDT-SWAP", bar=timeframe, limit=limit)
        if response['code'] != '0':
            raise Exception(f"API error: {response.get('msg', 'Unknown')}")
        
        data = response['data'][::-1]  # Reverse order for chronological data
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'])
        df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
        df[['open', 'high', 'low', 'close', 'vol']] = df[['open', 'high', 'low', 'close', 'vol']].astype(float)
        return df
    except Exception as e:
        print(f"⚠️ Error fetching data: {e}")
        return pd.DataFrame()

# ✅ Get Current Price Function
def get_current_price():
    try:
        response = MarketData.get_ticker(instId="SOL-USDT-SWAP")
        if response['code'] == '0':
            return float(response['data'][0]['last'])
        else:
            print(f"⚠️ API Error: {response.get('msg', 'Unknown')}")
            return None
    except Exception as e:
        print(f"⚠️ Failed to fetch price: {e}")
        return None

# ✅ Main Loop
while True:
    try:
        df_4h = fetch_recent_data(timeframe='4H', limit='400')
        df_15m = fetch_recent_data(timeframe='15m', limit='100')

        if df_4h.empty or df_15m.empty:
            print("⚠️ Insufficient data, retrying...")
            time.sleep(60)
            continue

        price = get_current_price()
        if price is None:
            time.sleep(60)
            continue

        print(f"✅ Current SOL/USDT price: {price}")
        time.sleep(60)

    except Exception as e:
        print(f"⚠️ Main loop error: {str(e)}")
        logging.error(f"Main loop error: {str(e)}")
        asyncio.run(bot.send_message(chat_id=CHAT_ID, text=f"🚨 Bot error: {str(e)}"))
        time.sleep(60)

