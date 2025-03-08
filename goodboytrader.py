import pandas as pd
import ta
import json
import os
import time
import logging
from datetime import datetime
from okx.api import Trade, Market, Account
import asyncio
import telegram

# --- Load Credentials ---
API_KEY = os.getenv('OKX_API_KEY', 'your_okx_api_key')
SECRET_KEY = os.getenv('OKX_SECRET_KEY', 'your_okx_secret_key')
PASSPHRASE = os.getenv('OKX_PASSPHRASE', 'your_okx_passphrase')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'your_telegram_bot_token')
CHAT_ID = os.getenv('CHAT_ID', 'your_chat_id')

# --- Initialize OKX APIs ---
trade_api = Trade(api_key=API_KEY, api_secret=SECRET_KEY, passphrase=PASSPHRASE)
market_api = Market(api_key=API_KEY, api_secret=SECRET_KEY, passphrase=PASSPHRASE)
account_api = Account(api_key=API_KEY, api_secret=SECRET_KEY, passphrase=PASSPHRASE)

# --- Logging Setup ---
logging.basicConfig(filename='goodboytrader.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Trading Config ---
symbol = "SOL-USDT-SWAP"
leverage = 5
base_trade_size_usdt = 50
ema_short = 5
ema_mid = 20
ema_long = 100
rsi_long = 55
rsi_short = 45
adx_4h = 12
adx_15m = 15
stop_loss_pct = 0.025
trailing_stop_factor = 1.8

# --- Telegram Bot ---
bot = telegram.Bot(token=TELEGRAM_TOKEN)

async def send_telegram_alert(message):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message)
        logging.info(f"Telegram alert sent: {message}")
    except Exception as e:
        logging.error(f"Failed to send Telegram alert: {str(e)}")

# --- Fetch Market Data ---
def fetch_recent_data(timeframe='4H', limit=10):
    try:
        response = market_api.get_candles(instId=symbol, bar=timeframe, limit=limit)
        if response.get("code") != "0":
            print(f"❌ API Error: {response.get('msg', 'Unknown error')}")
            return pd.DataFrame()
        
        data = response.get("data", [])
        if not data:
            print("⚠️ No market data received!")
            return pd.DataFrame()
        
        df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms")
        df[["open", "high", "low", "close", "vol"]] = df[["open", "high", "low", "close", "vol"]].astype(float)
        return df
    except Exception as e:
        print(f"⚠️ Data fetch failed: {str(e)}")
        return pd.DataFrame()

# --- Indicator Calculations ---
def calculate_indicators(df):
    if len(df) < ema_long:
        return df
    df["ema_short"] = ta.trend.ema_indicator(df["close"], window=ema_short)
    df["ema_mid"] = ta.trend.ema_indicator(df["close"], window=ema_mid)
    df["ema_long"] = ta.trend.ema_indicator(df["close"], window=ema_long)
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)
    df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
    return df

# --- Trading Execution ---
def execute_trade():
    df_4h = fetch_recent_data(timeframe='4H', limit=100)
    df_15m = fetch_recent_data(timeframe='15m', limit=100)
    
    if df_4h.empty or df_15m.empty:
        print("⚠️ No data available, retrying...")
        return
    
    df_4h = calculate_indicators(df_4h)
    df_15m = calculate_indicators(df_15m)
    
    if len(df_4h) < ema_long or len(df_15m) < ema_long:
        print("⚠️ Not enough data for indicators")
        return
    
    last_4h = df_4h.iloc[-1]
    last_15m = df_15m.iloc[-1]
    
    # Check Long Entry
    if last_4h["ema_short"] > last_4h["ema_mid"] > last_4h["ema_long"] and last_4h["rsi"] > rsi_long and last_4h["adx"] >= adx_4h:
        if last_15m["ema_short"] > last_15m["ema_mid"] > last_15m["ema_long"] and last_15m["rsi"] > rsi_long and last_15m["adx"] >= adx_15m:
            print("📈 Long Entry Detected!")
            asyncio.run(send_telegram_alert("📈 Long Entry Detected!"))
            return
    
    # Check Short Entry
    if last_4h["ema_short"] < last_4h["ema_mid"] < last_4h["ema_long"] and last_4h["rsi"] < rsi_short and last_4h["adx"] >= adx_4h:
        if last_15m["ema_short"] < last_15m["ema_mid"] < last_15m["ema_long"] and last_15m["rsi"] < rsi_short and last_15m["adx"] >= adx_15m:
            print("📉 Short Entry Detected!")
            asyncio.run(send_telegram_alert("📉 Short Entry Detected!"))
            return
    
    print("✅ No trade conditions met, waiting...")

# --- Main Loop ---
print("🚀 GoodBoyTrader Bot Started!")
while True:
    execute_trade()
    time.sleep(60)

