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
import okx.Funding as Funding
import asyncio
import telegram
import sqlite3
import requests
from telegram.ext import Application, CommandHandler
import pytz
import threading
import time
import sys

# Custom logging handler
class FlushFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

# Logging Setup
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler = FlushFileHandler('okx_trading_bot.log')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)
logging.info(f"Python version: {sys.version}")

# Constants
OKX_REFERRAL_LINK = "https://www.okx.com/join/43051887"
USDT_TRC20_ADDRESS = "TWVQnJJd8S1Kb6DXhNhsaREcMrYunUtswA"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8197397355:AAG0wqCpgdsjzgD1x5rnsEWoZy8WBVQNJdw")  # New token, ideally from env
TIMEZONE = pytz.timezone('Asia/Singapore')
leverage = 5
instId = "SOL-USDT-SWAP"
lot_size = 0.1
SLIPPAGE = 0.002
FEES = 0.00075
stop_loss_pct = 0.025
trailing_stop_factor = 1.8
ema_short_period = 5
ema_mid_period = 20
ema_long_period = 100

# OKX API for payment detection
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_SECRET = os.getenv("OKX_SECRET_KEY")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE")
funding_api = Funding.FundingAPI(OKX_API_KEY, OKX_SECRET, OKX_PASSPHRASE, flag="0")

# Global State
position_states = {}
entry_atrs = {}
trades = {}
trackers = {}
custom_tps = {}
trading_active = {}
market_api = MarketData.MarketAPI(flag='0')

# Database Setup
def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (chat_id TEXT PRIMARY KEY, tier TEXT, trade_size REAL, pnl REAL, profit_cut REAL, signup_date TEXT, sub_expiry TEXT, api_key TEXT, api_secret TEXT, api_pass TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS trades 
                 (chat_id TEXT, entry_time TEXT, entry_price REAL, exit_time TEXT, exit_price REAL, side TEXT, size_sol REAL, pnl REAL)''')
    conn.commit()
    conn.close()

# Tron TX Verification
def verify_tron_tx(txid, amount):
    url = f"https://api.tronscan.org/api/transaction/{txid}"
    try:
        response = requests.get(url).json()
        return (response.get('contractData', {}).get('to_address') == USDT_TRC20_ADDRESS and 
                float(response.get('contractData', {}).get('amount', 0)) / 10**6 == amount and 
                response.get('confirmed'))
    except:
        return False

# Utility Functions
async def send_telegram_alert(chat_id, message):
    try:
        await bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
        logging.info(f"Alert sent to {chat_id}: {message}")
    except Exception as e:
        logging.error(f"Failed to send alert to {chat_id}: {str(e)}")

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
    return None

# Telegram Handlers
async def start(update, context):
    chat_id = str(update.message.chat_id)
    update_user(chat_id, "free", 0)
    await update.message.reply_text(
        "🐾 *Woof!* Welcome to GoodBoyTrader! 14-day free trial activated!  \n"
        "⏰ Day 1/14—Upgrade: /standard ($25) or /elite ($75).  \n"
        "🌟 *Soon:* Binance, Bybit support!  \n"
        "📋 Menu: /pnl, /status, /starttrading, /stoptrading, /settp, /close, /history"
    , parse_mode='Markdown')

async def standard(update, context):
    chat_id = str(update.message.chat_id)
    await update.message.reply_text(
        "🚀 *Standard Tier ($25/month)*: 100–500 USDT trades!  \n"
        f"💸 Send 25 USDT (TRC-20) to: `{USDT_TRC20_ADDRESS}`  \n"
        "📩 Then: /verify <txid>"
    , parse_mode='Markdown')

async def elite(update, context):
    chat_id = str(update.message.chat_id)
    await update.message.reply_text(
        "🏆 *Elite Tier ($75/month)*: 500–5,000 USDT trades!  \n"
        f"💸 Send 75 USDT (TRC-20) to: `{USDT_TRC20_ADDRESS}`  \n"
        "📩 Then: /verify <txid>"
    , parse_mode='Markdown')

async def verify(update, context):
    chat_id = str(update.message.chat_id)
    try:
        txid = context.args[0]
        tier, _, _, _, _, _, _ = get_user(chat_id)
        if tier != "free":
            await update.message.reply_text("🐶 *Woof!* Already a VIP!")
            return
        
        amount = 25 if verify_tron_tx(txid, 25) else 75 if verify_tron_tx(txid, 75) else 0
        if amount == 0:
            await update.message.reply_text("❌ *Oops!* Invalid TXID!")
            return
        
        new_tier = "standard" if amount == 25 else "elite"
        expiry = datetime.now(TIMEZONE) + timedelta(days=30)
        update_user(chat_id, new_tier, 0, expiry=expiry.isoformat())
        await update.message.reply_text(
            f"🎉 *Woof woof!* Welcome to the {new_tier.capitalize()} VIP Pack! Expires: {expiry.strftime('%Y-%m-%d')}  \n"
            "🐾 *You’re special now—here’s how to start:*  \n"
            f"1️⃣ *Join OKX:* [{OKX_REFERRAL_LINK}]({OKX_REFERRAL_LINK})  \n"
            "2️⃣ *Fund Futures:* Assets → Deposit → USDT (TRC-20) → Transfer to Trading (150+ USDT)  \n"
            "3️⃣ *API:* Profile → API → Create (Name: GoodBoyTrader, 'Trade' on)  \n"
            "4️⃣ *Set API:* /setapi <Key> <Secret> <Passphrase>  \n"
            "5️⃣ *Size:* /setsize <100–500> or <500–5000>  \n"
            "💰 5x leverage auto-set! Get 15-min VIP updates soon!"
        , parse_mode='Markdown')
    except:
        await update.message.reply_text("❌ *Grr!* Use: /verify <txid>")

async def setapi(update, context):
    chat_id = str(update.message.chat_id)
    tier, trade_size, _, _, _, _, _, _, _, _ = get_user(chat_id)
    if tier == "free":
        await update.message.reply_text("👀 *Woof!* Upgrade first!")
        return
    try:
        key, secret, passphrase = context.args
        account_api = Account.AccountAPI(api_key=key, api_secret_key=secret, passphrase=passphrase, use_server_time=False, flag='0')
        account_api.set_position_mode(posMode="long_short_mode")
        account_api.set_leverage(instId=instId, lever=str(leverage), mgnMode="cross")
        update_user(chat_id, tier, trade_size, api_key=key, api_secret=secret, api_pass=passphrase)
        await update.message.reply_text(
            f"✅ *Woof!* API set! 5x leverage locked in.  \n"
            f"🐾 VIP step: /setsize {100 if tier == 'standard' else 500}–{500 if tier == 'standard' else 5000}"
        , parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ *Grr!* Failed: {str(e)}. Retry: /setapi")

async def setsize(update, context):
    chat_id = str(update.message.chat_id)
    tier, _, _, _, _, _, _, api_key, _, _ = get_user(chat_id)
    if tier == "free":
        await update.message.reply_text("👀 *Woof!* Upgrade first!")
        return
    if not api_key:
        await update.message.reply_text("🐾 *Woof!* Set API first!")
        return
    try:
        size = float(context.args[0])
        size = adjust_trade_size(tier, size)
        update_user(chat_id, tier, size)
        trackers[chat_id] = TradeTracker()
        trading_active[chat_id] = False
        await update.message.reply_text(
            f"✅ *Woof!* Trade size set to {size} USDT!  \n"
            "🐶 VIP trading ready—use /starttrading to begin!"
        , parse_mode='Markdown')
    except:
        await update.message.reply_text(f"❌ *Oops!* Use: /setsize {100 if tier == 'standard' else 500}–{500 if tier == 'standard' else 5000}")

async def starttrading(update, context):
    chat_id = str(update.message.chat_id)
    tier, trade_size, _, _, _, _, _, api_key, _, _ = get_user(chat_id)
    if tier == "free" or not api_key or trade_size == 0:
        await update.message.reply_text("🐾 *Woof!* Complete setup: /verify, /setapi, /setsize")
        return
    trading_active[chat_id] = True
    if chat_id not in [t.name for t in threading.enumerate()]:
        threading.Thread(target=run_trading_logic, args=(chat_id,), daemon=True).start()
    await update.message.reply_text("🚀 *Woof!* Trading started! GoodBoy’s on the hunt!")

async def stoptrading(update, context):
    chat_id = str(update.message.chat_id)
    if chat_id in trading_active:
        trading_active[chat_id] = False
        await update.message.reply_text("🛑 *Woof!* Trading paused. Position still open—use /close to exit.")
    else:
        await update.message.reply_text("🐾 *Woof!* Trading already stopped!")

async def settp(update, context):
    chat_id = str(update.message.chat_id)
    if chat_id not in position_states:
        await update.message.reply_text("🐾 *Woof!* No active position!")
        return
    try:
        tp_price = float(context.args[0])
        custom_tps[chat_id] = tp_price
        await update.message.reply_text(f"✅ *Woof!* TP set to {tp_price:.2f} USDT!")
    except:
        await update.message.reply_text("❌ *Grr!* Use: /settp <price>")

async def close(update, context):
    chat_id = str(update.message.chat_id)
    if chat_id not in position_states:
        await update.message.reply_text("🐾 *Woof!* No active position!")
        return
    position_states[chat_id] = "closing"
    await update.message.reply_text("🏁 *Woof!* Closing position now...")

async def pnl(update, context):
    chat_id = str(update.message.chat_id)
    _, _, _, total_pnl, _, _, _, _, _, _ = get_user(chat_id)
    tracker = trackers.get(chat_id, TradeTracker())
    await update.message.reply_text(
        f"💰 *VIP PnL Report*  \n"
        f"📈 Total PnL: {total_pnl:.2f} USDT  \n"
        f"🎲 Wins: {tracker.wins} | Losses: {tracker.losses} | Trades: {tracker.trade_count}"
    , parse_mode='Markdown')

async def status(update, context):
    chat_id = str(update.message.chat_id)
    tier, trade_size, _, _, _, _, _, _, _, _ = get_user(chat_id)
    pos = position_states.get(chat_id, "None")
    active = trading_active.get(chat_id, False)
    trade = trades.get(chat_id, {})
    status_msg = (
        f"🐾 *VIP Status*  \n"
        f"🎯 Tier: {tier.capitalize()} | Size: {trade_size} USDT  \n"
        f"📈 Position: {pos if pos != 'closing' else 'Closing'} "
    )
    if pos in ["long", "short"]:
        status_msg += f"at {trade['entry_price']:.2f}"
    status_msg += f"\n⏰ Trading: {'Active' if active else 'Stopped'}"
    await update.message.reply_text(status_msg, parse_mode='Markdown')

async def history(update, context):
    chat_id = str(update.message.chat_id)
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT entry_time, entry_price, exit_time, exit_price, side, pnl FROM trades WHERE chat_id = ? ORDER BY entry_time DESC LIMIT 5", (chat_id,))
    trade_list = c.fetchall()
    conn.close()
    if not trade_list:
        await update.message.reply_text("📜 *Woof!* No trade history yet!")
        return
    history_msg = "📜 *VIP Trade History (Last 5)*\n"
    for t in trade_list:
        history_msg += f"📅 {t[0]} | {t[4].capitalize()} | In: {t[1]:.2f} | Out: {t[3]:.2f} | PnL: {t[5]:.2f} USDT\n"
    await update.message.reply_text(history_msg, parse_mode='Markdown')

# Trading Logic
class TradeTracker:
    def __init__(self):
        self.total_pnl = 0
        self.trade_count = 0
        self.wins = 0
        self.losses = 0

    def update(self, trade, chat_id):
        size = trade['size_sol']
        entry_value = trade['entry_price'] * size
        exit_value = trade['exit_price'] * size
        fees = (entry_value + exit_value) * FEES
        slippage_cost = entry_value * SLIPPAGE * 2
        total_cost = fees + slippage_cost
        pnl_raw = (trade['exit_price'] - trade['entry_price']) * size * (1 if trade['side'] == 'long' else -1)
        profit_cut = get_user(chat_id)[4]
        pnl = (pnl_raw - total_cost) * leverage
        user_pnl = pnl * (1 - profit_cut)
        self.total_pnl += user_pnl
        self.trade_count += 1
        self.wins += 1 if user_pnl > 0 else 0
        self.losses += 1 if user_pnl < 0 else 0
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute("UPDATE users SET pnl = ? WHERE chat_id = ?", (self.total_pnl, chat_id))
        c.execute("INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                  (chat_id, trade['entry_time'].isoformat(), trade['entry_price'], 
                   trade['exit_time'].isoformat(), trade['exit_price'], trade['side'], size, user_pnl))
        conn.commit()
        conn.close()
        asyncio.run(send_telegram_alert(chat_id, 
            f"💰 *VIP Win!* {trade['exit_type']} at {trade['exit_price']:.2f}! You made {user_pnl:.2f} USDT (Cut: {pnl * profit_cut:.2f})"
        ))

def fetch_recent_data(timeframe='4H', limit='400'):
    response = fetch_with_retries(lambda: market_api.get_candlesticks(instId=instId, bar=timeframe, limit=limit))
    if not response:
        return pd.DataFrame()
    data = response['data'][::-1]
    df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm'])
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
    df[['open', 'high', 'low', 'close', 'vol']] = df[['open', 'high', 'low', 'close', 'vol']].astype(float)
    df['ema_5'] = ta.trend.ema_indicator(df['close'], window=ema_short_period)
    df['ema_20'] = ta.trend.ema_indicator(df['close'], window=ema_mid_period)
    df['ema_100'] = ta.trend.ema_indicator(df['close'], window=ema_long_period)
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
    df['atr_mean'] = df['atr'].rolling(14).mean()
    return df

def check_entry(df_4h, df_15m):
    if len(df_4h) < 2 or len(df_15m) < 1:
        return None, 0, 0, 0, 0
    current_4h, prev_4h = df_4h.iloc[-1], df_4h.iloc[-2]
    current_15m = df_15m.iloc[-1]
    
    short_points_4h = sum([
        prev_4h['ema_5'] > prev_4h['ema_100'] and current_4h['ema_5'] < current_4h['ema_100'],
        prev_4h['ema_20'] > prev_4h['ema_100'] and current_4h['ema_20'] < current_4h['ema_100'],
        current_4h['ema_5'] < current_4h['ema_100'],
        current_4h['ema_20'] < current_4h['ema_100']
    ])
    long_points_4h = sum([
        prev_4h['ema_5'] < prev_4h['ema_100'] and current_4h['ema_5'] > current_4h['ema_100'],
        prev_4h['ema_20'] < prev_4h['ema_100'] and current_4h['ema_20'] > current_4h['ema_100'],
        current_4h['ema_5'] > current_4h['ema_100'],
        current_4h['ema_20'] > current_4h['ema_100']
    ])
    short_points_15m = sum([
        current_15m['close'] < current_15m['ema_100'],
        current_15m['ema_5'] < current_15m['ema_20'],
        current_15m['ema_20'] < current_15m['ema_100']
    ])
    long_points_15m = sum([
        current_15m['close'] > current_15m['ema_100'],
        current_15m['ema_5'] > current_15m['ema_20'],
        current_15m['ema_20'] > current_15m['ema_100']
    ])
    
    if short_points_4h >= 3 and short_points_15m == 3:
        return 'short', short_points_4h, long_points_4h, short_points_15m, long_points_15m
    elif long_points_4h >= 3 and long_points_15m == 3:
        return 'long', short_points_4h, long_points_4h, short_points_15m, long_points_15m
    return None, short_points_4h, long_points_4h, short_points_15m, long_points_15m

def place_order(trade_api, side, price, size_usdt):
    size_sol = (size_usdt * leverage) / price
    size_contracts = max(round(size_sol / lot_size), 1)
    response = fetch_with_retries(lambda: trade_api.place_order(
        instId=instId, tdMode='cross', side='buy' if side == 'long' else 'sell',
        posSide=side, ordType='market', sz=str(size_contracts)
    ))
    if response and response['code'] == '0':
        return response['data'][0]['ordId'], size_contracts * lot_size
    return None, 0

def close_order(trade_api, side, price, size_sol, exit_type, chat_id):
    size_contracts = round(size_sol / lot_size)
    response = fetch_with_retries(lambda: trade_api.place_order(
        instId=instId, tdMode='cross', side=side,
        posSide='long' if side == 'sell' else 'short',
        ordType='market', sz=str(size_contracts)
    ))
    if response and response['code'] == '0':
        asyncio.run(send_telegram_alert(chat_id, f"🏁 *VIP Exit!* Closed at {price:.2f} ({exit_type})"))
        return True
    return False

def run_trading_logic(chat_id):
    global position_states, entry_atrs, trades, custom_tps
    tier, trade_size, _, _, _, _, expiry, api_key, api_secret, api_pass = get_user(chat_id)
    if tier == "free" or datetime.now(TIMEZONE) > datetime.fromisoformat(expiry or '9999-12-31'):
        return
    
    trade_api = Trade.TradeAPI(api_key=api_key, api_secret_key=api_secret, passphrase=api_pass, use_server_time=False, flag='0')
    last_update = datetime.now(TIMEZONE) - timedelta(minutes=15)
    
    while True:
        if not trading_active.get(chat_id, False):
            time.sleep(10)
            continue
        
        df_4h = fetch_recent_data(timeframe='4H', limit='400')
        df_15m = fetch_recent_data(timeframe='15m', limit='100')
        if df_4h.empty or len(df_4h) < ema_long_period or df_15m.empty or len(df_15m) < ema_long_period:
            time.sleep(10)
            continue
        
        current_price = float(market_api.get_ticker(instId=instId)['data'][0]['last'])
        signal, s4, l4, s15, l15 = check_entry(df_4h, df_15m)
        
        if (datetime.now(TIMEZONE) - last_update).total_seconds() >= 900:
            trend = "Up" if df_15m['ema_5'].iloc[-1] > df_15m['ema_100'].iloc[-1] else "Down"
            status = f"In {trades[chat_id]['side'].capitalize()} at {trades[chat_id]['entry_price']:.2f}" if chat_id in position_states else "Waiting for signal"
            asyncio.run(send_telegram_alert(chat_id, 
                f"🌟 *VIP Update* (15-min)  \n"
                f"💸 SOL-USDT: {current_price:.2f} | Trend: {trend}  \n"
                f"🐾 Status: {status}  \n"
                f"📊 Points - 4H Short: {s4}/4 | Long: {l4}/4 | 15m Short: {s15}/3 | Long: {l15}/3"
            ))
            last_update = datetime.now(TIMEZONE)
        
        if signal and chat_id not in position_states:
            order_id, size_sol = place_order(trade_api, signal, current_price, trade_size)
            if order_id:
                asyncio.run(send_telegram_alert(chat_id, 
                    f"🚀 *VIP Trade On!* {signal.capitalize()} at {current_price:.2f} with {trade_size} USDT!  \n"
                    f"📊 Trigger Points - 4H Short: {s4}/4 | Long: {l4}/4 | 15m Short: {s15}/3 | Long: {l15}/3"
                ))
                trades[chat_id] = {'entry_time': datetime.now(TIMEZONE), 'entry_price': current_price, 'side': signal, 'size_sol': size_sol}
                position_states[chat_id] = signal
                entry_atrs[chat_id] = df_15m['atr'].iloc[-1]
                
                stop_loss = current_price * (1 - stop_loss_pct) if signal == 'long' else current_price * (1 + stop_loss_pct)
                trailing_sl = current_price + (entry_atrs[chat_id] * trailing_stop_factor) if signal == 'long' else current_price - (entry_atrs[chat_id] * trailing_stop_factor)
                
                while position_states.get(chat_id) == signal:
                    current_price = float(market_api.get_ticker(instId=instId)['data'][0]['last'])
                    current_price -= current_price * SLIPPAGE if signal == 'long' else -current_price * SLIPPAGE
                    
                    custom_tp = custom_tps.get(chat_id)
                    if custom_tp and ((signal == 'long' and current_price >= custom_tp) or (signal == 'short' and current_price <= custom_tp)):
                        if close_order(trade_api, 'sell' if signal == 'long' else 'buy', current_price, size_sol, 'Custom TP', chat_id):
                            trades[chat_id].update({'exit_time': datetime.now(TIMEZONE), 'exit_price': current_price, 'exit_type': 'Custom TP'})
                            trackers[chat_id].update(trades[chat_id], chat_id)
                            del position_states[chat_id]
                            del custom_tps[chat_id]
                    elif (signal == 'long' and current_price <= stop_loss) or (signal == 'short' and current_price >= stop_loss):
                        if close_order(trade_api, 'sell' if signal == 'long' else 'buy', current_price, size_sol, 'Stop Loss', chat_id):
                            trades[chat_id].update({'exit_time': datetime.now(TIMEZONE), 'exit_price': current_price, 'exit_type': 'Stop Loss'})
                            trackers[chat_id].update(trades[chat_id], chat_id)
                            del position_states[chat_id]
                    elif (signal == 'long' and current_price <= trailing_sl) or (signal == 'short' and current_price >= trailing_sl):
                        if close_order(trade_api, 'sell' if signal == 'long' else 'buy', current_price, size_sol, 'Trailing Stop', chat_id):
                            trades[chat_id].update({'exit_time': datetime.now(TIMEZONE), 'exit_price': current_price, 'exit_type': 'Trailing Stop'})
                            trackers[chat_id].update(trades[chat_id], chat_id)
                            del position_states[chat_id]
                    elif position_states.get(chat_id) == "closing":
                        if close_order(trade_api, 'sell' if signal == 'long' else 'buy', current_price, size_sol, 'Manual Close', chat_id):
                            trades[chat_id].update({'exit_time': datetime.now(TIMEZONE), 'exit_price': current_price, 'exit_type': 'Manual Close'})
                            trackers[chat_id].update(trades[chat_id], chat_id)
                            del position_states[chat_id]
                    time.sleep(10)
        time.sleep(300)

def adjust_trade_size(tier, requested_size):
    if tier == "standard":
        return min(max(requested_size, 100), 500)
    elif tier == "elite":
        return min(max(requested_size, 500), 5000)
    return 0

def update_user(chat_id, tier, trade_size, expiry=None, api_key=None, api_secret=None, api_pass=None):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    profit_cut = 0.10 if tier == "standard" else 0.15 if tier == "elite" else 0
    signup_date = datetime.now(TIMEZONE).isoformat() if tier == "free" else get_user(chat_id)[5]
    sub_expiry = expiry or get_user(chat_id)[6]
    current = get_user(chat_id)
    c.execute("INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 
              (chat_id, tier, trade_size, current[3] or 0, profit_cut, signup_date, sub_expiry, api_key or current[7], api_secret or current[8], api_pass or current[9]))
    conn.commit()
    conn.close()

def get_user(chat_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
    result = c.fetchone()
    conn.close()
    return result or (chat_id, "free", 0, 0, 0, None, None, None, None, None)

# Main
init_db()
application = Application.builder().token(TELEGRAM_TOKEN).build()
bot = application.bot

# Add handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("standard", standard))
application.add_handler(CommandHandler("elite", elite))
application.add_handler(CommandHandler("verify", verify))
application.add_handler(CommandHandler("setapi", setapi))
application.add_handler(CommandHandler("setsize", setsize))
application.add_handler(CommandHandler("starttrading", starttrading))
application.add_handler(CommandHandler("stoptrading", stoptrading))
application.add_handler(CommandHandler("settp", settp))
application.add_handler(CommandHandler("close", close))
application.add_handler(CommandHandler("pnl", pnl))
application.add_handler(CommandHandler("status", status))
application.add_handler(CommandHandler("history", history))

# Start the bot
application.run_polling()

# Keep main thread alive
while True:
    logging.info("Heartbeat: Bot running...")
    time.sleep(1)
