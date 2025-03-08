import os
import time
import json
import logging
import asyncio
import pandas as pd
import ta
import requests
from datetime import datetime
from okx.api import Market, Trade, Account
import telegram

# --- Load Environment Variables ---
API_KEY = os.getenv("OKX_API_KEY")
SECRET_KEY = os.getenv("OKX_SECRET_KEY")
PASSPHRASE = os.getenv("OKX_PASSPHRASE")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SYMBOL = "SOL-USDT-SWAP"

# --- API Clients ---
market_api = Market()
trade_api = Trade()
account_api = Account()

# --- Trading Parameters ---
TRADE_SIZE_USDT = 50  # Fixed trade size in USDT
LEVERAGE = 5  # 5x leverage
STOP_LOSS_PERCENT = 0.025  # 2.5% Stop Loss
TRAILING_STOP_ATR_FACTOR = 1.8  # 1.8x ATR
FEES = 0.00075  # Trading fees (0.075%)
SLIPPAGE = 0.002  # 0.2% slippage

# --- Indicator Parameters ---
EMA_SHORT = 5
EMA_MID = 20
EMA_LONG = 100
RSI_LONG_THRESHOLD = 55
RSI_SHORT_THRESHOLD = 45
ADX_4H_THRESHOLD = 12
ADX_15M_THRESHOLD = 15

# --- Initialize Logging ---
logging.basicConfig(filename="goodboytrader.log", level=logging.INFO, format="%(asctime)s - %(message)s")

# --- Initialize Telegram Bot ---
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# --- Function to Fetch Market Data ---
def fetch_recent_data(timeframe="4H", limit=100):
    try:
        response = market_api.get_candles(instId=SYMBOL, bar=timeframe, limit=str(limit))
        if response["code"] != "0":
            logging.error(f"API Error: {response['msg']}")
            return pd.DataFrame()
        
        data = response["data"][::-1]
        df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms")
        df[["open", "high", "low", "close", "vol"]] = df[["open", "high", "low", "close", "vol"]].astype(float)
        return df
    except Exception as e:
        logging.error(f"Data fetch failed: {e}")
        return pd.DataFrame()

# --- Function to Calculate Indicators ---
def calculate_indicators(df):
    if len(df) < EMA_LONG:
        return df
    df["ema_short"] = ta.trend.ema_indicator(df["close"], window=EMA_SHORT)
    df["ema_mid"] = ta.trend.ema_indicator(df["close"], window=EMA_MID)
    df["ema_long"] = ta.trend.ema_indicator(df["close"], window=EMA_LONG)
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)
    df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
    df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)
    return df

# --- Function to Check Trade Entry Conditions ---
def check_entry(df_4h, df_15m):
    last_4h = df_4h.iloc[-1]
    last_15m = df_15m.iloc[-1]

    bullish_4h = (last_4h["close"] > last_4h["ema_short"] > last_4h["ema_mid"] > last_4h["ema_long"]
                  and last_4h["rsi"] > RSI_LONG_THRESHOLD and last_4h["adx"] >= ADX_4H_THRESHOLD)
    
    bearish_4h = (last_4h["close"] < last_4h["ema_short"] < last_4h["ema_mid"] < last_4h["ema_long"]
                  and last_4h["rsi"] < RSI_SHORT_THRESHOLD and last_4h["adx"] >= ADX_4H_THRESHOLD)
    
    bullish_15m = (last_15m["ema_short"] > last_15m["ema_mid"] > last_15m["ema_long"]
                   and last_15m["close"] > last_15m["ema_long"] and last_15m["rsi"] > RSI_LONG_THRESHOLD
                   and last_15m["adx"] >= ADX_15M_THRESHOLD)
    
    bearish_15m = (last_15m["ema_short"] < last_15m["ema_mid"] < last_15m["ema_long"]
                   and last_15m["close"] < last_15m["ema_long"] and last_15m["rsi"] < RSI_SHORT_THRESHOLD
                   and last_15m["adx"] >= ADX_15M_THRESHOLD)

    if bullish_4h and bullish_15m:
        return "long"
    if bearish_4h and bearish_15m:
        return "short"
    return None

# --- Function to Place Orders ---
def place_order(side, price):
    size = TRADE_SIZE_USDT / price
    response = trade_api.place_order(instId=SYMBOL, tdMode="cross", side="buy" if side == "long" else "sell",
                                     posSide=side, ordType="market", sz=str(round(size, 2)))

    if response["code"] == "0":
        logging.info(f"Order executed: {side} at {price}")
        asyncio.run(bot.send_message(chat_id=CHAT_ID, text=f"🚀 {side.upper()} position opened at {price}"))
        return response["data"][0]["ordId"], size
    else:
        logging.error(f"Order failed: {response['msg']}")
        return None, 0

# --- Main Loop ---
while True:
    logging.info("🔄 Checking for trade signals...")

    df_4h = calculate_indicators(fetch_recent_data("4H"))
    df_15m = calculate_indicators(fetch_recent_data("15m"))

    if df_4h.empty or df_15m.empty:
        logging.warning("⚠️ No data available, retrying...")
        time.sleep(60)
        continue

    entry_signal = check_entry(df_4h, df_15m)
    current_price = df_15m.iloc[-1]["close"]

    if entry_signal:
        order_id, size = place_order(entry_signal, current_price)
        if order_id:
            logging.info(f"✅ Trade placed: {entry_signal} at {current_price}")
    else:
        logging.info("⏳ No trade signal detected, waiting...")

    time.sleep(60)

