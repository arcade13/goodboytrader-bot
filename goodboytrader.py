import logging
import asyncio
import requests
from telegram import Bot

# ✅ CONFIGURE LOGGING
logging.basicConfig(
    filename="goodboytrader.log",
    filemode="a",
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ✅ TELEGRAM CONFIG
TELEGRAM_BOT_TOKEN = "your_actual_telegram_bot_token"  # Replace with actual bot token
TELEGRAM_CHAT_ID = "your_chat_id"  # Replace with actual chat ID
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# ✅ SEND TELEGRAM MESSAGE FUNCTION (Handles event loop issues)
async def send_telegram_alert(message):
    try:
        logging.info(f"📩 Sending Telegram Alert: {message}")
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
    except Exception as e:
        logging.error(f"⚠️ Failed to send Telegram alert: {e}")

# ✅ FUNCTION TO FETCH OKX DATA
def fetch_okx_data(timeframe="4H"):
    try:
        url = "https://www.okx.com/api/v5/market/candles"
        params = {"instId": "SOL-USDT-SWAP", "bar": timeframe, "limit": "100"}
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        if data["code"] == "0":
            return data["data"]
        else:
            logging.error(f"⚠️ OKX API Error: {data}")
            return None
    except requests.exceptions.RequestException as e:
        logging.error(f"⚠️ Data fetch failed: {e}")
        asyncio.create_task(send_telegram_alert(f"⚠️ Error fetching market data: {e}"))
        return None

# ✅ CHECK FOR TRADE SIGNALS
def check_trade_signal():
    data_4h = fetch_okx_data("4H")
    data_15m = fetch_okx_data("15m")

    if not data_4h or not data_15m:
        logging.warning("⚠️ No market data available, retrying...")
        return None

    # ✅ Implement your trading logic here
    trade_signal = "BUY" if float(data_15m[0][1]) > float(data_4h[0][1]) else "SELL"
    return trade_signal

# ✅ MAIN FUNCTION (No asyncio.run() conflict)
async def main():
    logging.info("✅ Script started successfully")
    print("🚀 GoodBoyTrader Bot Starting...")

    while True:
        logging.info("🔄 Checking for trade signals...")
        trade_signal = check_trade_signal()

        if trade_signal:
            message = f"📊 Trade Signal: {trade_signal}"
            logging.info(message)
            await send_telegram_alert(message)  # ✅ Await inside async function
        else:
            logging.info("⏳ No trade signal detected, waiting...")

        await asyncio.sleep(60)  # ✅ Proper async sleep without conflict

# ✅ RUN MAIN ASYNC FUNCTION SAFELY
if __name__ == "__main__":
    try:
        asyncio.run(main())  # ✅ Now runs safely without conflicts
    except KeyboardInterrupt:
        print("\n❌ Bot Stopped by User")

