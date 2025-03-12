import pandas as pd
import ta
from datetime import datetime, timedelta
import logging
import numpy as np
import json
import os
import okx.MarketData as MarketData
import okx.Trade as Trade
import okx.Account as Account
import asyncio
import telegram
import time
import schedule
import threading
import sys
import pytz

# Custom logging handler to ensure flush
class FlushFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

# Logging Setup
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# File handler
file_handler = FlushFileHandler('okx_trading_bot.log')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

logging.info(f"Python version: {sys.version}")

# --- Security: Load credentials ---
API_KEY = os.getenv('OKX_API_KEY')
SECRET_KEY = os.getenv('OKX_SECRET_KEY')
PASSPHRASE = os.getenv('OKX_PASSPHRASE')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

# --- Validate credentials ---
required_vars = {'OKX_API_KEY': API_KEY, 'OKX_SECRET_KEY': SECRET_KEY, 'OKX_PASSPHRASE': PASSPHRASE, 
                'TELEGRAM_TOKEN': TELEGRAM_TOKEN, 'CHAT_ID': CHAT_ID}
for var_name, var_value in required_vars.items():
    if var_value is None or var_value == f'your_{var_name.lower()}':
        logging.error(f"{var_name} not set in environment variables.")
        print(f"⚠️ Error: {var_name} not set in environment variables.")
        sys.exit(1)
    if var_name == 'CHAT_ID' and not var_value.isdigit():
        logging.error(f"{var_name} must be a numeric chat ID.")
        print(f"⚠️ Error: {var_name} must be a numeric chat ID.")
        sys.exit(1)

# --- Initialize Telegram Bot ---
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# --- Trading Parameters ---
base_trade_size_usdt = 75   # 75 USDT
leverage = 5               # 5x leverage
symbol = "SOL-USDT-SWAP"
instId = "SOL-USDT-SWAP"
lot_size = 0.1             # OKX contract size for SOL-USDT-SWAP
SLIPPAGE = 0.002           # 0.2% slippage
FEES = 0.00075             # 0.075% fees
stop_loss_pct = 0.025      # 2.5% stop loss
trailing_stop_factor = 1.8 # 1.8 × ATR trailing stop

# --- Tunable Parameters ---
ema_short_period = 5
ema_mid_period = 20
ema_long_period = 100

# --- Timezone Setup ---
TIMEZONE = pytz.timezone('UTC')  # Change to your local timezone, e.g., 'America/New_York'

# --- Detailed Startup Message ---
startup_message = (
    f" 🚀 OKX Trading Bot Initialized - GoodBoyTrader 🌌\n"
    f"📅 Date: {datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    f"💰 Trade Size: {base_trade_size_usdt} USDT @ {leverage}x Leverage\n"
    f"🎯 Symbol: {symbol}\n"
    f"📊 Strategy: EMA {ema_short_period}/{ema_mid_period}/{ema_long_period}, Points-Based Entry\n"
    f"🛡️ Risk: {stop_loss_pct*100:.1f}% SL, {trailing_stop_factor}×ATR Trailing Stop\n"
    f"💸 Costs: {FEES*100:.3f}% Fees, {SLIPPAGE*100:.1f}% Slippage\n"
    f"📬 Notifications: Telegram to Chat ID {CHAT_ID}\n"
    f"⏰ Schedule: Checking every 5m + real-time triggers"
)
print(startup_message)
logging.info(startup_message)

# --- Global State ---
position_state = None
entry_atr = 0

# --- Utility Functions ---
async def send_telegram_alert(message):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message)
        logging.info(f"Telegram alert sent: {message}")
    except Exception as e:
        logging.error(f"Failed to send Telegram alert: {str(e)}")
        print(f"⚠️ Telegram alert failed: {str(e)}")

def fetch_with_retries(api_call, max_attempts=3):
    for attempt in range(max_attempts):
        try:
            response = api_call()
            if response['code'] != '0':
                raise Exception(f"API error: {response.get('msg', 'Unknown')}")
            return response
        except Exception as e:
            error_msg = f"Attempt {attempt + 1} failed: {str(e)}"
            print(f"⚠️ {error_msg}")
            logging.error(error_msg)
            if attempt < max_attempts - 1:
                time.sleep(5 * (attempt + 1))
            else:
                return None

# --- Trade Tracker ---
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
        pnl = (pnl_raw - total_cost) * leverage if not partial else (pnl_raw - (total_cost / 2)) * leverage
        self.total_pnl += pnl
        if not partial:
            self.trade_count += 1
            self.wins += 1 if pnl > 0 else 0
            self.losses += 1 if pnl < 0 else 0
        msg = f"Trade {'partial' if partial else 'completed'}. PnL: {pnl:.2f} USDT, Total: {self.total_pnl:.2f}"
        logging.info(msg)
        print(f" 💸 {'Partial' if partial else 'Trade'} PnL: {pnl:.2f} USDT 💰 Total: {self.total_pnl:.2f} USDT 🎲 Wins: {self.wins} 🏆 Losses: {self.losses}")

tracker = TradeTracker()

# --- State Management ---
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

# --- Indicator Calculations ---
def calculate_indicators(df, timeframe='4H'):
    if len(df) < ema_long_period:
        return df
    df['ema_5'] = ta.trend.ema_indicator(df['close'], window=ema_short_period)
    df['ema_20'] = ta.trend.ema_indicator(df['close'], window=ema_mid_period)
    df['ema_100'] = ta.trend.ema_indicator(df['close'], window=ema_long_period)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    df['adx'] = ta.trend.adx(df['high'], df['low'], df['close'], window=14)
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
    df['atr_mean'] = df['atr'].rolling(14).mean()
    macd = ta.trend.MACD(df['close'], window_slow=26, window_fast=12, window_sign=9)
    df['macd'] = macd.macd()
    df['macd_signal'] = macd.macd_signal()
    return df

# --- Data Fetching ---
def fetch_recent_data(timeframe='4H', limit='400'):
    response = fetch_with_retries(lambda: market_api.get_candlesticks(instId=instId, bar=timeframe, limit=limit))
    if not response:
        return pd.DataFrame()
    data = response['data'][::-1]
    df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'])
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
    df[['open', 'high', 'low', 'close', 'vol']] = df[['open', 'high', 'low', 'close', 'vol']].astype(float)
    return calculate_indicators(df, timeframe)

def get_current_price():
    response = fetch_with_retries(lambda: market_api.get_ticker(instId=instId))
    return float(response['data'][0]['last']) if response else None

# --- Entry Logic ---
def check_entry(df_4h, df_15m):
    global position_state
    if len(df_4h) < 2 or len(df_15m) < 1:
        return None, 0, 0, 0, 0

    # 4H Conditions
    current_4h = df_4h.iloc[-1]
    prev_4h = df_4h.iloc[-2]

    # 4H Short Conditions
    short_points_4h = 0
    ema_5_cross_short = prev_4h['ema_5'] > prev_4h['ema_100'] and current_4h['ema_5'] < current_4h['ema_100']
    ema_5_below = current_4h['ema_5'] < current_4h['ema_100']
    if ema_5_cross_short or ema_5_below:
        short_points_4h += 1
    ema_20_cross_short = prev_4h['ema_20'] > prev_4h['ema_100'] and current_4h['ema_20'] < current_4h['ema_100']
    ema_20_below = current_4h['ema_20'] < current_4h['ema_100']
    if ema_20_cross_short or ema_20_below:
        short_points_4h += 1
    rsi_short = current_4h['rsi'] < 40
    if rsi_short:
        short_points_4h += 1
    macd_short = current_4h['macd'] < 0 and current_4h['macd_signal'] < 0
    if macd_short:
        short_points_4h += 1

    # 4H Long Conditions
    long_points_4h = 0
    ema_5_cross_long = prev_4h['ema_5'] < prev_4h['ema_100'] and current_4h['ema_5'] > current_4h['ema_100']
    ema_5_above = current_4h['ema_5'] > current_4h['ema_100']
    if ema_5_cross_long or ema_5_above:
        long_points_4h += 1
    ema_20_cross_long = prev_4h['ema_20'] < prev_4h['ema_100'] and current_4h['ema_20'] > current_4h['ema_100']
    ema_20_above = current_4h['ema_20'] > current_4h['ema_100']
    if ema_20_cross_long or ema_20_above:
        long_points_4h += 1
    rsi_long = current_4h['rsi'] > 55
    if rsi_long:
        long_points_4h += 1
    macd_long = current_4h['macd'] > 0 and current_4h['macd_signal'] > 0
    if macd_long:
        long_points_4h += 1

    # 15m Conditions
    current_15m = df_15m.iloc[-1]

    # 15m Short Conditions
    short_points_15m = 0
    if current_15m['close'] < current_15m['ema_100']:
        short_points_15m += 1
    if current_15m['adx'] > 15:
        short_points_15m += 1
    if current_15m['rsi'] < 45:
        short_points_15m += 1

    # 15m Long Conditions
    long_points_15m = 0
    if current_15m['close'] > current_15m['ema_100']:
        long_points_15m += 1
    if 18 <= current_15m['adx'] <= 25:
        long_points_15m += 1
    if current_15m['rsi'] > 60:
        long_points_15m += 1

    signal = None
    if short_points_4h >= 3 and short_points_15m == 3:
        signal = 'short'
    elif long_points_4h >= 3 and long_points_15m == 3:
        signal = 'long'

    return signal, short_points_4h, long_points_4h, short_points_15m, long_points_15m

# --- Trading Functions ---
def place_order(side, price, size_usdt):
    global entry_atr
    size_sol = (size_usdt * leverage) / price
    size_contracts = max(round(size_sol / lot_size), 1)
    response = trade_api.place_order(
        instId=instId, tdMode='cross', side='buy' if side == 'long' else 'sell',
        posSide=side, ordType='market', sz=str(size_contracts)
    )
    if response['code'] == '0':
        size_sol = size_contracts * lot_size
        alert = f"🎉 GoodBoyTrader jumps in! {side.capitalize()} at {price:.2f} 🚀 Size: {size_sol:.4f} SOL 🌞 Let’s ride the wave!"
        asyncio.run(send_telegram_alert(alert))
        return response['data'][0]['ordId'], size_sol
    else:
        logging.error(f"Order failed: {response.get('msg', 'Unknown error')}")
        return None, 0

def close_order(side, price, size_sol, exit_type=''):
    size_contracts = round(size_sol / lot_size)
    response = trade_api.place_order(
        instId=instId, tdMode='cross', side=side,
        posSide='long' if side == 'sell' else 'short',
        ordType='market', sz=str(size_contracts)
    )
    if response['code'] == '0':
        msg = f" 🏁 Closed {size_sol:.4f} SOL at {price:.2f} ({exit_type})"
        print(msg)
        logging.info(msg)
        return True
    else:
        logging.error(f"Close order failed: {response.get('msg', 'Unknown error')}")
        return False

# --- Position Monitoring ---
def monitor_position(position, entry_price, trade):
    global position_state, entry_atr
    size_sol = trade['size_sol']
    
    df_15m = fetch_recent_data(timeframe='15m', limit='100')
    if df_15m.empty:
        return
    entry_atr = df_15m['atr'].iloc[-1]
    atr_mean = df_15m['atr_mean'].iloc[-1]
    stop_loss = entry_price * (1 - stop_loss_pct) if position == 'long' else entry_price * (1 + stop_loss_pct)
    tp1_price = entry_price + (entry_atr * 1.5) if position == 'long' else entry_price - (entry_atr * 1.5)
    atr_multiplier = 2.0 if entry_atr < atr_mean else 2.8
    tp2_price = entry_price + (entry_atr * atr_multiplier) if position == 'long' else entry_price - (entry_atr * atr_multiplier)
    sl_adjusted = False

    msg = f" 📉 SL: {stop_loss:.2f}, TP1: {tp1_price:.2f}, TP2: {tp2_price:.2f}, TSL Factor: {trailing_stop_factor} × ATR"
    print(msg)
    logging.info(msg)

    while position_state == position:
        current_price = get_current_price()
        if not current_price:
            time.sleep(5)
            continue

        current_price -= current_price * SLIPPAGE if position == 'long' else -current_price * SLIPPAGE

        trailing_sl = entry_price + (entry_atr * trailing_stop_factor) if position == 'long' else entry_price - (entry_atr * trailing_stop_factor)

        if not sl_adjusted and ((position == 'long' and current_price >= tp1_price) or (position == 'short' and current_price <= tp1_price)):
            sl_adjusted = True
            stop_loss = entry_price
            msg = f" 🎯 {position.capitalize()} hit TP1 at {tp1_price:.2f}, SL moved to breakeven ({entry_price:.2f})"
            print(msg)
            logging.info(msg)
            alert = f"🏆 Woo-hoo! {position.capitalize()} hit TP1 at {tp1_price:.2f} 🎯 SL now at breakeven {entry_price:.2f} 😎 Safe zone activated!"
            asyncio.run(send_telegram_alert(alert))

        if (position == 'long' and current_price <= stop_loss) or (position == 'short' and current_price >= stop_loss):
            if close_order('sell' if position == 'long' else 'buy', current_price, size_sol, 'Stop Loss'):
                trade['exit_time'], trade['exit_price'], trade['exit_type'] = datetime.now(TIMEZONE), current_price, 'Stop Loss'
                tracker.update(trade)
                alert = f"😿 Oof! Stop Loss triggered at {current_price:.2f} for {position.capitalize()} 💥 Better luck next time, champ!"
                asyncio.run(send_telegram_alert(alert))
                position_state = None
                clear_trade_state()
                return
        elif (position == 'long' and current_price >= tp2_price) or (position == 'short' and current_price <= tp2_price):
            if close_order('sell' if position == 'long' else 'buy', current_price, size_sol, 'Take Profit 2'):
                trade['exit_time'], trade['exit_price'], trade['exit_type'] = datetime.now(TIMEZONE), current_price, 'Take Profit 2'
                tracker.update(trade)
                alert = f"💰 Jackpot! {position.capitalize()} cashed out at TP2 {current_price:.2f} 🎉 GoodBoyTrader strikes gold! 🥳"
                asyncio.run(send_telegram_alert(alert))
                position_state = None
                clear_trade_state()
                return
        elif (position == 'long' and current_price <= trailing_sl) or (position == 'short' and current_price >= trailing_sl):
            if close_order('sell' if position == 'long' else 'buy', current_price, size_sol, 'Trailing Stop'):
                trade['exit_time'], trade['exit_price'], trade['exit_type'] = datetime.now(TIMEZONE), current_price, 'Trailing Stop'
                tracker.update(trade)
                alert = f"🏃‍♂️ Trailing Stop kicked in at {current_price:.2f} for {position.capitalize()}! 🐾 GoodBoyTrader locked in profits! 💪"
                asyncio.run(send_telegram_alert(alert))
                position_state = None
                clear_trade_state()
                return

        time.sleep(10)

# --- Real-Time Monitoring ---
def monitor_points():
    while True:
        try:
            df_4h = fetch_recent_data(timeframe='4H', limit='400')
            df_15m = fetch_recent_data(timeframe='15m', limit='100')
            if df_4h.empty or len(df_4h) < ema_long_period or df_15m.empty or len(df_15m) < ema_long_period:
                msg = "Insufficient data for monitoring..."
                print(msg)
                logging.info(msg)
                time.sleep(10)
                continue

            signal, short_points_4h, long_points_4h, short_points_15m, long_points_15m = check_entry(df_4h, df_15m)
            current_price = get_current_price() or "N/A"
            next_run = schedule.next_run().strftime('%Y-%m-%d %H:%M:%S %Z') if schedule.next_run() else "N/A"

            monitor_output = (
                f"📊 GoodBoyTrader - Real-Time Entry Points Monitor\n"
                f"⏰ Current Time: {datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                f"💰 Current Price: {current_price}\n"
                f"⏳ Next Scheduled Check: {next_run}\n"
                f"📈 Position State: {position_state or 'None'}\n"
                f"4H Points (Need ≥ 3):\n"
                f"  Short: {short_points_4h}/4\n"
                f"  Long:  {long_points_4h}/4\n"
                f"15m Points (Need = 3):\n"
                f"  Short: {short_points_15m}/3\n"
                f"  Long:  {long_points_15m}/3\n"
                f"Trade will trigger when: (4H Short ≥ 3 AND 15m Short = 3) OR (4H Long ≥ 3 AND 15m Long = 3)"
            )
            logging.info(monitor_output)
            print(monitor_output)

            # Real-time trade trigger
            if signal and position_state is None:
                logging.info(f"Real-time trigger: {signal} detected, executing trade now.")
                run_trading_logic()

            time.sleep(10)
        except Exception as e:
            msg = f"⚠️ Monitoring error: {str(e)}"
            print(msg)
            logging.error(msg)
            time.sleep(10)

# --- Trading Logic ---
def run_trading_logic():
    global position_state, trade
    try:
        df_4h = fetch_recent_data(timeframe='4H', limit='400')
        df_15m = fetch_recent_data(timeframe='15m', limit='100')
        if df_4h.empty or len(df_4h) < ema_long_period or df_15m.empty or len(df_15m) < ema_long_period:
            msg = "Insufficient data, skipping this check..."
            print(msg)
            logging.info(msg)
            return

        entry_price = get_current_price()
        if not entry_price:
            msg = "Failed to get current price, skipping this check..."
            print(msg)
            logging.info(msg)
            return

        trade_size_usdt = base_trade_size_usdt

        if position_state is None:
            signal, short_points_4h, long_points_4h, short_points_15m, long_points_15m = check_entry(df_4h, df_15m)
            logging.info(f"Trade Check - Signal: {signal}, 4H Short/Long: {short_points_4h}/{long_points_4h}, 15m Short/Long: {short_points_15m}/{long_points_15m}")
            if signal in ['long', 'short']:
                msg = f"Trade Signal: {signal.capitalize()} at {entry_price} (4H: {short_points_4h if signal == 'short' else long_points_4h}, 15m: {short_points_15m if signal == 'short' else long_points_15m})"
                logging.info(msg)
                print(msg)
                order_id, size_sol = place_order(signal, entry_price, trade_size_usdt)
                if order_id:
                    trade = {'entry_time': datetime.now(TIMEZONE), 'entry_price': entry_price, 'side': signal, 'size_sol': size_sol}
                    position_state = signal
                    save_trade_state(trade, position_state)
                    monitor_position(position_state, entry_price, trade)
    except Exception as e:
        logging.error(f"Trading logic error: {str(e)}")
        alert = f"🚨 Uh-oh! GoodBoyTrader hit a snag: {str(e)} 😵 Fixing it soon—stay tuned!"
        asyncio.run(send_telegram_alert(alert))

# --- Initialization ---
market_api = MarketData.MarketAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')
trade_api = Trade.TradeAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')
account_api = Account.AccountAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')
account_api.set_position_mode(posMode="long_short_mode")
account_api.set_leverage(instId=instId, lever=str(leverage), mgnMode="cross")

# --- Schedule Setup ---
schedule.every(5).minutes.do(run_trading_logic)  # Check every 5 minutes

# --- Main Loop ---
position_state, trade = load_trade_state()
msg = "Starting scheduler and monitor... Waiting for triggers."
print(msg)
logging.info(msg)
logging.info(f"System time: {datetime.now(TIMEZONE).isoformat()} (Timezone: {TIMEZONE})")

# Start the points monitor in a separate thread
monitor_thread = threading.Thread(target=monitor_points, daemon=True)
monitor_thread.start()

# Run the scheduler
while True:
    schedule.run_pending()
    logging.info("Heartbeat: Scheduler running...")
    time.sleep(1)

