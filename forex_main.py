import os
import time
import logging
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import numpy as np
import yfinance as yf
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Global Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
LOCAL_TZ = ZoneInfo("America/Toronto")  # Local Time

# Focused on high-volume, high-profit assets
PAIRS = {
    "XAU/USD (GOLD)": "GC=F",
    "BTC/USD (BITCOIN)": "BTC-USD",
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X"
}

ACTIVE_SUBSCRIBERS = set()

# ==========================================
# FOREX TECHNICAL ANALYSIS ENGINE (ATR + Supertrend + Stoch + RSI)
# ==========================================

def calculate_supertrend(df, period=10, multiplier=3):
    high = df['High'].squeeze()
    low = df['Low'].squeeze()
    close = df['Close'].squeeze()
    
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    
    hl2 = (high + low) / 2
    final_upperband = hl2 + (multiplier * atr)
    final_lowerband = hl2 - (multiplier * atr)
    
    supertrend = pd.Series(index=df.index, dtype='float64')
    direction = pd.Series(index=df.index, dtype='int64')
    
    for i in range(period, len(df)):
        c_val = float(close.iloc[i])
        u_val = float(final_upperband.iloc[i-1])
        l_val = float(final_lowerband.iloc[i-1])
        prev_dir = int(direction.iloc[i-1]) if i > period else 1

        if c_val > u_val:
            dir_val = 1
        elif c_val < l_val:
            dir_val = -1
        else:
            dir_val = prev_dir

        direction.iloc[i] = dir_val

        if dir_val == 1:
            supertrend.iloc[i] = float(final_lowerband.iloc[i])
        else:
            supertrend.iloc[i] = float(final_upperband.iloc[i])
            
    df['Supertrend'] = supertrend
    df['Supertrend_Dir'] = direction
    df['ATR'] = atr
    return df

def calculate_indicators(df):
    close = df['Close'].squeeze()
    low = df['Low'].squeeze()
    high = df['High'].squeeze()

    # 1. Stochastic Oscillator (5, 3, 3)
    low_min = low.rolling(window=5).min()
    high_max = high.rolling(window=5).max()
    df['Stoch_K'] = 100 * ((close - low_min) / (high_max - low_min))
    df['Stoch_D'] = df['Stoch_K'].rolling(window=3).mean()
    
    # 2. RSI (14)
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # 3. Supertrend + ATR
    df = calculate_supertrend(df, period=10, multiplier=3)
    
    return df

def generate_forex_signal(pair_name, pair_symbol):
    try:
        data = yf.download(tickers=pair_symbol, period="5d", interval="15m", progress=False)
        if len(data) < 30:
            return None
        
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        df = calculate_indicators(data.copy())
        latest = df.iloc[-1]

        score_buy = 0
        score_sell = 0

        # Supertrend
        st_dir = int(latest['Supertrend_Dir'])
        if st_dir == 1:
            score_buy += 35
        elif st_dir == -1:
            score_sell += 35

        # Stochastic
        stoch_k = float(latest['Stoch_K'])
        stoch_d = float(latest['Stoch_D'])
        if stoch_k > stoch_d and stoch_k < 80:
            score_buy += 35
        elif stoch_k < stoch_d and stoch_k > 20:
            score_sell += 35

        # RSI
        rsi_val = float(latest['RSI'])
        if rsi_val >= 50:
            score_buy += 30
        else:
            score_sell += 30

        # Determine signal direction
        if score_buy >= 70 and score_buy > score_sell:
            action = "BUY 📈"
            confidence = score_buy
        elif score_sell >= 70 and score_sell > score_buy:
            action = "SELL 📉"
            confidence = score_sell
        else:
            return None

        entry_price = float(latest['Close'])
        atr_val = float(latest['ATR'])
        
        # Risk Management Calculations
        sl_distance = atr_val * 1.5
        
        if "BUY" in action:
            sl = entry_price - sl_distance
            tp1 = entry_price + (sl_distance * 1.0)
            tp2 = entry_price + (sl_distance * 2.0)
            tp3 = entry_price + (sl_distance * 3.0)
        else: # SELL
            sl = entry_price + sl_distance
            tp1 = entry_price - (sl_distance * 1.0)
            tp2 = entry_price - (sl_distance * 2.0)
            tp3 = entry_price - (sl_distance * 3.0)

        # Precision rounding
        digits = 2 if ("GOLD" in pair_name or "BTC" in pair_name or "JPY" in pair_symbol) else 5

        return {
            "asset": pair_name,
            "action": action,
            "confidence": confidence,
            "entry": round(entry_price, digits),
            "sl": round(sl, digits),
            "tp1": round(tp1, digits),
            "tp2": round(tp2, digits),
            "tp3": round(tp3, digits)
        }

    except Exception as e:
        logging.error(f"Forex Signal Error {pair_symbol}: {e}")
        return None

def scan_forex_pairs():
    best_signal = None

    for asset_name, symbol in PAIRS.items():
        sig = generate_forex_signal(asset_name, symbol)
        if sig is not None:
            if best_signal is None or sig["confidence"] > best_signal["confidence"]:
                best_signal = sig

    if best_signal is None:
        return None

    now_local = datetime.now(LOCAL_TZ)
    # 5-minute advance buffer
    entry_time_target = now_local + timedelta(minutes=5)

    alert_time_str = now_local.strftime("%I:%M:%S %p EST")
    entry_time_str = entry_time_target.strftime("%I:%M:00 %p EST")

    msg = (
        "📊 *BONNEY FOREX SIGNAL — 5-MIN ADVANCE NOTICE*\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"📍 Asset: *{best_signal['asset']}*\n"
        f"🎯 Order Type: *{best_signal['action']}*\n"
        f"📩 Signal Issued: `{alert_time_str}`\n"
        f"⏳ *Target Entry Time:* `{entry_time_str}`\n"
        f"🔥 Confidence: `{best_signal['confidence']}%`\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"💵 *Entry Price:* `{best_signal['entry']}`\n"
        f"🛑 *Stop Loss (SL):* `{best_signal['sl']}`\n"
        f"🎯 *Take Profit 1 (TP1):* `{best_signal['tp1']}` (Safe Exit)\n"
        f"🎯 *Take Profit 2 (TP2):* `{best_signal['tp2']}` (Standard)\n"
        f"🎯 *Take Profit 3 (TP3):* `{best_signal['tp3']}` (Extended Run)\n"
        "━━━━━━━━━━━━━━━━━\n"
        "⚠️ *Execution:* Set your Buy/Sell Limit or Market Order 5 minutes after receiving this alert!"
    )
    return msg

# ==========================================
# BACKGROUND SCAN LOOP
# ==========================================

async def auto_signal_loop(app):
    while True:
        await asyncio.sleep(60)  # Checks every 60 seconds
        if ACTIVE_SUBSCRIBERS:
            signal_msg = scan_forex_pairs()
            if signal_msg is not None:
                for chat_id in list(ACTIVE_SUBSCRIBERS):
                    try:
                        await app.bot.send_message(chat_id=chat_id, text=signal_msg, parse_mode="Markdown")
                    except Exception as e:
                        logging.error(f"Failed to send alert to {chat_id}: {e}")

# ==========================================
# TELEGRAM BOT COMMAND HANDLERS
# ==========================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.add(chat_id)
    welcome_text = (
        "📊 *WELCOME TO BONNEY FOREX SIGNAL BOT*\n\n"
        "🟢 *5-Minute Advance Alerts ACTIVE:* Provides Entry, SL, TP1, TP2, and TP3.\n\n"
        "*Commands:*\n"
        "• `/signals` - Force immediate Forex market scan\n"
        "• `/stop_alerts` - Turn off alerts"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def stop_alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.discard(chat_id)
    await update.message.reply_text("🔴 *Forex Alerts Disabled.*", parse_mode="Markdown")

async def signals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Scanning Forex markets for Entry, SL, and TPs...*", parse_mode="Markdown")
    msg = scan_forex_pairs()
    if msg is None:
        await update.message.reply_text("🚫 *NO FOREX TRADE:* No high-probability setup found right now.", parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")

# ==========================================
# LAUNCHER
# ==========================================

async def post_init(app):
    asyncio.create_task(auto_signal_loop(app))

def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: Please set your TELEGRAM_TOKEN environment variable.")
        return

    proxy_url = "http://proxy.server:3128"
    
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .proxy(proxy_url)
        .get_updates_proxy(proxy_url)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("stop_alerts", stop_alerts_cmd))
    app.add_handler(CommandHandler("signals", signals_cmd))

    print("📊 BONNEY Forex Bot is online...")
    app.run_polling()

if __name__ == "__main__":
    main()
  
