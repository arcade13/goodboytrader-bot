import sys
import subprocess
import pkgutil
import logging
import os
import time
import asyncio
import pandas as pd
import numpy as np
import ta
from datetime import datetime, timedelta
import json
import telegram  # Telegram alerts

# ✅ DEBUGGING: Print installed modules
installed_modules = [module.name for module in pkgutil.iter_modules()]
print(f"Installed modules: {installed_modules}")

# ✅ CHECK & INSTALL OKX MODULE IF MISSING
try:
    import okx
except ModuleNotFoundError:
    print("⚠️ OKX module not found. Installing...")
    subprocess.run([sys.executable, "-m", "pip", "install", "okx"])
    import okx  # Try importing again

# ✅ CHECK IF OKX API IS ACCESSIBLE
try:
    from okx import api
    print("✅ OKX API found:", dir(api))

    # ✅ Initialize API Instances
    MarketData = api.Market()
    Trade = api.Trade()
    Account = api.Account()
except Exception as e:
    print("⚠️ ERROR: Failed to import OKX API:", str(e))
    exit(1)

# ✅ SECURITY: Load Credentials
API_KEY = os.getenv('OKX_API_KEY')
SECRET_KEY = os.getenv('OKX_SECRET_KEY')
PASSPHRASE = os.getenv('OKX_PASSPHRASE')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

# ✅ DEBUG: Print environment variables (Remove this after testing)
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

# ✅ LOGGING SETUP
logging.basicConfig(filename='okx_trading_bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ✅ TRADING PARAMETERS
base_trade_size_usdt = 50   # Set to 50 USDT
leverage = 5               # From backtest
symbol = "SOL-USDT-SWAP"
instId = "SOL-USDT-SWAP"
lot_size = 0.1             # OKX contract size for SOL-USDT-SWAP
SLIPPAGE = 0.002           # 0.2% slippage from backtest update
FEES = 0.00075             # 0.075% fees from backtest update
stop_loss_pct = 0.025      # 2.5% stop loss from backtest
trailing_stop_factor = 1.8 # 1.8 × ATR trailing stop from backtest

# ✅ STRATEGY PARAMETERS
ema_short_period = 5
ema_mid_period = 20
ema_long_period = 100
rsi_long_threshold = 55
rsi_short_threshold = 45
adx_4h_threshold = 12
adx_15m_threshold = 15

# ✅ STARTUP MESSAGE
startup_message = (
    f" 🚀 OKX Trading Bot Initialized - GoodBoyTrader 🌌\n"
    f"📅 Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    f"💰 Trade Size: {base_trade_size_usdt} USDT @ {leverage}x Leverage\n"
    f"🎯 Symbol: {symbol}\n"
    f"📊 Strategy: EMA {ema_short_period}/{ema_mid_period}/{ema_long_period}, RSI {rsi_long_threshold}/{rsi_short_threshold}, "
    f"ADX 4H >= {adx_4h_threshold}, ADX 15M >= {adx_15m_threshold}\n"
    f"🛡️ Risk: {stop_loss_pct*100:.1f}% SL, {trailing_stop_factor}×ATR Trailing Stop\n"
    f"💸 Costs: {FEES*100:.3f}% Fees, {SLIPPAGE*100:.1f}% Slippage\n"
    f"📬 Notifications: Telegram to Chat ID {CHAT_ID}"
)
print(startup_message)
logging.info(startup_message)

# ✅ DATA FETCHING FUNCTION
def fetch_recent_data(timeframe='4H', limit='400'):
    try:
        response = MarketData.get_candles(instId=instId, bar=timeframe, limit=limit)
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

# ✅ GET CURRENT PRICE
def get_current_price():
    try:
        response = MarketData.get_ticker(instId=instId)
        if response['code'] == '0':
            return float(response['data'][0]['last'])
        else:
            print(f"⚠️ API Error: {response.get('msg', 'Unknown')}")
            return None
    except Exception as e:
        print(f"⚠️ Failed to fetch price: {e}")
        return None

# ✅ MAIN LOOP (LIVE TRADING)
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

