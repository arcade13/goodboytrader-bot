import pandas as pd
import ta
from datetime import datetime, timedelta
import logging
import numpy as np
import json
import os
import asyncio
import telegram
import time

# ✅ Corrected OKX API Imports
from okx import MarketAPI, TradeAPI, AccountAPI

# --- Security: Load credentials ---
API_KEY = os.getenv('OKX_API_KEY', '')
SECRET_KEY = os.getenv('OKX_SECRET_KEY', '')
PASSPHRASE = os.getenv('OKX_PASSPHRASE', '')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '')
CHAT_ID = os.getenv('CHAT_ID', '')

# --- Validate credentials ---
required_vars = {'OKX_API_KEY': API_KEY, 'OKX_SECRET_KEY': SECRET_KEY, 'OKX_PASSPHRASE': PASSPHRASE, 'TELEGRAM_TOKEN': TELEGRAM_TOKEN, 'CHAT_ID': CHAT_ID}
for var_name, var_value in required_vars.items():
    if not var_value:
        print(f"⚠️ Error: {var_name} is missing. Set it in environment variables.")
        exit(1)

# --- Initialize Telegram Bot ---
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# --- Logging Setup ---
logging.basicConfig(filename='okx_trading_bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Trading Parameters ---
TRADE_SIZE_USDT = 50  # Fixed at 50 USDT
LEVERAGE = 5          # ✅ Corrected to 5x leverage
SYMBOL = "SOL-USDT-SWAP"
INST_ID = "SOL-USDT-SWAP"
LOT_SIZE = 0.1  # Contract size
SLIPPAGE = 0.002  # 0.2% slippage
FEES = 0.00075  # 0.075% fees
STOP_LOSS_PCT = 0.025  # 2.5% stop loss
TRAILING_STOP_FACTOR = 1.8  # 1.8 × ATR trailing stop

# --- Indicator Settings ---
EMA_SHORT = 5
EMA_MID = 20
EMA_LONG = 100
RSI_LONG_THRESH = 55
RSI_SHORT_THRESH = 45
ADX_4H_THRESH = 12
ADX_15M_THRESH = 15

# --- Initialize OKX API ---
market_api = MarketAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')
trade_api = TradeAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')
account_api = AccountAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')

# Set leverage and position mode
account_api.set_position_mode(posMode="long_short_mode")
account_api.set_leverage(instId=INST_ID, lever=str(LEVERAGE), mgnMode="cross")

# --- Utility Functions ---
async def send_telegram_alert(message):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message)
        logging.info(f"Telegram alert sent: {message}")
    except Exception as e:
        logging.error(f"Failed to send Telegram alert: {str(e)}")

def fetch_with_retries(api_call, max_attempts=3):
    for attempt in range(max_attempts):
        try:
            response = api_call()
            if response['code'] != '0':
                raise Exception(f"API error: {response.get('msg', 'Unknown')}")
            return response
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {str(e)}")
            if attempt < max_attempts - 1:
                time.sleep(5 * (attempt + 1))
            else:
                return None

# --- Fetch Market Data ---
def fetch_recent_data(timeframe='4H', limit='400'):
    response = fetch_with_retries(lambda: market_api.get_candlesticks(instId=INST_ID, bar=timeframe, limit=limit))
    if not response:
        return pd.DataFrame()
    data = response['data'][::-1]
    df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'vol'])
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
    df[['open', 'high', 'low', 'close']] = df[['open', 'high', 'low', 'close']].astype(float)
    return df

def get_current_price():
    response = fetch_with_retries(lambda: market_api.get_ticker(instId=INST_ID))
    return float(response['data'][0]['last']) if response else None

# --- Entry Strategy ---
def check_entry(df_4h, df_15m):
    if len(df_4h) < EMA_LONG or len(df_15m) < EMA_LONG:
        return None

    if df_4h['close'].iloc[-1] > df_4h['ema_short'].iloc[-1] > df_4h['ema_mid'].iloc[-1] > df_4h['ema_long'].iloc[-1] and df_4h['rsi'].iloc[-1] > RSI_LONG_THRESH and df_4h['adx'].iloc[-1] >= ADX_4H_THRESH:
        if df_15m['ema_short'].iloc[-1] > df_15m['ema_mid'].iloc[-1] > df_15m['ema_long'].iloc[-1] and df_15m['close'].iloc[-1] > df_15m['ema_long'].iloc[-1] and df_15m['rsi'].iloc[-1] > RSI_LONG_THRESH and df_15m['adx'].iloc[-1] >= ADX_15M_THRESH:
            return 'long'

    if df_4h['close'].iloc[-1] < df_4h['ema_short'].iloc[-1] < df_4h['ema_mid'].iloc[-1] < df_4h['ema_long'].iloc[-1] and df_4h['rsi'].iloc[-1] < RSI_SHORT_THRESH and df_4h['adx'].iloc[-1] >= ADX_4H_THRESH:
        if df_15m['ema_short'].iloc[-1] < df_15m['ema_mid'].iloc[-1] < df_15m['ema_long'].iloc[-1] and df_15m['close'].iloc[-1] < df_15m['ema_long'].iloc[-1] and df_15m['rsi'].iloc[-1] < RSI_SHORT_THRESH and df_15m['adx'].iloc[-1] >= ADX_15M_THRESH:
            return 'short'

    return None

# --- Trading Loop ---
while True:
    try:
        df_4h = fetch_recent_data('4H', '400')
        df_15m = fetch_recent_data('15m', '100')

        if df_4h.empty or df_15m.empty:
            print("❌ Not enough data. Retrying in 1 min...")
            time.sleep(60)
            continue

        entry_price = get_current_price()
        if not entry_price:
            time.sleep(60)
            continue

        signal = check_entry(df_4h, df_15m)
        if signal:
            alert = f"🚀 Trade Signal: {signal.upper()} on {SYMBOL} at {entry_price:.2f}"
            asyncio.run(send_telegram_alert(alert))

        time.sleep(60)  # Check every minute
    except Exception as e:
        logging.error(f"Error: {str(e)}")
        time.sleep(60)

