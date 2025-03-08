import pandas as pd
import ta
from datetime import datetime, timedelta
import logging
import os
import asyncio
import telegram
import time
from okx.api import Market, Trade, Account  # Ensure correct import
import requests

# --- Security: Load API Credentials ---
API_KEY = os.getenv('OKX_API_KEY', 'your_okx_api_key')
SECRET_KEY = os.getenv('OKX_SECRET_KEY', 'your_okx_secret_key')
PASSPHRASE = os.getenv('OKX_PASSPHRASE', 'your_okx_passphrase')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'your_telegram_bot_token')
CHAT_ID = os.getenv('CHAT_ID', 'your_chat_id')

# --- Ensure Correct API URL ---
OKX_BASE_URL = "https://www.okx.com"

# --- Initialize APIs with Correct URL ---
market_api = Market(base_url=OKX_BASE_URL)
trade_api = Trade(base_url=OKX_BASE_URL)
account_api = Account(base_url=OKX_BASE_URL)

# --- Logging Setup ---
logging.basicConfig(filename='okx_trading_bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Trading Parameters ---
base_trade_size_usdt = 50
leverage = 5
symbol = "SOL-USDT-SWAP"
stop_loss_pct = 0.025
trailing_stop_factor = 1.8

# --- Indicator Parameters ---
ema_short_period = 5
ema_mid_period = 20
ema_long_period = 100
rsi_long_threshold = 55
rsi_short_threshold = 45
adx_4h_threshold = 12
adx_15m_threshold = 15

# --- Initialize Telegram Bot ---
bot = telegram.Bot(token=TELEGRAM_TOKEN)

async def send_telegram_alert(message):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message)
    except Exception as e:
        logging.error(f"Failed to send Telegram alert: {str(e)}")

# --- Fetch Market Data with DNS Retry ---
def fetch_recent_data(timeframe='4H', limit='400'):
    for attempt in range(3):
        try:
            response = market_api.get_candles(instId=symbol, bar=timeframe, limit=limit)
            if response:
                data = response['data'][::-1]
                df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'vol'])
                df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
                df[['open', 'high', 'low', 'close', 'vol']] = df[['open', 'high', 'low', 'close', 'vol']].astype(float)
                return df
        except requests.exceptions.ConnectionError:
            logging.error("Connection error: Retrying in 5 seconds...")
            time.sleep(5)
    return pd.DataFrame()

# --- Check Internet Connection ---
def check_okx_connection():
    try:
        response = requests.get("https://www.okx.com/api/v5/public/time", timeout=5)
        if response.status_code == 200:
            return True
    except requests.exceptions.RequestException:
        return False
    return False

# --- Main Loop ---
while True:
    if not check_okx_connection():
        logging.error("⚠️ No internet connection to OKX! Retrying...")
        time.sleep(30)
        continue

    df_4h = fetch_recent_data(timeframe='4H', limit='400')
    if df_4h.empty:
        logging.error("Failed to fetch market data. Retrying...")
        time.sleep(30)
        continue
    
    print("✅ Market data fetched successfully!")
    logging.info("✅ Market data fetched successfully!")
    time.sleep(60)
