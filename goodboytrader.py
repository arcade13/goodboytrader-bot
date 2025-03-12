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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import pytz
import time
import sys

# Logging Setup
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler = logging.FileHandler('okx_trading_bot.log')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)
logging.info(f"Python version: {sys.version}")

# Constants
OKX_REFERRAL_LINK = "https://www.okx.com/join/43051887"
USDT_TRC20_ADDRESS = "TWVQnJJd8S1Kb6DXhNhsaREcMrYunUtswA"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPPORT_EMAIL = "goodboytrader_client_123@yahoo.com"
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

# Global State
position_states = {}
entry_atrs = {}
trades = {}
trackers = {}
custom_tps = {}
trading_active = {}
market_api = MarketData.MarketAPI(flag='0')
pending_verifications = {}
latest_trade = {"time": None, "side": None, "entry_price": None, "exit_price": None, "pnl": None}

# Sample Trades (Top 5 by PnL)
SAMPLE_TRADES = [
    {"entry_time": "2025-03-10 18:00:00", "side": "short", "entry_price": 126.70, "exit_price": 124.40, "pnl": 31.60},
    {"entry_time": "2025-03-11 04:30:00", "side": "short", "entry_price": 127.20, "exit_price": 124.90, "pnl": 31.45},
    {"entry_time": "2025-03-10 15:30:00", "side": "short", "entry_price": 128.30, "exit_price": 126.00, "pnl": 31.10},
    {"entry_time": "2025-03-11 14:00:00", "side": "short", "entry_price": 128.50, "exit_price": 126.20, "pnl": 31.00},
    {"entry_time": "2025-03-10 09:15:00", "side": "short", "entry_price": 128.80, "exit_price": 126.50, "pnl": 30.95}
]

# Database Setup
def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
        (chat_id TEXT PRIMARY KEY, tier TEXT, trade_size REAL, pnl REAL, profit_cut REAL, signup_date TEXT, sub_expiry TEXT, api_key TEXT, api_secret TEXT, api_pass TEXT, referral_code TEXT, referred_by TEXT, referral_reward_claimed INTEGER DEFAULT 0, wallet TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS trades 
        (chat_id TEXT, entry_time TEXT, entry_price REAL, exit_time TEXT, exit_price REAL, side TEXT, size_sol REAL, pnl REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS referrals 
        (referrer_id TEXT, referee_id TEXT, timestamp TEXT, PRIMARY KEY (referrer_id, referee_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS referral_profits 
        (referrer_id TEXT, referee_id TEXT, trade_time TEXT, profit REAL)''')
    conn.commit()
    conn.close()

# Referral Functions
def generate_referral_code(chat_id):
    return f"GBT{chat_id[-6:]}"

def add_referral(referrer_id, referee_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO referrals VALUES (?, ?, ?)", 
              (referrer_id, referee_id, datetime.now(TIMEZONE).isoformat()))
    conn.commit()
    conn.close()
    asyncio.run_coroutine_threadsafe(send_telegram_alert(referrer_id, 
        f"🐶 Woof! Your friend (ID: {referee_id[-6:]}) joined with your code! Earn 1% of their profits when they subscribe!"), 
        asyncio.get_event_loop())

async def monthly_payout(context: ContextTypes.DEFAULT_TYPE):
    while True:
        now = datetime.now(TIMEZONE)
        if now.day == 1 and now.hour == 0:
            conn = sqlite3.connect('users.db')
            c = conn.cursor()
            c.execute("SELECT referrer_id, SUM(profit) FROM referral_profits WHERE trade_time LIKE ? GROUP BY referrer_id", 
                      (f"{now.year}-{str(now.month).zfill(2)}%",))
            payouts = c.fetchall()
            for referrer_id, total_profit in payouts:
                c.execute("SELECT wallet FROM users WHERE chat_id = ?", (referrer_id,))
                wallet_result = c.fetchone()
                wallet = wallet_result[0] if wallet_result else None
                if wallet:
                    await send_telegram_alert(referrer_id,
                        f"💰 Referral Payout! You earned {total_profit:.2f} USDT from your invitees last month!\n"
                        f"Sent to: {wallet}\n"
                        f"Update wallet: /setwallet <USDT_TRC20_address>")
                else:
                    await send_telegram_alert(referrer_id,
                        f"💰 Referral Payout! You earned {total_profit:.2f} USDT from your invitees last month!\n"
                        f"Set a wallet to claim: /setwallet <USDT_TRC20_address>")
            c.execute("DELETE FROM referral_profits WHERE trade_time LIKE ?", (f"{now.year}-{str(now.month).zfill(2)}%",))
            conn.commit()
            conn.close()
        await asyncio.sleep(3600)

async def heartbeat(context: ContextTypes.DEFAULT_TYPE):
    while True:
        logging.info("Heartbeat: Bot running...")
        await asyncio.sleep(60)

# Utility Functions
async def send_telegram_alert(chat_id, message, reply_markup=None):
    try:
        await bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown', reply_markup=reply_markup)
        logging.info(f"Alert sent to {chat_id}: {message}")
    except Exception as e:
        logging.error(f"Failed to send alert to {chat_id}: {str(e)}")

async def pin_latest_trade(chat_id):
    if latest_trade["time"]:
        trade_msg = (
            f"📌 *Latest Trade (Pinned)*\n"
            f"Time: {latest_trade['time'].strftime('%Y-%m-%d %H:%M')}\n"
            f"Side: {latest_trade['side'].capitalize()}\n"
            f"Entry: {latest_trade['entry_price']:.2f} USDT | Exit: {latest_trade['exit_price']:.2f} USDT\n"
            f"PnL: {latest_trade['pnl']:.2f} USDT"
        )
        try:
            message = await bot.send_message(chat_id=chat_id, text=trade_msg, parse_mode='Markdown')
            await bot.pin_chat_message(chat_id=chat_id, message_id=message.message_id, disable_notification=True)
            logging.info(f"Pinned latest trade for {chat_id}")
        except Exception as e:
            logging.error(f"Failed to pin trade for {chat_id}: {str(e)}")

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

def verify_tron_tx(txid, amount):
    url = f"https://api.tronscan.org/api/transaction/{txid}"
    try:
        response = requests.get(url).json()
        return (response.get('contractData', {}).get('to_address') == USDT_TRC20_ADDRESS and 
                float(response.get('contractData', {}).get('amount', 0)) / 10**6 == amount and 
                response.get('confirmed'))
    except:
        return False

def update_user(chat_id, tier, trade_size, expiry=None, api_key=None, api_secret=None, api_pass=None, referral_code=None, referred_by=None, referral_reward_claimed=None, wallet=None):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    profit_cut = 0.05 if tier == "standard" else 0.03 if tier == "elite" else 0
    signup_date = datetime.now(TIMEZONE).isoformat() if tier == "free" else get_user(chat_id)[5]
    sub_expiry = expiry or get_user(chat_id)[6]
    current = get_user(chat_id)
    referral_reward = referral_reward_claimed if referral_reward_claimed is not None else current[12]
    wallet = wallet or current[13]
    c.execute("INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 
              (chat_id, tier, trade_size, current[3] or 0, profit_cut, signup_date, sub_expiry, 
               api_key or current[7], api_secret or current[8], api_pass or current[9], 
               referral_code or current[10], referred_by or current[11], referral_reward, wallet))
    conn.commit()
    conn.close()

def get_user(chat_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
    result = c.fetchone()
    conn.close()
    return result or (chat_id, "free", 0, 0, 0, None, None, None, None, None, None, None, 0, None)

# Telegram Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    referral_code = generate_referral_code(chat_id)
    referred_by = context.args[0] if context.args else None

    if referred_by and referred_by.startswith("GBT"):
        referrer_id = None
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute("SELECT chat_id FROM users WHERE referral_code = ?", (referred_by,))
        result = c.fetchone()
        if result:
            referrer_id = result[0]
            add_referral(referrer_id, chat_id)
        conn.close()

    tier, trade_size, _, total_pnl, _, signup_date, sub_expiry, api_key, api_secret, api_pass, _, _, _, _ = get_user(chat_id)
    referral_link = f"https://t.me/GoodBoyTraderBot?start={referral_code}"

    # Safely handle latest_trade with None checks
    trade_msg = (
        f"📈 *Latest Trade*\n"
        f"Time: {(latest_trade['time'].strftime('%Y-%m-%d %H:%M') if latest_trade['time'] is not None else 'N/A')}\n"
        f"Side: {(latest_trade['side'].capitalize() if latest_trade['side'] is not None else 'None')}\n"
        f"Entry: {(f'{latest_trade['entry_price']:.2f}' if latest_trade['entry_price'] is not None else 'N/A')} USDT | "
        f"Exit: {(f'{latest_trade['exit_price']:.2f}' if latest_trade['exit_price'] is not None else 'N/A')} USDT\n"
        f"PnL: {(f'{latest_trade['pnl']:.2f}' if latest_trade['pnl'] is not None else 'N/A')} USDT\n\n"
    )

    if tier in ["free", "trial_expired"]:
        keyboard = [
            [InlineKeyboardButton("📊 PnL", callback_data='pnl'),
             InlineKeyboardButton("🎁 Free Trial", callback_data='freetrial')],
            [InlineKeyboardButton("⭐ Standard", callback_data='standard'),
             InlineKeyboardButton("🌟 Elite", callback_data='elite')],
            [InlineKeyboardButton("👥 Referrals", callback_data='referrals'),
             InlineKeyboardButton("📞 Support", callback_data='support')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        days_left = (datetime.fromisoformat(sub_expiry) - datetime.now(TIMEZONE)).days if sub_expiry else 14
        dashboard_msg = (
            f"🐶 *Trade While You Sleep, Wake Up with a Smile – GoodBoyTrader’s Got You!*\n\n"
            f"Welcome to the first sophisticated trading bot that analyzes every move before pouncing!\n"
            f"✅ *92% Proven Success* from 5-Month Backtests!\n"
            f"📈 Trading *SOL-USDT-SWAP* on OKX now – Tier-1 exchanges (Binance, Bybit) coming soon!\n"
            f"🎁 *14-Day Free Trial*: See our biggest wins!\n"
            f"{trade_msg}"
            f"🔹 *Tier*: {tier.capitalize()} | *Trial Days Left*: {days_left}/14\n"
            f"💰 *PnL*: {total_pnl:.2f} USDT\n"
            f"👥 *Refer & Earn*: Invite friends with this link: {referral_link}\n"
            f"   - Earn 1% of their profits monthly when they subscribe! Check /referrals\n\n"
            f"🔧 *Choose Your Tier*:\n"
            f"   *Standard ($40/mo)*: Unlock 100–500 USDT trade size on 5x leverage, basic auto-trading with EMA signals (4H & 15m), predefined stop-loss & trailing stops, 5% profit cut.\n"
            f"      Start Now: /standard\n"
            f"   *Elite ($75/mo)*: Unlock 500–5,000 USDT trade size on 5x leverage, all Standard features plus custom TP (/settp), detailed 15-min signal updates, 3% profit cut, priority support.\n"
            f"      Start Now: /elite\n\n"
            f"🆕 New? Try /freetrial | Navigate below!"
        )
        if tier == "trial_expired" or (sub_expiry and datetime.now(TIMEZONE) > datetime.fromisoformat(sub_expiry)):
            update_user(chat_id, "free", 0, expiry=(datetime.now(TIMEZONE) + timedelta(days=14)).isoformat(), referral_code=referral_code, referred_by=referred_by)
            await update.message.reply_text(f"🎉 *Trial Reset!* {dashboard_msg}", reply_markup=reply_markup, parse_mode='Markdown')
        else:
            update_user(chat_id, "free", 0, expiry=(datetime.now(TIMEZONE) + timedelta(days=14)).isoformat(), referral_code=referral_code, referred_by=referred_by)
            await update.message.reply_text(dashboard_msg, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        keyboard = [
            [InlineKeyboardButton("📊 PnL", callback_data='pnl'),
             InlineKeyboardButton("🔍 Status", callback_data='status')],
            [InlineKeyboardButton("📜 History", callback_data='history'),
             InlineKeyboardButton("👥 Referrals", callback_data='referrals')],
            [InlineKeyboardButton("📞 Support", callback_data='support')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        expiry_days = (datetime.fromisoformat(sub_expiry) - datetime.now(TIMEZONE)).days if sub_expiry else 0
        dashboard_msg = (
            f"🐶 *Trade While You Sleep, Wake Up with a Smile – GoodBoyTrader’s Got You!*\n\n"
            f"🌟 *VIP Dashboard*\n"
            f"🔹 *Tier*: {tier.capitalize()} | *Expires in*: {expiry_days} days\n"
            f"💸 *Trade Size*: {trade_size} USDT @ 5x Leverage\n"
            f"🔑 *API*: {'Set' if api_key else 'Not Set'} (Update: /setapi)\n"
            f"{trade_msg}"
            f"💰 *PnL*: {total_pnl:.2f} USDT\n"
            f"👥 *Refer & Earn*: Invite friends with this link: {referral_link}\n"
            f"   - Earn 1% of their profits monthly! Check /referrals\n\n"
            f"🔧 *Manage your trades below!*"
        )
        await update.message.reply_text(dashboard_msg, reply_markup=reply_markup, parse_mode='Markdown')
        await pin_latest_trade(chat_id)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat_id)
    if query.data == 'pnl':
        await pnl(update, context)
    elif query.data == 'freetrial':
        await freetrial(update, context)
    elif query.data == 'standard':
        await standard(update, context)
    elif query.data == 'elite':
        await elite(update, context)
    elif query.data == 'referrals':
        await referrals(update, context)
    elif query.data == 'support':
        await support(update, context)
    elif query.data == 'status':
        await status(update, context)
    elif query.data == 'history':
        await history(update, context)

async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id) if update.callback_query else str(update.message.chat_id)
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("""
    SELECT COUNT(*) 
    FROM referrals r 
    JOIN users u ON r.referee_id = u.chat_id 
    WHERE r.referrer_id = ? AND u.tier IN ('standard', 'elite')
    """, (chat_id,))
    valid_refs = c.fetchone()[0]
    c.execute("SELECT SUM(profit) FROM referral_profits WHERE referrer_id = ?", (chat_id,))
    total_profit = c.fetchone()[0] or 0.0
    conn.close()
    keyboard = [[InlineKeyboardButton("🔙 Back to Dashboard", callback_data='start')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    referral_msg = (
        f"👥 *Your Referral Stats*\n\n"
        f"✅ *Valid Invitees*: {valid_refs} (Subscribed VIPs)\n"
        f"💰 *Total Earnings*: {total_profit:.2f} USDT\n\n"
        f"📎 Invite more with your link: https://t.me/GoodBoyTraderBot?start={generate_referral_code(chat_id)}\n"
        f"💸 Earn 1% of their profits monthly when they subscribe!"
    )
    await (update.callback_query.message.reply_text if update.callback_query else update.message.reply_text)(
        referral_msg, reply_markup=reply_markup, parse_mode='Markdown'
    )

async def pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id) if update.callback_query else str(update.message.chat_id)
    _, _, _, total_pnl, _, _, _, _, _, _, _, _, _, _ = get_user(chat_id)
    tracker = trackers.get(chat_id, TradeTracker())
    keyboard = [[InlineKeyboardButton("🔙 Back to Dashboard", callback_data='start')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await (update.callback_query.message.reply_text if update.callback_query else update.message.reply_text)(
        f"📊 *VIP PnL Report*\n\n"
        f"💰 *Total PnL*: {total_pnl:.2f} USDT\n"
        f"✅ *Wins*: {tracker.wins}\n"
        f"❌ *Losses*: {tracker.losses}\n"
        f"📈 *Total Trades*: {tracker.trade_count}",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id) if update.callback_query else str(update.message.chat_id)
    tier, trade_size, _, _, _, _, _, _, _, _, _, _, _, _ = get_user(chat_id)
    pos = position_states.get(chat_id, "None")
    active = trading_active.get(chat_id, False)
    trade = trades.get(chat_id, {})
    status_msg = (
        f"🔍 *VIP Status*\n\n"
        f"🔹 *Tier*: {tier.capitalize()}\n"
        f"💸 *Trade Size*: {trade_size} USDT\n"
        f"📊 *Position*: {pos if pos != 'closing' else 'Closing'}"
    )
    if pos in ["long", "short"]:
        status_msg += f" at {trade['entry_price']:.2f}"
    status_msg += f"\n🔧 *Trading*: {'Active' if active else 'Stopped'}"
    keyboard = [[InlineKeyboardButton("🔙 Back to Dashboard", callback_data='start')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await (update.callback_query.message.reply_text if update.callback_query else update.message.reply_text)(
        status_msg, reply_markup=reply_markup, parse_mode='Markdown'
    )

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT entry_time, entry_price, exit_time, exit_price, side, pnl FROM trades WHERE chat_id = ? ORDER BY entry_time DESC LIMIT 5", (chat_id,))
    trade_list = c.fetchall()
    conn.close()
    if not trade_list:
        keyboard = [[InlineKeyboardButton("🔙 Back to Dashboard", callback_data='start')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"📜 *VIP Trade History*\n\n"
            f"📉 *No trades yet!* Start trading with /setsize after upgrading.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return
    history_msg = f"📜 *VIP Trade History (Last 5)*\n\n"
    for t in trade_list:
        history_msg += (
            f"🕒 {t[0]} | {t[4].capitalize()}\n"
            f"   In: {t[1]:.2f} | Out: {t[3]:.2f}\n"
            f"   PnL: {t[5]:.2f} USDT\n\n"
        )
    keyboard = [[InlineKeyboardButton("🔙 Back to Dashboard", callback_data='start')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(history_msg.strip(), reply_markup=reply_markup, parse_mode='Markdown')

# Trading Logic
class TradeTracker:
    def __init__(self):
        self.total_pnl = 0
        self.trade_count = 0
        self.wins = 0
        self.losses = 0

    async def update(self, trade, chat_id):
        global latest_trade
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
        latest_trade = {
            "time": trade['exit_time'],
            "side": trade['side'],
            "entry_price": trade['entry_price'],
            "exit_price": trade['exit_price'],
            "pnl": user_pnl
        }
        await send_telegram_alert(chat_id, 
            f"🏆 *VIP Win!* {trade['exit_type']} at {trade['exit_price']:.2f}! You made {user_pnl:.2f} USDT (Cut: {pnl * profit_cut:.2f})")
        await pin_latest_trade(chat_id)

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
    return df

# Main
init_db()
if not TELEGRAM_TOKEN:
    logging.error("TELEGRAM_TOKEN not set in environment variables. Exiting.")
    sys.exit(1)

application = Application.builder().token(TELEGRAM_TOKEN).build()
bot = application.bot

# Add handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("pnl", pnl))
application.add_handler(CommandHandler("status", status))
application.add_handler(CommandHandler("history", history))
application.add_handler(CommandHandler("referrals", referrals))
application.add_handler(CallbackQueryHandler(button_handler))

# Schedule background tasks
application.job_queue.run_once(monthly_payout, 0)
application.job_queue.run_once(heartbeat, 0)

# Start the bot
application.run_polling()
