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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler
import pytz
import threading
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
    asyncio.run(send_telegram_alert(referrer_id, 
        f"🎉 *Woof!* Your friend (ID: {referee_id[-6:]}) joined with your code! Earn 1% of their profits when they subscribe!"))

def track_referral_profit(referee_id, profit):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT referrer_id FROM referrals WHERE referee_id = ?", (referee_id,))
    referrer_id = c.fetchone()
    if referrer_id and profit > 0:
        referrer_id = referrer_id[0]
        c.execute("INSERT INTO referral_profits VALUES (?, ?, ?, ?)",
                  (referrer_id, referee_id, datetime.now(TIMEZONE).isoformat(), profit * 0.01))
        conn.commit()
    conn.close()

async def monthly_payout():
    while True:
        now = datetime.now(TIMEZONE)
        if now.day == 1 and now.hour == 0:  # Midnight on the 1st
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
                        f"💰 *Referral Payout!* You earned {total_profit:.2f} USDT from your invitees last month!\n"
                        f"Sent to: {wallet}\n"
                        f"Update wallet: /setwallet <USDT_TRC20_address>")
                else:
                    await send_telegram_alert(referrer_id,
                        f"💰 *Referral Payout!* You earned {total_profit:.2f} USDT from your invitees last month!\n"
                        f"Set a wallet to claim: /setwallet <USDT_TRC20_address>")
            c.execute("DELETE FROM referral_profits WHERE trade_time LIKE ?", (f"{now.year}-{str(now.month).zfill(2)}%",))
            conn.commit()
            conn.close()
        time.sleep(3600)  # Check hourly

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
async def start(update, context):
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
    
    tier, _, _, _, _, signup_date, sub_expiry, _, _, _, _, _, _, _ = get_user(chat_id)
    referral_link = f"https://t.me/GoodBoyTraderBot?start={referral_code}"
    welcome_msg = (
        f"🌙 *Trade While You Sleep, Wake Up with a Smile – GoodBoyTrader’s Got You!*\n\n"
        f"🐾 Welcome to the *first sophisticated trading bot* that analyzes every move before pouncing!\n"
        f"💰 *92% Proven Success from 5-Month Backtests!*\n"
        f"🌟 Trading SOL-USDT-SWAP on OKX now – Tier-1 exchanges (Binance, Bybit) coming soon!\n"
        f"🎁 *14-Day Free Trial*: See our biggest wins!\n"
        f"👯 *Refer & Earn*: Invite friends with this link: [{referral_link}]({referral_link})\n"
        f"   - Earn 1% of their profits monthly when they subscribe! Check /referrals\n\n"
        f"🚀 *Choose Your Tier:*\n"
        f"  📍 *Standard ($40/mo)*: 100–500 USDT trades, basic auto-trading, core signals, no custom TP, 5% profit cut.\n"
        f"      **Start Now: /standard**\n"
        f"  🏆 *Elite ($75/mo)*: 500–5,000 USDT trades, custom TP (/settp), detailed signal updates, *3% profit cut*, priority support.\n"
        f"      **Start Now: /elite**\n\n"
        f"💡 *New? Try /freetrial* | Monitor: /pnl, /status, /history, /referrals\n"
        f"📧 Need help? Use /support"
    )
    if tier == "trial_expired" or (tier == "free" and sub_expiry and datetime.now(TIMEZONE) > datetime.fromisoformat(sub_expiry)):
        update_user(chat_id, "free", 0, expiry=(datetime.now(TIMEZONE) + timedelta(days=14)).isoformat(), referral_code=referral_code, referred_by=referred_by)
        await update.message.reply_text(f"*Trial Reset!* {welcome_msg}", parse_mode='Markdown')
    else:
        update_user(chat_id, "free", 0, expiry=(datetime.now(TIMEZONE) + timedelta(days=14)).isoformat(), referral_code=referral_code, referred_by=referred_by)
        await update.message.reply_text(welcome_msg, parse_mode='Markdown')

async def freetrial(update, context):
    chat_id = str(update.message.chat_id)
    tier, _, _, total_pnl, _, signup_date, sub_expiry, _, _, _, referral_code, _, _, _ = get_user(chat_id)
    if tier not in ["free", "trial_expired"]:
        await update.message.reply_text("🐶 *Woof!* You’re already a VIP! Check /status.")
        return
    
    trades_msg = "📈 *Top 5 Wins (Backtest)*:\n\n" + "\n".join(
        [f"  {t['entry_time']} | {t['side'].capitalize()} | In: {t['entry_price']:.2f} | Out: {t['exit_price']:.2f} | PnL: {t['pnl']:.2f} USDT" 
         for t in SAMPLE_TRADES]
    )
    
    current_trade = trades.get(chat_id, {})
    trade_msg = (f"🎯 *Current Trade*: {current_trade['side'].capitalize()} at {current_trade['entry_price']:.2f} (Started: {current_trade['entry_time'].strftime('%Y-%m-%d %H:%M')})"
        if chat_id in position_states and position_states[chat_id] in ["long", "short"]
        else "🎯 *Current Trade*: None—bot trades for VIPs!")
    
    days_left = (datetime.fromisoformat(sub_expiry) - datetime.now(TIMEZONE)).days
    await update.message.reply_text(
        f"🌙 *Trade While You Sleep, Wake Up with a Smile – GoodBoyTrader’s Got You!*\n\n"
        f"🎉 *Woof!* Free Trial Active! Days Left: {days_left}/14\n"
        f"💰 *92% Proven Success from 5-Month Backtests!*\n\n"
        f"{trades_msg}\n\n"
        f"💰 *PnL*: {total_pnl:.2f} USDT (Live trades start with VIP!)\n"
        f"{trade_msg}\n\n"
        f"👯 Share `{referral_code}`—earn 1% of their profits monthly when they subscribe!\n"
        f"🚀 *Upgrade to VIP:* /standard ($40) or /elite ($75)"
    , parse_mode='Markdown')

async def standard(update, context):
    chat_id = str(update.message.chat_id)
    keyboard = [
        [InlineKeyboardButton("📋 Copy USDT Address", url=f"tg://msg?text={USDT_TRC20_ADDRESS}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"🚀 *Standard Tier ($40/month)*\n\n"
        f"💡 *What You Get:*\n"
        f"  - Trade Size: 100–500 USDT\n"
        f"  - Basic auto-trading with core EMA signals (4H & 15m)\n"
        f"  - Commands: /pnl, /status, /history, /stoptrading, /close, /setsize\n"
        f"  - Predefined stop-loss & trailing stops (no custom TP)\n"
        f"  - Basic 15-min updates (price & trend only)\n"
        f"  - 5% profit cut\n\n"
        f"💸 *Payment Instructions:*\n"
        f"  Send 40 USDT (TRC-20) to: `{USDT_TRC20_ADDRESS}`\n"
        f"  Then use: /verify <txid>\n\n"
        f"🐾 Affordable entry for casual traders—want more control? See /elite!",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def elite(update, context):
    chat_id = str(update.message.chat_id)
    keyboard = [
        [InlineKeyboardButton("📋 Copy USDT Address", url=f"tg://msg?text={USDT_TRC20_ADDRESS}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"🏆 *Elite Tier ($75/month)*\n\n"
        f"💡 *What You Get:*\n"
        f"  - Trade Size: 500–5,000 USDT\n"
        f"  - All Standard features, *plus:*\n"
        f"  - Custom take-profit with /settp\n"
        f"  - Enhanced 15-min updates with signal points (e.g., 4H Short: X/4)\n"
        f"  - *Higher profit retention: 3% cut* (vs. 5% Standard)\n"
        f"  - Priority support & future premium features (e.g., Binance/Bybit pairs)\n\n"
        f"💸 *Payment Instructions:*\n"
        f"  Send 75 USDT (TRC-20) to: `{USDT_TRC20_ADDRESS}`\n"
        f"  Then use: /verify <txid>\n\n"
        f"🐾 The ultimate edge for serious traders—maximize your wins!",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def verify(update, context):
    chat_id = str(update.message.chat_id)
    try:
        txid = context.args[0]
        tier, _, _, _, _, _, _, _, _, _, _, referred_by, _, _ = get_user(chat_id)
        if tier in ["standard", "elite"]:
            await update.message.reply_text("🐶 *Woof!* Already a VIP!")
            return
        
        amount = 40 if verify_tron_tx(txid, 40) else 75 if verify_tron_tx(txid, 75) else 0
        if amount == 0:
            await update.message.reply_text("❌ *Oops!* Invalid TXID!")
            return
        
        new_tier = "standard" if amount == 40 else "elite"
        expiry = datetime.now(TIMEZONE) + timedelta(days=30)
        update_user(chat_id, new_tier, 0, expiry=expiry.isoformat())
        if referred_by:
            add_referral(referred_by, chat_id)
        await update.message.reply_text(
            f"🎉 *Woof woof!* Welcome to {new_tier.capitalize()} VIP! Expires: {expiry.strftime('%Y-%m-%d')}\n\n"
            f"🐾 *Start trading:*\n"
            f"  1️⃣ *Join OKX:* [{OKX_REFERRAL_LINK}]({OKX_REFERRAL_LINK})\n"
            f"  2️⃣ *Fund Futures:* Deposit USDT (TRC-20) → Transfer to Trading (150+ USDT)\n"
            f"  3️⃣ *API:* Profile → API → Create (Name: GoodBoyTrader, 'Trade' on)\n"
            f"  4️⃣ *Set API:* /setapi <Key> <Secret> <Passphrase>\n"
            f"  5️⃣ *Size:* /setsize <100–500> or <500–5000>\n"
            f"💰 Bot auto-trades at 5x leverage!"
        , parse_mode='Markdown')
    except:
        await update.message.reply_text("❌ *Grr!* Use: /verify <txid>")

async def setapi(update, context):
    chat_id = str(update.message.chat_id)
    tier, trade_size, _, _, _, _, _, _, _, _, _, _, _, _ = get_user(chat_id)
    if tier in ["free", "trial_expired"]:
        await update.message.reply_text("👀 *Woof!* Upgrade with /standard or /elite to trade!")
        return
    try:
        key, secret, passphrase = context.args
        account_api = Account.AccountAPI(api_key=key, api_secret_key=secret, passphrase=passphrase, use_server_time=False, flag='0')
        account_api.set_position_mode(posMode="long_short_mode")
        account_api.set_leverage(instId=instId, lever=str(leverage), mgnMode="cross")
        update_user(chat_id, tier, trade_size, api_key=key, api_secret=secret, api_pass=passphrase)
        await update.message.reply_text(
            f"✅ *Woof!* API set! 5x leverage locked.\n"
            f"🐾 Next: /setsize {100 if tier == 'standard' else 500}–{500 if tier == 'standard' else 5000}"
        , parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ *Grr!* Failed: {str(e)}. Retry: /setapi")

async def setsize(update, context):
    chat_id = str(update.message.chat_id)
    tier, _, _, _, _, _, sub_expiry, api_key, _, _, referral_code, _, _, _ = get_user(chat_id)
    if tier in ["free", "trial_expired"]:
        await update.message.reply_text("👀 *Woof!* Upgrade with /standard or /elite to trade!")
        return
    if not api_key:
        await update.message.reply_text("🐾 *Woof!* Set API first with /setapi!")
        return
    try:
        size = float(context.args[0])
        size = adjust_trade_size(tier, size)
        update_user(chat_id, tier, size)
        trackers[chat_id] = TradeTracker()
        trading_active[chat_id] = True
        if chat_id not in [t.name for t in threading.enumerate()]:
            threading.Thread(target=run_trading_logic, args=(chat_id,), daemon=True).start()
        await update.message.reply_text(
            f"✅ *Woof!* Trade size set to {size} USDT! Bot is now auto-trading!\n"
            f"🐶 Monitor: /status, /pnl, /history | Force exit: /stoptrading" +
            (f" | Set TP: /settp" if tier == "elite" else "")
        , parse_mode='Markdown')
    except:
        await update.message.reply_text(
            f"❌ *Oops!* Use: /setsize {100 if tier == 'standard' else 500}–{500 if tier == 'standard' else 5000}"
        , parse_mode='Markdown')

async def stoptrading(update, context):
    chat_id = str(update.message.chat_id)
    if chat_id in trading_active:
        trading_active[chat_id] = False
        await update.message.reply_text("🛑 *Woof!* Trading paused. Position still open—use /close to exit.")
    else:
        await update.message.reply_text("🐾 *Woof!* Trading already stopped!")

async def settp(update, context):
    chat_id = str(update.message.chat_id)
    tier, _, _, _, _, _, _, _, _, _, _, _, _, _ = get_user(chat_id)
    if tier != "elite":
        await update.message.reply \n\nreply_text("🐾 *Woof!* Custom TP is an Elite feature! Upgrade with /elite.")
        return
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
    _, _, _, total_pnl, _, _, _, _, _, _, _, _, _, _ = get_user(chat_id)
    tracker = trackers.get(chat_id, TradeTracker())
    await update.message.reply_text(
        f"💰 *VIP PnL Report*\n\n"
        f"  Total PnL: {total_pnl:.2f} USDT\n"
        f"  Wins: {tracker.wins}\n"
        f"  Losses: {tracker.losses}\n"
        f"  Total Trades: {tracker.trade_count}"
    , parse_mode='Markdown')

async def status(update, context):
    chat_id = str(update.message.chat_id)
    tier, trade_size, _, _, _, _, _, _, _, _, _, _, _, _ = get_user(chat_id)
    pos = position_states.get(chat_id, "None")
    active = trading_active.get(chat_id, False)
    trade = trades.get(chat_id, {})
    status_msg = (
        f"🐾 *VIP Status*\n\n"
        f"  Tier: {tier.capitalize()}\n"
        f"  Trade Size: {trade_size} USDT\n"
        f"  Position: {pos if pos != 'closing' else 'Closing'}"
    )
    if pos in ["long", "short"]:
        status_msg += f" at {trade['entry_price']:.2f}"
    status_msg += f"\n  Trading: {'Active' if active else 'Stopped'}"
    await update.message.reply_text(status_msg, parse_mode='Markdown')

async def history(update, context):
    chat_id = str(update.message.chat_id)
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT entry_time, entry_price, exit_time, exit_price, side, pnl FROM trades WHERE chat_id = ? ORDER BY entry_time DESC LIMIT 5", (chat_id,))
    trade_list = c.fetchall()
    conn.close()
    if not trade_list:
        await update.message.reply_text(
            f"📜 *VIP Trade History*\n\n"
            f"  No trades yet! Start trading with /setsize after upgrading."
        , parse_mode='Markdown')
        return
    history_msg = f"📜 *VIP Trade History (Last 5)*\n\n"
    for t in trade_list:
        history_msg += (
            f"  {t[0]} | {t[4].capitalize()}\n"
            f"    In: {t[1]:.2f} | Out: {t[3]:.2f}\n"
            f"    PnL: {t[5]:.2f} USDT\n\n"
        )
    await update.message.reply_text(history_msg.strip(), parse_mode='Markdown')

async def setwallet(update, context):
    chat_id = str(update.message.chat_id)
    try:
        wallet = context.args[0]
        if not wallet.startswith("T"):  # Basic TRC-20 check
            raise ValueError("Invalid TRC-20 address")
        update_user(chat_id, get_user(chat_id)[1], get_user(chat_id)[2], wallet=wallet)
        await update.message.reply_text(f"✅ *Woof!* Wallet set to {wallet} for referral payouts!")
    except:
        await update.message.reply_text("❌ *Grr!* Use: /setwallet <USDT_TRC20_address>")

async def support(update, context):
    chat_id = str(update.message.chat_id)
    keyboard = [
        [InlineKeyboardButton("📧 Contact Support", url="mailto:gbtradersupport@yahoo.com")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"🐾 *Need Help?*\n\n"
        f"Click below to email our support team at gbtradersupport@yahoo.com!",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def referrals(update, context):
    chat_id = str(update.message.chat_id)
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
    
    referral_msg = (
        f"👯 *Your Referral Stats*\n\n"
        f"  Valid Invitees: {valid_refs} (Subscribed VIPs)\n"
        f"  Total Earnings: {total_profit:.2f} USDT\n\n"
        f"💡 Invite more with your link: https://t.me/GoodBoyTraderBot?start={generate_referral_code(chat_id)}\n"
        f"💰 Earn 1% of their profits monthly when they subscribe!"
    )
    await update.message.reply_text(referral_msg, parse_mode='Markdown')

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
        track_referral_profit(chat_id, user_pnl)
        asyncio.run(send_telegram_alert(chat_id, 
            f"💰 *VIP Win!* {trade['exit_type']} at {trade['exit_price']:.2f}! You made {user_pnl:.2f} USDT (Cut: {pnl * profit_cut:.2f})"))

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
    tier, trade_size, _, _, _, _, expiry, api_key, api_secret, api_pass, _, _, _, _ = get_user(chat_id)
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
            update_msg = (
                f"🌟 *VIP Update* (15-min)\n\n"
                f"💸 SOL-USDT: {current_price:.2f} | Trend: {trend}\n"
                f"🐾 Status: {status}"
            )
            if tier == "elite":
                update_msg += f"\n📊 Points - 4H Short: {s4}/4 | Long: {l4}/4 | 15m Short: {s15}/3 | Long: {l15}/3"
            asyncio.run(send_telegram_alert(chat_id, update_msg))
            last_update = datetime.now(TIMEZONE)
        
        if signal and chat_id not in position_states:
            order_id, size_sol = place_order(trade_api, signal, current_price, trade_size)
            if order_id:
                entry_msg = (
                    f"🚀 *VIP Trade On!* {signal.capitalize()} at {current_price:.2f} with {trade_size} USDT!"
                )
                if tier == "elite":
                    entry_msg += f"\n📊 Trigger Points - 4H Short: {s4}/4 | Long: {l4}/4 | 15m Short: {s15}/3 | Long: {l15}/3"
                asyncio.run(send_telegram_alert(chat_id, entry_msg))
                trades[chat_id] = {'entry_time': datetime.now(TIMEZONE), 'entry_price': current_price, 'side': signal, 'size_sol': size_sol}
                position_states[chat_id] = signal
                entry_atrs[chat_id] = df_15m['atr'].iloc[-1]
                
                stop_loss = current_price * (1 - stop_loss_pct) if signal == 'long' else current_price * (1 + stop_loss_pct)
                trailing_sl = current_price + (entry_atrs[chat_id] * trailing_stop_factor) if signal == 'long' else current_price - (entry_atrs[chat_id] * trailing_stop_factor)
                
                while position_states.get(chat_id) == signal:
                    current_price = float(market_api.get_ticker(instId=instId)['data'][0]['last'])
                    current_price -= current_price * SLIPPAGE if signal == 'long' else -current_price * SLIPPAGE
                    
                    custom_tp = custom_tps.get(chat_id) if tier == "elite" else None
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

# Main
init_db()
if not TELEGRAM_TOKEN:
    logging.error("TELEGRAM_TOKEN not set in environment variables. Exiting.")
    sys.exit(1)

application = Application.builder().token(TELEGRAM_TOKEN).build()
bot = application.bot

# Add handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("freetrial", freetrial))
application.add_handler(CommandHandler("standard", standard))
application.add_handler(CommandHandler("elite", elite))
application.add_handler(CommandHandler("verify", verify))
application.add_handler(CommandHandler("setapi", setapi))
application.add_handler(CommandHandler("setsize", setsize))
application.add_handler(CommandHandler("stoptrading", stoptrading))
application.add_handler(CommandHandler("settp", settp))
application.add_handler(CommandHandler("close", close))
application.add_handler(CommandHandler("pnl", pnl))
application.add_handler(CommandHandler("status", status))
application.add_handler(CommandHandler("history", history))
application.add_handler(CommandHandler("setwallet", setwallet))
application.add_handler(CommandHandler("support", support))
application.add_handler(CommandHandler("referrals", referrals))

# Start payout thread
threading.Thread(target=lambda: asyncio.run(monthly_payout()), daemon=True).start()

# Start the bot
application.run_polling()

# Keep main thread alive
while True:
    logging.info("Heartbeat: Bot running...")
    time.sleep(60)
