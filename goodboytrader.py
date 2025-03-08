import os
import time
import json
import asyncio
import logging
import requests
import pandas as pd
import ta
from datetime import datetime
import telegram
from okx.api import Market, Trade, Account  # ✅ Corrected Import

# --- Load Environment Variables ---
API_KEY = os.getenv('OKX_API_KEY', 'your_okx_api_key')
SECRET_KEY = os.getenv('OKX_SECRET_KEY', 'your_okx_secret_key')
PASSPHRASE = os.getenv('OKX_PASSPHRASE', 'your_okx_passphrase')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'your_telegram_token')
CHAT_ID = os.getenv('CHAT_ID', 'your_chat_id')

# --- OKX API Setup ---
market_api = Market()
trade_api = Trade()  # ✅ No need for API keys here
account_api = Account()  # ✅ No need for API keys here

# --- Logging Setup ---
logging.basicConfig(filename='goodboytrader.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Initialize Telegram Bot ---
bot = telegram.Bot(token=TELEGRAM_TOKEN)

async def send_telegram_alert(message):
    """Sends an alert to Telegram"""
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message)
        logging.info(f"Telegram Alert Sent: {message}")
    except Exception as e:
        logging.error(f"Telegram Alert Failed: {e}")

# --- Utility Function to Fetch Data ---
def fetch_recent_data(timeframe='4H', limit='400'):
    """Fetches recent market data from OKX"""
    try:
        response = market_api.get_candles(instId="SOL-USDT-SWAP", bar=timeframe, limit=limit)
        if response['code'] != '0':
            raise Exception(f"API error: {response.get('msg', 'Unknown')}")
        data = response['data'][::-1]
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'])
        df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
        df[['open', 'high', 'low', 'close', 'vol']] = df[['open', 'high', 'low', 'close', 'vol']].astype(float)
        return df
    except Exception as e:
        logging.error(f"Error fetching data: {e}")
        asyncio.run(send_telegram_alert(f"⚠️ Error fetching market data: {e}"))
        return pd.DataFrame()  # Return empty DataFrame if error occurs

# --- Main Function ---
if __name__ == "__main__":
    print("🚀 GoodBoyTrader Bot Started!")
    logging.info("GoodBoyTrader Bot Started")

    while True:
        df_4h = fetch_recent_data(timeframe='4H', limit='400')
        if df_4h.empty:
            print("⚠️ No data fetched, retrying in 60 seconds...")
            time.sleep(60)
            continue

        print("✅ Market data fetched successfully.")
        time.sleep(60)  # Check every minute

