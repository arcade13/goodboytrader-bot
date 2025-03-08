import asyncio
import os
import json
import sys
from datetime import datetime
import pandas as pd
import ta
from telegram import Bot

# OKX API Imports (Debugging)
import logging
import okx

# Configure Logging
logging.basicConfig(
    filename="goodboytrader.log",
    filemode="a",
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# Log OKX version and structure
logging.info(f"OKX SDK version: {okx.__version__}")
logging.info(f"OKX contents: {dir(okx)}")
print(f"OKX SDK version: {okx.__version__}")
print(f"OKX contents: {dir(okx)}")

# ✅ Correct OKX API Imports
from okx.api import Market, Trade, Account

# Load Environment Variables
API_KEY = os.getenv("OKX_API_KEY")
SECRET_KEY = os.getenv("OKX_SECRET_KEY")
PASSPHRASE = os.getenv("OKX_PASSPHRASE")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Validate Credentials
if not all([API_KEY, SECRET_KEY, PASSPHRASE]):
    raise ValueError("❌ Missing OKX_API_KEY, OKX_SECRET_KEY, or OKX_PASSPHRASE.")
if not TELEGRAM_TOKEN:
    raise ValueError("❌ Missing TELEGRAM_TOKEN.")
if not CHAT_ID or not CHAT_ID.strip().isdigit():
    raise ValueError("❌ CHAT_ID must be numeric.")

TELEGRAM_CHAT_ID = int(CHAT_ID)

# Define the correct base URL
BASE_API_URL = "https://www.okx.com"

# ✅ Initialize OKX API Clients
market_api = Market(key=API_KEY, secret=SECRET_KEY, passphrase=PASSPHRASE, flag='0')
trade_api = Trade(key=API_KEY, secret=SECRET_KEY, passphrase=PASSPHRASE, flag='0')
account_api = Account(key=API_KEY, secret=SECRET_KEY, passphrase=PASSPHRASE, flag='0')

# 🔥 Patch the base URL using `_client._base_url`
market_api._client._base_url = "https://www.okx.com"
trade_api._client._base_url = "https://www.okx.com"
account_api._client._base_url = "https://www.okx.com"

# ✅ Debug to confirm
print(f"DEBUG: Market API base URL set to: {market_api._client._base_url}")

# Initialize Telegram Bot
bot = Bot(token=TELEGRAM_TOKEN)

# Logging Environment Info
logging.info(f"Python executable: {sys.executable}")
logging.info(f"API_KEY loaded: {bool(API_KEY)}, SECRET_KEY loaded: {bool(SECRET_KEY)}, PASSPHRASE loaded: {bool(PASSPHRASE)}")

# Trading Parameters
base_trade_size_usdt = 50
leverage = 5
symbol = "SOL-USDT-SWAP"
instId = "SOL-USDT-SWAP"
lot_size = 0.1
SLIPPAGE = 0.002
FEES = 0.00075
stop_loss_pct = 0.025
trailing_stop_factor = 1.8

# Tunable Parameters
ema_short_period = 5
ema_mid_period = 20
ema_long_period = 100
rsi_long_threshold = 55
rsi_short_threshold = 45
adx_4h_threshold = 12
adx_15m_threshold = 15

# Startup Message
startup_message = (
    f" 🚀 OKX Trading Bot Initialized - GoodBoyTrader 🌌\n"
    f"📅 Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    f"💰 Trade Size: {base_trade_size_usdt} USDT @ {leverage}x Leverage\n"
    f"🎯 Symbol: {symbol}\n"
    f"📊 Strategy: EMA {ema_short_period}/{ema_mid_period}/{ema_long_period}, RSI {rsi_long_threshold}/{rsi_short_threshold}, "
    f"ADX 4H >= {adx_4h_threshold}, ADX 15M >= {adx_15m_threshold}\n"
    f"🛡️ Risk: {stop_loss_pct*100:.1f}% SL, {trailing_stop_factor}×ATR Trailing Stop\n"
    f"💸 Costs: {FEES*100:.3f}% Fees, {SLIPPAGE*100:.1f}% Slippage\n"
    f"📬 Notifications: Telegram to Chat ID {TELEGRAM_CHAT_ID}"
)

# Global State
position_state = None
entry_atr = 0
trade = None

# Utility Functions
async def send_telegram_alert(message):
    try:
        logging.info(f"📩 Sending Telegram Alert: {message}")
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
    except Exception as e:
        logging.error(f"⚠️ Failed to send Telegram alert: {e}")

async def fetch_with_retries(api_call, max_attempts=3):
    for attempt in range(max_attempts):
        try:
            print(f"DEBUG: Attempt {attempt + 1} to call OKX API")
            response = api_call()  # Synchronous call, since MarketAPI isn’t async
            print(f"DEBUG: Raw API response: {response}")
            if response is None:
                raise ValueError("API returned None")
            if 'code' not in response:
                raise ValueError("Response missing 'code' key")
            if response['code'] != '0':
                raise Exception(f"API error: code={response['code']}, msg={response.get('msg', 'Unknown')}")
            return response
        except Exception as e:
            print(f"❌ ERROR: Attempt {attempt + 1} failed: {str(e)}")
            logging.error(f"Attempt {attempt + 1} failed: {str(e)}")
            if attempt < max_attempts - 1:
                await asyncio.sleep(5 * (attempt + 1))
            else:
                print(f"❌ ERROR: All {max_attempts} attempts failed, giving up")
                logging.error(f"All {max_attempts} attempts failed")
                return None

# Trade Tracker
class TradeTracker:
    def __init__(self):
        self.total_pnl = 0
        self.trade_count = 0
        self.wins = 0
        self.losses = 0

    def update(self, trade, partial=False):
        size = trade['size_sol'] * (0.5 if partial else 1.0)
        entry_value = trade['entry_price'] * size
        exit_value = trade['exit_price'] * size
        fees = (entry_value + exit_value) * FEES
        slippage_cost = entry_value * SLIPPAGE * 2
        total_cost = fees + slippage_cost
        pnl_raw = (trade['exit_price'] - trade['entry_price']) * size * (1 if trade['side'] == 'long' else -1)
        pnl = pnl_raw - total_cost if not partial else pnl_raw - (total_cost / 2)
        self.total_pnl += pnl
        if not partial:
            self.trade_count += 1
            self.wins += 1 if pnl > 0 else 0
            self.losses += 1 if pnl < 0 else 0
        logging.info(f"Trade {'partial' if partial else 'completed'}. PnL: {pnl:.2f} USDT, Total: {self.total_pnl:.2f}")

tracker = TradeTracker()

# State Management
def save_trade_state(trade, position_state):
    state = {'position_state': position_state, 'trade': trade}
    with open("trade_state.json", 'w') as f:
        json.dump(state, f, default=str)

def load_trade_state():
    if os.path.exists("trade_state.json"):
        with open("trade_state.json", 'r') as f:
            state = json.load(f)
            state['trade']['entry_time'] = datetime.fromisoformat(state['trade']['entry_time'])
            return state['position_state'], state['trade']
    return None, None

def clear_trade_state():
    if os.path.exists("trade_state.json"):
        os.remove("trade_state.json")

# Indicator Calculations
def calculate_indicators(df, timeframe='4H'):
    if len(df) < ema_long_period:
        return df
    df['ema_short'] = ta.trend.ema_indicator(df['close'], window=ema_short_period)
    df['ema_mid'] = ta.trend.ema_indicator(df['close'], window=ema_mid_period)
    df['ema_long'] = ta.trend.ema_indicator(df['close'], window=ema_long_period)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    df['adx'] = ta.trend.adx(df['high'], df['low'], df['close'], window=14)
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
    df['atr_mean'] = df['atr'].rolling(14).mean()
    return df

# Data Fetching (Updated with your fix)
async def fetch_recent_data(timeframe='4H', limit='400'):
    print(f"DEBUG: Fetching {timeframe} data from OKX with limit={limit}")
    try:
        response = await fetch_with_retries(
            lambda: market_api.get_candles(instId=instId, bar=timeframe, limit=limit)
        )
    except asyncio.TimeoutError:
        print(f"❌ ERROR: Timeout while fetching {timeframe} data! Retrying in 60s...")
        logging.error(f"❌ Timeout while fetching {timeframe} data!")
        return pd.DataFrame()
    except Exception as e:
        print(f"❌ ERROR: Exception while fetching {timeframe} data: {e}")
        logging.error(f"❌ Exception in fetch_recent_data({timeframe}): {e}")
        return pd.DataFrame()

    print(f"DEBUG: Received response for {timeframe}: {response}")
    if not response:
        print(f"❌ ERROR: API response is None for {timeframe}! Double-check `instId` and API permissions.")
        logging.error(f"❌ API response is None for {timeframe}. instId={instId}")
        return pd.DataFrame()
    if 'code' in response and response['code'] != '0':
        print(f"❌ ERROR: OKX API Error: {response.get('msg', 'Unknown Error')}")
        logging.error(f"❌ OKX API Error: {response}")
        return pd.DataFrame()

    print(f"✅ SUCCESS: Data received for {timeframe}, processing...")
    data = response['data'][::-1]
    df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'])
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
    df[['open', 'high', 'low', 'close', 'vol']] = df[['open', 'high', 'low', 'close', 'vol']].astype(float)
    return calculate_indicators(df, timeframe)

# Entry Logic
def check_entry(df_4h, df_15m):
    if len(df_4h) < ema_long_period or len(df_15m) < ema_long_period:
        return None
    current_4h = df_4h.iloc[-1]
    current_15m = df_15m.iloc[-1]
    bearish_4h = (current_4h['close'] < current_4h['ema_short'] < current_4h['ema_mid'] < current_4h['ema_long'] and
                  current_4h['rsi'] < rsi_short_threshold and current_4h['adx'] >= adx_4h_threshold)
    bullish_4h = (current_4h['close'] > current_4h['ema_short'] > current_4h['ema_mid'] > current_4h['ema_long'] and
                  current_4h['rsi'] > rsi_long_threshold and current_4h['adx'] >= adx_4h_threshold)
    if not bearish_4h and not bullish_4h:
        return None
    if bearish_4h:
        bearish_15m = (current_15m['ema_short'] < current_15m['ema_mid'] < current_15m['ema_long'] and
                       current_15m['close'] < current_15m['ema_long'] and
                       current_15m['rsi'] < rsi_short_threshold and current_15m['adx'] >= adx_15m_threshold)
        if bearish_15m:
            return 'short'
    elif bullish_4h:
        bullish_15m = (current_15m['ema_short'] > current_15m['ema_mid'] > current_15m['ema_long'] and
                       current_15m['close'] > current_15m['ema_long'] and
                       current_15m['rsi'] > rsi_long_threshold and current_15m['adx'] >= adx_15m_threshold)
        if bullish_15m:
            return 'long'
    return None

# Trading Functions
async def place_order(side, price, size_usdt):
    global entry_atr
    size_sol = size_usdt / price
    size_contracts = max(round(size_sol / lot_size), 1)
    response = await trade_api.place_order(  # Already async
        instId=instId, tdMode='cross', side='buy' if side == 'long' else 'sell',
        posSide=side, ordType='market', sz=str(size_contracts)
    )
    if response['code'] == '0':
        size_sol = size_contracts * lot_size
        alert = f"🎉 GoodBoyTrader jumps in! {side.capitalize()} at {price:.2f} 🚀 Size: {size_sol:.4f} SOL 🌞 Let’s ride the wave!"
        await send_telegram_alert(alert)
        return response['data'][0]['ordId'], size_sol
    else:
        logging.error(f"Order failed: {response.get('msg', 'Unknown error')}")
        return None, 0

async def close_order(side, price, size_sol, exit_type=''):
    size_contracts = round(size_sol / lot_size)
    response = await trade_api.place_order(  # Already async
        instId=instId, tdMode='cross', side=side,
        posSide='long' if side == 'sell' else 'short',
        ordType='market', sz=str(size_contracts)
    )
    if response['code'] == '0':
        logging.info(f"Closed {size_sol:.4f} SOL at {price:.2f} ({exit_type})")
        return True
    else:
        logging.error(f"Close order failed: {response.get('msg', 'Unknown error')}")
        return False

async def get_current_price():
    try:
        response = market_api.get_ticker(instId=instId)
        if response['code'] == '0' and response['data']:
            return float(response['data'][0]['last'])
        logging.error(f"Failed to fetch price: {response.get('msg', 'Unknown error')}")
        return None
    except Exception as e:
        logging.error(f"Error fetching price: {e}")
        return None

# Position Monitoring
async def monitor_position(position, entry_price, trade):
    global position_state, entry_atr
    size_sol = trade['size_sol']
    df_15m = await fetch_recent_data(timeframe='15m', limit='100')
    if df_15m.empty:
        return
    entry_atr = df_15m['atr'].iloc[-1]
    atr_mean = df_15m['atr_mean'].iloc[-1]
    stop_loss = entry_price * (1 - stop_loss_pct) if position == 'long' else entry_price * (1 + stop_loss_pct)
    tp1_price = entry_price + (entry_atr * 1.5) if position == 'long' else entry_price - (entry_atr * 1.5)
    atr_multiplier = 2.0 if entry_atr < atr_mean else 2.8
    tp2_price = entry_price + (entry_atr * atr_multiplier) if position == 'long' else entry_price - (entry_atr * atr_multiplier)
    sl_adjusted = False
    logging.info(f"SL: {stop_loss:.2f}, TP1: {tp1_price:.2f}, TP2: {tp2_price:.2f}, TSL Factor: {trailing_stop_factor} × ATR")
    while position_state == position:
        current_price = await get_current_price()
        if not current_price:
            await asyncio.sleep(5)
            continue
        current_price -= current_price * SLIPPAGE if position == 'long' else -current_price * SLIPPAGE
        trailing_sl = entry_price + (entry_atr * trailing_stop_factor) if position == 'long' else entry_price - (entry_atr * trailing_stop_factor)
        if not sl_adjusted and ((position == 'long' and current_price >= tp1_price) or (position == 'short' and current_price <= tp1_price)):
            sl_adjusted = True
            stop_loss = entry_price
            alert = f"🏆 Woo-hoo! {position.capitalize()} hit TP1 at {tp1_price:.2f} 🎯 SL now at breakeven {entry_price:.2f} 😎 Safe zone activated!"
            await send_telegram_alert(alert)
        if (position == 'long' and current_price <= stop_loss) or (position == 'short' and current_price >= stop_loss):
            if await close_order('sell' if position == 'long' else 'buy', current_price, size_sol, 'Stop Loss'):
                trade['exit_time'], trade['exit_price'], trade['exit_type'] = datetime.now(), current_price, 'Stop Loss'
                tracker.update(trade)
                alert = f"😿 Oof! Stop Loss triggered at {current_price:.2f} for {position.capitalize()} 💥 Better luck next time, champ!"
                await send_telegram_alert(alert)
                position_state = None
                clear_trade_state()
                return
        elif (position == 'long' and current_price >= tp2_price) or (position == 'short' and current_price <= tp2_price):
            if await close_order('sell' if position == 'long' else 'buy', current_price, size_sol, 'Take Profit 2'):
                trade['exit_time'], trade['exit_price'], trade['exit_type'] = datetime.now(), current_price, 'Take Profit 2'
                tracker.update(trade)
                alert = f"💰 Jackpot! {position.capitalize()} cashed out at TP2 {current_price:.2f} 🎉 GoodBoyTrader strikes gold! 🥳"
                await send_telegram_alert(alert)
                position_state = None
                clear_trade_state()
                return
        elif (position == 'long' and current_price <= trailing_sl) or (position == 'short' and current_price >= trailing_sl):
            if await close_order('sell' if position == 'long' else 'buy', current_price, size_sol, 'Trailing Stop'):
                trade['exit_time'], trade['exit_price'], trade['exit_type'] = datetime.now(), current_price, 'Trailing Stop'
                tracker.update(trade)
                alert = f"🏃‍♂️ Trailing Stop kicked in at {current_price:.2f} for {position.capitalize()}! 🐾 GoodBoyTrader locked in profits! 💪"
                await send_telegram_alert(alert)
                position_state = None
                clear_trade_state()
                return
        await asyncio.sleep(10)

# Main Function
async def main():
    global position_state, trade
    print("DEBUG: Entering main()")  # Debug
    logging.info("✅ Bot Started Successfully!")
    print("DEBUG: Before Telegram alert")  # Debug
    try:
        await asyncio.wait_for(send_telegram_alert(startup_message), timeout=10)  # 10s timeout
        print("DEBUG: Telegram alert sent")  # Debug
    except asyncio.TimeoutError:
        print("DEBUG: Telegram alert timed out, continuing anyway")
        logging.error("⚠️ Telegram alert timed out, proceeding without notification")
    except Exception as e:
        print(f"DEBUG: Telegram alert failed: {e}")
        logging.error(f"⚠️ Telegram alert failed: {e}, proceeding anyway")
    
    position_state, trade = load_trade_state()
    print("DEBUG: After load_trade_state()")  # Debug
    if position_state:
        logging.info(f"Resuming existing {position_state} position from {trade['entry_time']}")
        asyncio.create_task(monitor_position(position_state, trade['entry_price'], trade))
    while True:
        try:
            logging.info("🔄 Checking for trade signals...")
            print("DEBUG: Fetching 4H data")  # Debug
            df_4h = await fetch_recent_data(timeframe='4H', limit='400')
            print("DEBUG: Fetching 15m data")  # Debug
            df_15m = await fetch_recent_data(timeframe='15m', limit='100')
            if df_4h.empty or len(df_4h) < ema_long_period or df_15m.empty or len(df_15m) < ema_long_period:
                logging.warning("⚠️ Insufficient data, waiting...")
                await asyncio.sleep(60)
                continue
            print("DEBUG: Getting current price")  # Debug
            entry_price = await get_current_price()
            if not entry_price:
                logging.warning("⚠️ Failed to fetch current price, retrying...")
                await asyncio.sleep(60)
                continue
            if position_state is None:
                signal = check_entry(df_4h, df_15m)
                if signal in ['long', 'short']:
                    order_id, size_sol = await place_order(signal, entry_price, base_trade_size_usdt)
                    if order_id:
                        trade = {'entry_time': datetime.now(), 'entry_price': entry_price, 'side': signal, 'size_sol': size_sol}
                        position_state = signal
                        save_trade_state(trade, position_state)
                        asyncio.create_task(monitor_position(position_state, entry_price, trade))
            await asyncio.sleep(60)
        except Exception as e:
            logging.error(f"Main loop error: {str(e)}")
            await send_telegram_alert(f"🚨 Uh-oh! GoodBoyTrader hit a snag: {str(e)} 😵 Fixing it soon—stay tuned!")
            await asyncio.sleep(60)

if __name__ == "__main__":
    try:
        print("DEBUG: Starting asyncio.run(main())")  # Debug
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n❌ Bot Stopped by User")
