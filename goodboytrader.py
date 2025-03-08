import time
import logging
import requests
import json
from okx.api import Market, Trade, Account

# ✅ OKX API SETTINGS (Updated Domain)
OKX_BASE_URL = "https://www.okx.com"
SYMBOL = "SOL-USDT-SWAP"

# ✅ API Keys (Replace with your actual API keys)
API_KEY = "your_api_key"
SECRET_KEY = "your_secret_key"
PASSPHRASE = "your_passphrase"

# ✅ Setup Logging
logging.basicConfig(
    filename="goodboytrader.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# ✅ Initialize API Clients
market_api = Market()
trade_api = Trade(api_key=API_KEY, secret_key=SECRET_KEY, passphrase=PASSPHRASE)
account_api = Account(api_key=API_KEY, secret_key=SECRET_KEY, passphrase=PASSPHRASE)

# ✅ Function to Fetch Market Data
def fetch_recent_data(timeframe="4H", limit=100):
    try:
        response = market_api.get_candles(instId=SYMBOL, bar=timeframe, limit=str(limit))
        if response.get("code") == "0":
            logging.info(f"✅ Successfully fetched {timeframe} data")
            return response["data"]
        else:
            logging.error(f"⚠️ API Error: {response}")
            return None
    except requests.exceptions.RequestException as e:
        logging.error(f"⚠️ Data fetch failed: {e}")
        return None

# ✅ Function to Check Trading Conditions
def check_trade_signals():
    logging.info("🔄 Checking for trade signals...")
    df_4h = fetch_recent_data("4H")
    df_15m = fetch_recent_data("15m")

    if not df_4h or not df_15m:
        logging.warning("⚠️ No data available, retrying...")
        return None

    # Example: Placeholder Logic for Trade Signals
    latest_4h_close = float(df_4h[0][4])
    latest_15m_close = float(df_15m[0][4])

    if latest_15m_close > latest_4h_close:
        logging.info(f"📈 Buy Signal Detected! {SYMBOL} @ {latest_15m_close}")
        return "BUY"
    elif latest_15m_close < latest_4h_close:
        logging.info(f"📉 Sell Signal Detected! {SYMBOL} @ {latest_15m_close}")
        return "SELL"
    else:
        logging.info("⏳ No trade signal detected, waiting...")
        return None

# ✅ Function to Execute Trades
def execute_trade(action):
    logging.info(f"🚀 Executing {action} Trade for {SYMBOL}...")
    try:
        order = trade_api.place_order(
            instId=SYMBOL,
            tdMode="cross",
            side=action.lower(),
            ordType="market",
            sz="50",  # Trade size (adjust as needed)
        )
        logging.info(f"✅ Trade Executed: {order}")
    except requests.exceptions.RequestException as e:
        logging.error(f"⚠️ Trade execution failed: {e}")

# ✅ Main Trading Loop
if __name__ == "__main__":
    logging.info("🚀 GoodBoyTrader Bot Starting...")

    while True:
        signal = check_trade_signals()
        if signal:
            execute_trade(signal)

        logging.info("🔄 Sleeping for 60 seconds before next check...")
        time.sleep(60)

