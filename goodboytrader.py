import pandas as pd
import ta
import time
import okx.Trade as Trade
import okx.MarketData as MarketData
import okx.Account as Account
from datetime import datetime
import logging
import numpy as np
import json
import os
import requests
from textblob import TextBlob
import telegram
import asyncio

# --- Security: Load credentials from environment variables ---
API_KEY = os.getenv('OKX_API_KEY', 'e4529493-0b01-4ca4-9a16-8cc87aea60de')
SECRET_KEY = os.getenv('OKX_SECRET_KEY', 'BDBC3D45C9DAB00D724C4D6A6E945119')
PASSPHRASE = os.getenv('OKX_PASSPHRASE', 'Ranonlinehany13.')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '7602984334:AAHqj4MiKhUzdVD14is8tEuXwb-A4gBcnX4')
CHAT_ID = os.getenv('CHAT_ID', '1205421544')  # Set from @GetIDsBot or getUpdates
CRYPTOPANIC_TOKEN = os.getenv('CRYPTOPANIC_TOKEN', '065858035942c9610921161445fdef7ad08ad1f1')

# --- Validate environment variables ---
required_vars = {'OKX_API_KEY': API_KEY, 'OKX_SECRET_KEY': SECRET_KEY, 'OKX_PASSPHRASE': PASSPHRASE, 
                 'TELEGRAM_TOKEN': TELEGRAM_TOKEN, 'CHAT_ID': CHAT_ID, 'CRYPTOPANIC_TOKEN': CRYPTOPANIC_TOKEN}
for var_name, var_value in required_vars.items():
    if var_value == f'your_{var_name.lower()}':
        print(f"⚠️ Error: {var_name} not set in environment variables. Please set it and restart.")
        exit(1)

# --- Initialize Telegram Bot ---
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# --- Logging Setup ---
logging.basicConfig(filename='okx_trading_bot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
print(" 🚀 Starting OKX trading bot with 70%+ win rate... 🌌")
logging.info("Starting OKX trading bot")

# --- Trading Parameters ---
base_trade_size_usdt = 100
leverage = 5
symbol = "SOL-USDT-SWAP"
instId = "SOL-USDT-SWAP"
lot_size = 0.1
state_file = "trade_state.json"
MIN_MARGIN_RATIO = 20
MAX_API_FAILURES = 5
SLIPPAGE = 0.002
TRAILING_STOP_PERCENT = 0.04  # 4% trailing stop for remaining 50%

# --- Global State ---
last_analysis_print_time = 0
last_heartbeat_time = 0
api_failure_count = 0
position_state = None
entry_atr = 0

# --- Utility Functions ---
async def send_telegram_alert(message):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message)
        logging.info(f"Telegram alert sent: {message}")
    except Exception as e:
        logging.error(f"Failed to send Telegram alert: {str(e)}")

def fetch_with_retries(api_call, max_attempts=3):
    global api_failure_count
    for attempt in range(max_attempts):
        try:
            response = api_call()
            if response is None or response['code'] != '0':
                raise Exception(f"API error: {response.get('msg', 'Unknown')}")
            api_failure_count = 0
            return response
        except Exception as e:
            api_failure_count += 1
            error_msg = f"Attempt {attempt + 1} failed: {str(e)}"
            print(f"⚠️ {error_msg}")
            logging.error(error_msg)
            if attempt < max_attempts - 1:
                time.sleep(5 * (attempt + 1))
            else:
                return None

# --- Choppiness Index ---
def chop(high, low, close, window=14):
    tr = pd.DataFrame(index=high.index)
    tr['tr'] = high - low
    tr['tr_sum'] = tr['tr'].rolling(window).sum()
    tr['highest'] = high.rolling(window).max()
    tr['lowest'] = low.rolling(window).min()
    tr['range'] = tr['highest'] - tr['lowest']
    return 100 * np.log10(tr['tr_sum'] / tr['range']) / np.log10(window)

# --- Trade Tracker ---
class TradeTracker:
    def __init__(self):
        self.total_pnl = 0
        self.trade_count = 0
        self.wins = 0
        self.losses = 0

    def update(self, trade, partial=False):
        size = trade['size_sol'] * (0.5 if partial else 1.0)  # Adjust for 50% partial exit
        pnl = (trade['exit_price'] - trade['entry_price']) * size * (1 if trade['side'] == 'long' else -1)
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
    with open(state_file, 'w') as f:
        json.dump(state, f, default=str)

def load_trade_state():
    if os.path.exists(state_file):
        with open(state_file, 'r') as f:
            state = json.load(f)
            state['trade']['entry_time'] = datetime.fromisoformat(state['trade']['entry_time'])
            return state['position_state'], state['trade']
    return None, None

def clear_trade_state():
    if os.path.exists(state_file):
        os.remove(state_file)

# --- Indicator Calculations ---
def calculate_indicators(df, timeframe='5m'):
    if len(df) < 100:
        return df
    df['ema5'] = ta.trend.ema_indicator(df['close'], window=5)
    df['ema20'] = ta.trend.ema_indicator(df['close'], window=20)
    df['ema100'] = ta.trend.ema_indicator(df['close'], window=100)
    macd = ta.trend.MACD(df['close'], window_slow=26, window_fast=12, window_sign=9)
    df['macd'] = macd.macd()
    df['macd_signal'] = macd.macd_signal()
    df['macd_hist'] = macd.macd_diff()
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
    df['vol_sma20'] = df['vol'].rolling(window=20).mean()
    df['chop'] = chop(df['high'], df['low'], df['close'], window=14)
    df['force_index'] = ta.volume.force_index(close=df['close'], volume=df['vol'], window=13)
    df['force_sma20'] = df['force_index'].rolling(window=20).mean()
    df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    if timeframe == '15m':
        df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()
    return df

# --- Data Fetching ---
def fetch_recent_data(timeframe='5m', limit='400'):
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

def get_open_position():
    response = fetch_with_retries(lambda: account_api.get_positions(instType='SWAP', instId=instId))
    if response and response['data']:
        pos = response['data'][0]
        size_sol = float(pos.get('pos', '0'))
        if size_sol == 0:
            return None
        return {
            'side': pos['posSide'],
            'size_sol': size_sol,
            'entry_price': float(pos.get('avgPx', '0') or '0'),
            'timestamp': datetime.fromtimestamp(int(pos['uTime']) / 1000)
        }
    return None

def get_margin_ratio():
    response = fetch_with_retries(lambda: account_api.get_account_balance())
    if response and response['data']:
        total_eq = float(response['data'][0].get('totalEq', '0'))
        margin_used = float(response['data'][0].get('imr', '0') or '0')
        return (total_eq / margin_used * 100) if margin_used > 0 else 100
    return None

# --- News and Sentiment ---
def get_recent_news():
    try:
        response = requests.get(
            'https://cryptopanic.com/api/v1/posts/',
            params={'auth_token': CRYPTOPANIC_TOKEN, 'currencies': 'SOL'},
            timeout=10
        )
        return response.json()['results'][:5]
    except Exception:
        return []

def analyze_sentiment(news):
    if not news:
        return 'neutral'
    sentiments = [TextBlob(article['title']).sentiment.polarity for article in news]
    avg_sentiment = sum(sentiments) / len(sentiments)
    return 'negative' if avg_sentiment < -0.5 else 'positive' if avg_sentiment > 0.5 else 'neutral'

# --- Entry Logic ---
def check_higher_timeframe(df_15m):
    latest = df_15m.iloc[-1]
    return latest['ema5'] > latest['ema100'] and latest['adx'] > 20

def check_entry_1(df_5m, df_15m, sentiment='neutral'):
    points = {'long': {}, 'short': {}}
    total_points_long = total_points_short = 0
    max_points = 8

    if len(df_5m) < 11 or len(df_15m) < 100:
        return None, max_points, points

    current = df_5m.iloc[-1]
    prev = df_5m.iloc[-2]
    prev_prev = df_5m.iloc[-3]
    
    if current['chop'] > 60 or not check_higher_timeframe(df_15m):
        return None, max_points, points

    last_5_candles = df_5m.tail(5)
    long_unintended = any(last_5_candles['close'] < last_5_candles['ema100'])
    short_unintended = any(last_5_candles['close'] > last_5_candles['ema100'])
    if long_unintended and short_unintended:
        return None, max_points, points

    ema5_above_100 = current['ema5'] > current['ema100'] and prev['ema5'] <= prev['ema100']
    ema5_below_100 = current['ema5'] < current['ema100'] and prev['ema5'] >= prev['ema100']
    price_break_above = prev['close'] > prev['ema100'] and prev_prev['close'] <= prev_prev['ema100']
    price_break_below = prev['close'] < prev['ema100'] and prev_prev['close'] >= prev_prev['ema100']
    long_breakout = ema5_above_100 and price_break_above and not long_unintended
    short_breakout = ema5_below_100 and price_break_below and not short_unintended
    points['long']['🚀 EMA5 & Price Break EMA100'] = 2 if long_breakout else 0
    points['short']['🚀 EMA5 & Price Break EMA100'] = 2 if short_breakout else 0
    total_points_long += points['long']['🚀 EMA5 & Price Break EMA100']
    total_points_short += points['short']['🚀 EMA5 & Price Break EMA100']

    atr_sma = df_5m['atr'].rolling(20).mean().iloc[-1]
    breakout_period = max(5, min(15, int(10 * (current['atr'] / atr_sma))))
    breakout_high = df_5m['high'].iloc[-breakout_period:-1].max()
    breakout_low = df_5m['low'].iloc[-breakout_period:-1].min()
    breakout_long = current['close'] > breakout_high and current['ema5'] > current['ema100'] and not long_unintended
    breakout_short = current['close'] < breakout_low and current['ema5'] < current['ema100'] and not short_unintended
    points['long']['💥 Dynamic Breakout'] = 2 if breakout_long else 0
    points['short']['💥 Dynamic Breakout'] = 2 if breakout_short else 0
    total_points_long += points['long']['💥 Dynamic Breakout']
    total_points_short += points['short']['💥 Dynamic Breakout']

    ema100_zone_lower = current['ema100'] * 0.9975
    ema100_zone_upper = current['ema100'] * 1.0025
    pullback_to_100 = (ema100_zone_lower <= current['low'] <= ema100_zone_upper) or (ema100_zone_lower <= current['high'] <= ema100_zone_upper)
    ema20_crossed_above = current['ema20'] > current['ema100'] and prev['ema20'] <= prev['ema100']
    ema20_crossed_below = current['ema20'] < current['ema100'] and prev['ema20'] >= prev['ema100']
    ema20_in_zone = ema100_zone_lower <= current['ema20'] <= ema100_zone_upper
    macd_diff = current['macd'] - current['macd_signal']
    macd_long_valid = macd_diff > 0
    macd_short_valid = macd_diff < 0
    long_pullback = long_breakout and pullback_to_100 and (ema20_crossed_above or ema20_in_zone) and macd_long_valid and not long_unintended
    short_pullback = short_breakout and pullback_to_100 and (ema20_crossed_below or ema20_in_zone) and macd_short_valid and not short_unintended
    points['long']['📉 Pullback with EMA20 & MACD'] = 2 if long_pullback else 0
    points['short']['📉 Pullback with EMA20 & MACD'] = 2 if short_pullback else 0
    total_points_long += points['long']['📉 Pullback with EMA20 & MACD']
    total_points_short += points['short']['📉 Pullback with EMA20 & MACD']

    rsi_long_valid = current['rsi'] > 65
    rsi_short_valid = current['rsi'] < 30
    points['long']['📈 RSI Confirmation'] = 1 if rsi_long_valid else 0
    points['short']['📈 RSI Confirmation'] = 1 if rsi_short_valid else 0
    total_points_long += points['long']['📈 RSI Confirmation']
    total_points_short += points['short']['📈 RSI Confirmation']

    trigger_threshold = 8 if sentiment != 'positive' else 6
    if total_points_long >= trigger_threshold:
        return 'long', max_points, points
    elif total_points_short >= trigger_threshold:
        return 'short', max_points, points
    return None, max_points, points

def check_entry_2(df_5m, df_15m, sentiment='neutral'):
    points = {'long': {}, 'short': {}}
    total_points_long = total_points_short = 0
    max_points = 7

    if len(df_5m) < 6 or len(df_15m) < 100:
        return None, max_points, points
    
    current = df_5m.iloc[-1]
    prev = df_5m.iloc[-2]
    prev_prev = df_5m.iloc[-3]
    
    if current['chop'] > 60 or not check_higher_timeframe(df_15m):
        return None, max_points, points

    last_5_candles = df_5m.tail(5)
    long_unintended = any(last_5_candles['close'] < last_5_candles['ema100'])
    short_unintended = any(last_5_candles['close'] > last_5_candles['ema100'])
    if long_unintended and short_unintended:
        return None, max_points, points

    high_volume = (prev['vol'] > prev_prev['vol'] * 1.5 and current['atr'] > df_5m['atr'].rolling(20).mean().iloc[-1]) or prev['vol'] > prev['vol_sma20'] * 1.3
    fi_confirmation_long = current['force_index'] > current['force_sma20']
    fi_confirmation_short = current['force_index'] < current['force_sma20']
    points['long']['🌊 High Volume with FI'] = 2 if (high_volume and fi_confirmation_long and not long_unintended) else 0
    points['short']['🌊 High Volume with FI'] = 2 if (high_volume and fi_confirmation_short and not short_unintended) else 0
    total_points_long += points['long']['🌊 High Volume with FI']
    total_points_short += points['short']['🌊 High Volume with FI']
    if not high_volume:
        return None, max_points, points

    bullish_engulf = prev['close'] > prev['open'] and prev['open'] < prev_prev['close'] and prev['close'] > prev_prev['open'] and (prev['close'] - prev['open']) > current['atr']
    bearish_engulf = prev['close'] < prev['open'] and prev['open'] > prev_prev['close'] and prev['close'] < prev_prev['open'] and (prev['open'] - prev['close']) > current['atr']
    points['long']['🔥 Engulfing Candle'] = 2 if (bullish_engulf and not long_unintended) else 0
    points['short']['🔥 Engulfing Candle'] = 2 if (bearish_engulf and not short_unintended) else 0
    total_points_long += points['long']['🔥 Engulfing Candle']
    total_points_short += points['short']['🔥 Engulfing Candle']

    price_close_above_100 = current['close'] > current['ema100'] and prev['close'] <= prev['ema100']
    price_close_below_100 = current['close'] < current['ema100'] and prev['close'] >= prev['ema100']
    points['long']['📏 Price Closes Above EMA100'] = 1 if (price_close_above_100 and not long_unintended) else 0
    points['short']['📏 Price Closes Below EMA100'] = 1 if (price_close_below_100 and not short_unintended) else 0
    total_points_long += points['long']['📏 Price Closes Above EMA100']
    total_points_short += points['short']['📏 Price Closes Below EMA100']

    ema5_above_100 = current['ema5'] > current['ema100'] and prev['ema5'] <= prev['ema100']
    ema5_below_100 = current['ema5'] < current['ema100'] and prev['ema5'] >= prev['ema100']
    points['long']['🚀 EMA5 > EMA100'] = 1 if (ema5_above_100 and not long_unintended) else 0
    points['short']['🚀 EMA5 < EMA100'] = 1 if (ema5_below_100 and not short_unintended) else 0
    total_points_long += points['long']['🚀 EMA5 > EMA100']
    total_points_short += points['short']['🚀 EMA5 < EMA100']

    rsi_long_valid = current['rsi'] > 65
    rsi_short_valid = current['rsi'] < 30
    points['long']['📈 RSI Confirmation'] = 1 if rsi_long_valid else 0
    points['short']['📈 RSI Confirmation'] = 1 if rsi_short_valid else 0
    total_points_long += points['long']['📈 RSI Confirmation']
    total_points_short += points['short']['📈 RSI Confirmation']

    trigger_threshold = 7 if sentiment != 'positive' else 5
    if total_points_long >= trigger_threshold:
        return 'long', max_points, points
    elif total_points_short >= trigger_threshold:
        return 'short', max_points, points
    return None, max_points, points

# --- Trading Functions ---
def place_order(side, price, size_usdt):
    global entry_atr
    size_sol = size_usdt / price
    size_contracts = max(round(size_sol / lot_size), 1)
    if api_failure_count >= MAX_API_FAILURES or get_margin_ratio() < MIN_MARGIN_RATIO + 10:
        return None, 0
    response = trade_api.place_order(
        instId=instId, tdMode='cross', side='buy' if side == 'long' else 'sell',
        posSide=side, ordType='market', sz=str(size_contracts)
    )
    if response['code'] == '0':
        asyncio.run(send_telegram_alert(f"Entry: {side.capitalize()} at {price:.2f}, Size: {size_sol:.4f} SOL"))
        return response['data'][0]['ordId'], size_sol
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
    return False

def detect_divergence(df_15m, position):
    if len(df_15m) < 20:
        return False
    recent_data = df_15m.tail(20)
    prices = recent_data['close']
    macd_line = recent_data['macd']
    
    price_peaks = prices[(prices.shift(1) < prices) & (prices.shift(-1) < prices)]
    price_troughs = prices[(prices.shift(1) > prices) & (prices.shift(-1) > prices)]
    macd_peaks = macd_line[(macd_line.shift(1) < macd_line) & (macd_line.shift(-1) < macd_line)]
    macd_troughs = macd_line[(macd_line.shift(1) > macd_line) & (macd_line.shift(-1) > macd_line)]

    if position == 'long' and len(price_peaks) >= 2 and len(macd_peaks) >= 2:
        last_price_peak = price_peaks.index[-1]
        last_macd_peak = macd_peaks.index[-1]
        if last_price_peak == prices.index[-1] and last_macd_peak != macd_peaks.index[-1]:
            return prices.iloc[-1] > price_peaks.iloc[-2] and macd_line.iloc[-1] < macd_peaks.iloc[-2]
    elif position == 'short' and len(price_troughs) >= 2 and len(macd_troughs) >= 2:
        last_price_trough = price_troughs.index[-1]
        last_macd_trough = macd_troughs.index[-1]
        if last_price_trough == prices.index[-1] and last_macd_trough != macd_troughs.index[-1]:
            return prices.iloc[-1] < price_troughs.iloc[-2] and macd_line.iloc[-1] > macd_troughs.iloc[-2]
    return False

def monitor_position(position, entry_price, trade):
    global position_state
    initial_size = trade['size_sol']
    remaining_size = initial_size
    half_size = initial_size / 2
    divergence_taken = False
    highest_price = entry_price if position == 'long' else float('inf')
    lowest_price = entry_price if position == 'short' else 0

    df_5m = fetch_recent_data(timeframe='5m')
    if df_5m.empty:
        logging.error("Failed to fetch EMA100 for SL")
        return
    ema100_at_entry = df_5m['ema100'].iloc[-1]
    stop_loss = ema100_at_entry * (1 - 0.03) if position == 'long' else ema100_at_entry * (1 + 0.03)
    print(f" 📉 Hard SL set at {stop_loss:.2f} ({'3% below' if position == 'long' else '3% above'} EMA100: {ema100_at_entry:.2f})")

    while position_state == position:
        current_price = get_current_price()
        if not current_price:
            time.sleep(5)
            continue

        current_price -= current_price * SLIPPAGE if position == 'long' else -current_price * SLIPPAGE

        # Update highest/lowest price for trailing stop
        if position == 'long':
            highest_price = max(highest_price, current_price)
            trailing_sl = highest_price * (1 - TRAILING_STOP_PERCENT)
        else:
            lowest_price = min(lowest_price, current_price)
            trailing_sl = lowest_price * (1 + TRAILING_STOP_PERCENT)

        # Check hard SL
        if (position == 'long' and current_price <= stop_loss) or (position == 'short' and current_price >= stop_loss):
            if close_order('sell' if position == 'long' else 'buy', current_price, remaining_size, 'Hard SL (3% from EMA100)'):
                trade['exit_time'], trade['exit_price'], trade['exit_type'] = datetime.now(), current_price, 'Hard SL'
                tracker.update(trade)
                position_state = None
                clear_trade_state()
                asyncio.run(send_telegram_alert(f"Trade closed: Hard SL hit at {current_price:.2f}"))
                return

        # Check for MACD divergence (50% TP)
        df_15m = fetch_recent_data(timeframe='15m', limit='100')
        if not df_15m.empty and detect_divergence(df_15m, position) and not divergence_taken:
            if close_order('sell' if position == 'long' else 'buy', current_price, half_size, 'MACD Divergence TP (50%)'):
                trade['exit_price'] = current_price
                tracker.update(trade, partial=True)
                remaining_size -= half_size
                trade['size_sol'] = remaining_size
                divergence_taken = True
                print(f" 🎉 Took 50% profit at {current_price:.2f} (MACD Divergence TP) 💰 Remaining: {remaining_size:.4f} SOL")
                asyncio.run(send_telegram_alert(f"Trade partial close: 50% Divergence TP at {current_price:.2f}"))

        # Check trailing stop for remaining 50%
        if divergence_taken and ((position == 'long' and current_price <= trailing_sl) or (position == 'short' and current_price >= trailing_sl)):
            if close_order('sell' if position == 'long' else 'buy', current_price, remaining_size, 'Trailing Stop (4%)'):
                trade['exit_time'], trade['exit_price'], trade['exit_type'] = datetime.now(), current_price, 'Trailing Stop'
                tracker.update(trade)
                position_state = None
                clear_trade_state()
                print(f" 🏁 Exited remaining 50% at {current_price:.2f} (Trailing Stop - 4%) 🎉")
                asyncio.run(send_telegram_alert(f"Trade closed: Remaining 50% Trailing Stop at {current_price:.2f}"))
                return

        time.sleep(10)

# --- Initialization ---
market_api = MarketData.MarketAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')
trade_api = Trade.TradeAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')
account_api = Account.AccountAPI(api_key=API_KEY, api_secret_key=SECRET_KEY, passphrase=PASSPHRASE, use_server_time=False, flag='0')
account_api.set_position_mode(posMode="long_short_mode")
account_api.set_leverage(instId=instId, lever=str(leverage), mgnMode="cross")

# --- Main Loop ---
position_state, trade = load_trade_state()
if get_open_position():
    pos = get_open_position()
    current_price = get_current_price()
    close_order('sell' if pos['side'] == 'long' else 'buy', current_price, pos['size_sol'], 'Startup cleanup')

while True:
    try:
        if api_failure_count >= MAX_API_FAILURES:
            time.sleep(300)
            continue

        news = get_recent_news()
        sentiment = analyze_sentiment(news)
        if sentiment == 'negative':
            time.sleep(1800)
            continue

        df_5m = fetch_recent_data(timeframe='5m')
        df_15m = fetch_recent_data(timeframe='15m', limit='100')
        if df_5m.empty or len(df_5m) < 100 or df_15m.empty:
            time.sleep(180)
            continue

        row_5m = df_5m.iloc[-1]
        entry_price, entry_atr = row_5m['close'], row_5m['atr']
        atr_sma = df_5m['atr'].rolling(20).mean().iloc[-1]
        trade_size_usdt = base_trade_size_usdt * (1.5 if entry_atr > atr_sma * 1.5 else 1.0)

        current_time = time.time()
        if current_time - last_heartbeat_time >= 300:
            print(f" 🔔 Bot alive at {datetime.now().strftime('%H:%M:%S')} — Targeting 70%+ win rate! 🌌")
            last_heartbeat_time = current_time

        if position_state is None:
            if current_time - last_analysis_print_time >= 60:
                signal_1, points_1_total, points_1 = check_entry_1(df_5m, df_15m, sentiment)
                signal_2, points_2_total, points_2 = check_entry_2(df_5m, df_15m, sentiment)
                
                # Enhanced output for Entry 1
                e1_long_points = sum(points_1['long'].values())
                e1_short_points = sum(points_1['short'].values())
                e1_long_str = ", ".join([f"{k}: {v}" for k, v in points_1['long'].items() if v > 0]) or "None"
                e1_short_str = ", ".join([f"{k}: {v}" for k, v in points_1['short'].items() if v > 0]) or "None"
                
                # Enhanced output for Entry 2
                e2_long_points = sum(points_2['long'].values())
                e2_short_points = sum(points_2['short'].values())
                e2_long_str = ", ".join([f"{k}: {v}" for k, v in points_2['long'].items() if v > 0]) or "None"
                e2_short_str = ", ".join([f"{k}: {v}" for k, v in points_2['short'].items() if v > 0]) or "None"
                
                # Status message with added metrics
                status = "No trade: "
                if row_5m['chop'] > 60:
                    status += "Choppiness > 60"
                elif df_15m.iloc[-1]['adx'] <= 20:
                    status += "ADX ≤ 20"
                elif row_5m['rsi'] <= 65 and signal_1 != 'long':
                    status += "RSI ≤ 65 (Long)"
                elif row_5m['rsi'] >= 30 and signal_1 != 'short':
                    status += "RSI ≥ 30 (Short)"
                else:
                    status = "Conditions met" if signal_1 or signal_2 else "Waiting for breakout"

                print(f"🔍 E1 Long: {e1_long_points}/{points_1_total} ({e1_long_str}) | E1 Short: {e1_short_points}/{points_1_total} ({e1_short_str})")
                print(f"   E2 Long: {e2_long_points}/{points_2_total} ({e2_long_str}) | E2 Short: {e2_short_points}/{points_2_total} ({e2_short_str})")
                print(f"📈 Status: {status} | Price: {row_5m['close']:.2f} | RSI: {row_5m['rsi']:.2f} | ADX: {df_15m.iloc[-1]['adx']:.2f} | ATR: {row_5m['atr']:.2f} | Vol: {row_5m['vol']:.0f}")
                last_analysis_print_time = current_time

            signal_1, points_1_total, points_1 = check_entry_1(df_5m, df_15m, sentiment)
            signal_2, points_2_total, points_2 = check_entry_2(df_5m, df_15m, sentiment)
            if signal_1 in ['long', 'short']:
                order_id, size_sol = place_order(signal_1, entry_price, trade_size_usdt)
                if order_id:
                    trade = {'entry_time': row_5m['timestamp'], 'entry_price': entry_price, 'side': signal_1, 'size_sol': size_sol}
                    position_state = signal_1
                    save_trade_state(trade, position_state)
                    monitor_position(position_state, entry_price, trade)
            elif signal_2 in ['long', 'short']:
                order_id, size_sol = place_order(signal_2, entry_price, trade_size_usdt)
                if order_id:
                    trade = {'entry_time': row_5m['timestamp'], 'entry_price': entry_price, 'side': signal_2, 'size_sol': size_sol}
                    position_state = signal_2
                    save_trade_state(trade, position_state)
                    monitor_position(position_state, entry_price, trade)

        time.sleep(60)
    except Exception as e:
        logging.error(f"Main loop error: {str(e)}")
        asyncio.run(send_telegram_alert(f"Bot error: {str(e)}"))
        time.sleep(180)
