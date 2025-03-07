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
import telegram  # Back to Telegram
import time

# --- Security: Load credentials ---
API_KEY = os.getenv('OKX_API_KEY', 'your_okx_api_key')
SECRET_KEY = os.getenv('OKX_SECRET_KEY', 'your_okx_secret_key')
PASSPHRASE = os.getenv('OKX_PASSPHRASE', 'your_okx_passphrase')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'your_telegram_bot_token')
CHAT_ID = os.getenv('CHAT_ID', 'your_chat_id')

# --- Validate credentials ---
required_vars = {'OKX_API_KEY': API_KEY, 'OKX_SECRET_KEY': SECRET_KEY, 'OKX_PASSPHRASE': PASSPHRASE, 
                'TELEGRAM_TOKEN': TELEGRAM_TOKEN, 'CHAT_ID': CHAT_ID}
for var_name, var_value in required_vars.items():
    if var_name == 'CHAT_ID' and not var_value.isdigit() and var_value == f'your_{var_name.lower()}':
        print(f"⚠️ Error: {var_name} must be a numeric chat ID or properly set.")
        exit(1)
    if var_value == f'your_{var_name.lower()}':
        print(f"⚠️ Error: {var_name} not set in environment variables.")
        exit(1)

# --- Initialize Telegram Bot ---
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# --- Logging Setup ---
logging.basicConfig(filename='okx_trading_bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Trading Parameters (From Backtest) ---
base_trade_size_usdt = 50   # Set to 50 USDT
leverage = 5               # From backtest
symbol = "SOL-USDT-SWAP"
instId = "SOL-USDT-SWAP"
lot_size = 0.1             # OKX contract size for SOL-USDT-SWAP
SLIPPAGE = 0.002           # 0.2% slippage from backtest update
FEES = 0.00075             # 0.075% fees from backtest update
stop_loss_pct = 0.025      # 2.5% stop loss from backtest
trailing_stop_factor = 1.8 # 1.8 × ATR trailing stop from backtest

# --- Tunable Parameters (From Backtest) ---
ema_short_period = 5
ema_mid_period = 20
ema_long_period = 100
rsi_long_threshold = 55
rsi_short_threshold = 45
adx_4h_threshold = 12
adx_15m_threshold = 15

# --- Detailed Startup Message ---
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
        fees = (entry_value + exit_value) * FEES  # 0.075% fee
        slippage_cost = entry_value * SLIPPAGE * 2  # 0.2% slippage on entry/exit
        total_cost = fees + slippage_cost
        pnl_raw = (trade['exit_price'] - trade['entry_price']) * size * (1 if trade['side'] == 'long' else -1)
        pnl = pnl_raw - total_cost if not partial else pnl_raw - (total_cost / 2)
        self.total_pnl += pnl
        if not partial:
            self.trade_count += 1
            self.wins += 1 if pnl > 0 else 0
            self.losses += 1 if pnl < 0 else 0
        logging.info(f"Trade {'partial' if partial else 'completed'}. PnL: {pnl:.2f} USDT, Total: {self.total_pnl:.2f}")
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
    df['ema_short'] = ta.trend.ema_indicator(df['close'], window=ema_short_period)  # EMA 5
    df['ema_mid'] = ta.trend.ema_indicator(df['close'], window=ema_mid_period)      # EMA 20
    df['ema_long'] = ta.trend.ema_indicator(df['close'], window=ema_long_period)    # EMA 100
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)                            # RSI 14
    df['adx'] = ta.trend.adx(df['high'], df['low'], df['close'], window=14)        # ADX 14
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)  # ATR 14
    df['atr_mean'] = df['atr'].rolling(14).mean()
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

# --- Trading Functions ---
def place_order(side, price, size_usdt):
    global entry_atr
    size_sol = size_usdt / price
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
        print(f" 🏁 Closed {size_sol:.4f} SOL at {price:.2f} ({exit_type})")
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

    print(f" 📉 SL: {stop_loss:.2f}, TP1: {tp1_price:.2f}, TP2: {tp2_price:.2f}, TSL Factor: {trailing_stop_factor} × ATR")

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
            print(f" 🎯 {position.capitalize()} hit TP1 at {tp1_price:.2f}, SL moved to breakeven ({entry_price:.2f})")
            alert = f"🏆 Woo-hoo! {position.capitalize()} hit TP1 at {tp1_price:.2f} 🎯 SL now at breakeven {entry_price:.2f} 😎 Safe zone activated!"
            asyncio.run(send_telegram_alert(alert))

        if (position == 'long' and current_price <= stop_loss) or (position == 'short' and current_price >= stop_loss):
            if close_order('sell' if position == 'long' else 'buy', current_price, size_sol, 'Stop Loss'):
                trade['exit_time'], trade['exit_price'], trade['exit_type'] = datetime.now(), current_price, 'Stop Loss'
                tracker.update(trade)
                alert = f"😿 Oof! Stop Loss triggered at {current_price:.2f} for {position.capitalize()} 💥 Better luck next time, champ!"
                asyncio.run(send_telegram_alert(alert))
                position_state = None
                clear_trade_state()
                return
        elif (position == 'long' and current_price >= tp2_price) or (position == 'short' and current_price <= tp2_price):
            if close_order('sell' if position == 'long' else 'buy', current_price, size_sol, 'Take Profit 2'):
                trade['exit_time'], trade['exit_price'], trade['exit_type'] = datetime.now(), current_price, 'Take Profit 2'
                tracker.update(trade)
                alert = f"💰 Jackpot! {position.capitalize()} cashed out at TP2 {current_price:.2f} 🎉 GoodBoyTrader strikes gold! 🥳"
                asyncio.run(send_telegram_alert(alert))
                position_state = None
                clear_trade_state()
                return
        elif (position == 'long' and current_price <= trailing_sl) or (position == 'short' and current_price >= trailing_sl):
            if close_order('sell' if position == 'long' else 'buy', current_price, size_sol, 'Trailing Stop'):
                trade['exit_time'], trade['exit_price'], trade['exit_type'] = datetime.now(), current_price, 'Trailing Stop'
                tracker.update(trade)
                alert = f"🏃‍♂️ Trailing Stop kicked in at {current_price:.2f} for {position.capitalize()}! 🐾 GoodBoyTrader locked in profits! 💪"
                asyncio.run(send_telegram_alert(alert))
                position_state = None
                clear_trade_state()
                return

        time.sleep(10)

# --- Initialization ---
market_api = MarketData.MarketAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')
trade_api = Trade.TradeAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')
account_api = Account.AccountAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')
account_api.set_position_mode(posMode="long_short_mode")
account_api.set_leverage(instId=instId, lever=str(leverage), mgnMode="cross")

# --- Main Loop (Live Trading) ---
position_state, trade = load_trade_state()
while True:
    try:
        df_4h = fetch_recent_data(timeframe='4H', limit='400')
        df_15m = fetch_recent_data(timeframe='15m', limit='100')
        if df_4h.empty or len(df_4h) < ema_long_period or df_15m.empty or len(df_15m) < ema_long_period:
            print("Insufficient data, waiting...")
            time.sleep(60)
            continue

        entry_price = get_current_price()
        if not entry_price:
            time.sleep(60)
            continue

        trade_size_usdt = base_trade_size_usdt  # Fixed at 50 USDT

        if position_state is None:
            signal = check_entry(df_4h, df_15m)
            if signal in ['long', 'short']:
                order_id, size_sol = place_order(signal, entry_price, trade_size_usdt)
                if order_id:
                    trade = {'entry_time': datetime.now(), 'entry_price': entry_price, 'side': signal, 'size_sol': size_sol}
                    position_state = signal
                    save_trade_state(trade, position_state)
                    monitor_position(position_state, entry_price, trade)

        time.sleep(60)  # Check every minute
    except Exception as e:
        logging.error(f"Main loop error: {str(e)}")
        alert = f"🚨 Uh-oh! GoodBoyTrader hit a snag: {str(e)} 😵 Fixing it soon—stay tuned!"
        asyncio.run(send_telegram_alert(alert))
        time.sleep(60)

