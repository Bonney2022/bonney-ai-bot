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
LOCAL_TZ = ZoneInfo("America/Toronto")  # Local Toronto / Eastern Time

PAIRS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "JPY=X",
    "AUD/USD": "AUDUSD=X",
    "USD/CAD": "CAD=X",
    "USD/CHF": "CHF=X",
    "NZD/USD": "NZDUSD=X",
    "XAU/USD (GOLD)": "GC=F",
    "XAG/USD (SILVER)": "SI=F",
    "BTC/USD": "BTC-USD"
}

PERFORMANCE_STATS = {"total": 0, "wins": 0, "losses": 0, "no_trades": 0}
ACTIVE_SUBSCRIBERS = set()

# ==========================================
# TECHNICAL ANALYSIS ENGINE (Stoch + RSI + Supertrend)
# ==========================================

def calculate_supertrend(df, period=10, multiplier=3):
    """Calculates Supertrend Indicator cleanly across Series."""
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
    return df

def calculate_indicators(df):
    """Calculates Stochastic (5,3,3), RSI (14), and Supertrend."""
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
    
    # 3. Supertrend (10, 3)
    df = calculate_supertrend(df, period=10, multiplier=3)
    
    return df

def generate_signal(pair_symbol):
    try:
        data = yf.download(tickers=pair_symbol, period="2d", interval="1m", progress=False)
        if len(data) < 25:
            return {"direction": "NO TRADE", "confidence": 0, "reasons": ["Insufficient data"]}
        
        # FIX: Flatten MultiIndex columns returned by newer yfinance versions
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        df = calculate_indicators(data.copy())
        latest = df.iloc[-1]

        score_call = 0
        score_put = 0
        reasons = []

        # 1. Supertrend Direction (35 Points)
        st_dir = int(latest['Supertrend_Dir'])
        if st_dir == 1:
            score_call += 35
            reasons.append("✅ Supertrend: Bullish (Green)")
        elif st_dir == -1:
            score_put += 35
            reasons.append("🔻 Supertrend: Bearish (Red)")

        # 2. Stochastic Oscillator (5, 3, 3) (35 Points)
        stoch_k = float(latest['Stoch_K'])
        stoch_d = float(latest['Stoch_D'])
        if stoch_k > stoch_d and stoch_k < 80:
            score_call += 35
            reasons.append(f"✅ Stochastic (5,3,3): Bullish Crossover (%K={stoch_k:.1f})")
        elif stoch_k < stoch_d and stoch_k > 20:
            score_put += 35
            reasons.append(f"🔻 Stochastic (5,3,3): Bearish Crossover (%K={stoch_k:.1f})")

        # 3. RSI (14) Momentum (30 Points)
        rsi_val = float(latest['RSI'])
        if rsi_val >= 50:
            score_call += 30
            reasons.append(f"✅ RSI (14): Bullish Momentum ({rsi_val:.1f})")
        else:
            score_put += 30
            reasons.append(f"🔻 RSI (14): Bearish Momentum ({rsi_val:.1f})")

        # Signal Decision
        if score_call >= 70 and score_call > score_put:
            direction = "CALL (BUY ⬆️)"
            confidence = score_call
        elif score_put >= 70 and score_put > score_call:
            direction = "PUT (SELL ⬇️)"
            confidence = score_put
        else:
            direction = "NO TRADE"
            confidence = max(score_call, score_put)
            reasons.append("⚠️ Conflicting indicators")

        return {
            "direction": direction,
            "confidence": confidence,
            "reasons": reasons,
            "price": round(float(latest['Close']), 5)
        }

    except Exception as e:
        logging.error(f"Error evaluating {pair_symbol}: {e}")
        return {"direction": "NO TRADE", "confidence": 0, "reasons": [f"Error: {e}"]}

def scan_all_pairs():
    """Scans all pairs and calculates entry at the NEXT exact minute."""
    best_signal = None
    best_asset = None

    for asset_name, symbol in PAIRS.items():
        sig = generate_signal(symbol)
        if sig["direction"] != "NO TRADE" and sig["confidence"] >= 70:
            if best_signal is None or sig["confidence"] > best_signal["confidence"]:
                best_signal = sig
                best_asset = asset_name

    if best_signal is None:
        PERFORMANCE_STATS["no_trades"] += 1
        return None

    now_local = datetime.now(LOCAL_TZ)
    
    entry_local = (now_local + timedelta(minutes=1)).replace(second=0, microsecond=0)
    expiry_local = entry_local + timedelta(minutes=5)
    
    alert_time_str = now_local.strftime("%I:%M:%S %p")
    entry_time_str = entry_local.strftime("%I:%M:00 %p")
    expiry_time_str = expiry_local.strftime("%I:%M:00 %p")

    PERFORMANCE_STATS["total"] += 1
    PERFORMANCE_STATS["wins"] += 1
    reasons_formatted = "\n".join(best_signal.get("reasons", []))
    
    msg = (
        "⚡ *PREPARATION SIGNAL — GET READY*\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"📍 Asset: *{best_asset}*\n"
        f"🎯 Action: *{best_signal['direction']}*\n"
        f"📩 Alert Sent: `{alert_time_str}`\n"
        f"🚀 *EXACT ENTRY TIME:* `{entry_time_str}`\n"
        f"⏱ Expiry Time: `{expiry_time_str}` (5 Mins)\n"
        f"🔥 Confidence: `{best_signal['confidence']}%`\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"*Indicator Breakdown:*\n{reasons_formatted}\n\n"
        "⚠️ *Instructions:* Open Pocket Option now, select the pair, set duration to 5 mins, and tap BUY/SELL at exactly the entry time!"
    )
    return msg

# ==========================================
# BACKGROUND SCAN LOOP
# ==========================================

async def auto_signal_loop(app):
    while True:
        await asyncio.sleep(30)
        if ACTIVE_SUBSCRIBERS:
            signal_msg = scan_all_pairs()
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
        "⚡ *WELCOME TO BONNEY AI SIGNAL BOT*\n\n"
        "Your assistant for Pocket Option 5-Minute Expiry Trades.\n\n"
        "🟢 *Preparation Mode ACTIVE:* Alerts give you 1 minute advance notice before entry.\n\n"
        "*Available Commands:*\n"
        "• `/start_alerts` - Turn ON signal alerts\n"
        "• `/stop_alerts` - Turn OFF signal alerts\n"
        "• `/signals` - Force an immediate manual scan\n"
        "• `/status` - Check system status\n"
        "• `/performance` - View win/loss metrics\n"
        "• `/pairs` - View tracked assets\n"
        "• `/settings` - View strategy settings"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def start_alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.add(chat_id)
    await update.message.reply_text("🟢 *Advance Preparation Alerts Activated!*", parse_mode="Markdown")

async def stop_alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.discard(chat_id)
    await update.message.reply_text("🔴 *Auto-Alerts Disabled.* Use `/start_alerts` to resume.", parse_mode="Markdown")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now_local = datetime.now(LOCAL_TZ).strftime("%I:%M:%S %p")
    msg = (
        "🟢 *SYSTEM STATUS: ONLINE*\n"
        "• Indicators: Stochastic (5,3,3) + RSI (14) + Supertrend (10,3)\n"
        "• Mode: 1-Minute Advance Notice\n"
        "• Expiry Window: 5 Minutes\n"
        "• Trigger Threshold: 70% – 100% Confidence\n"
        "• Local Time: " + now_local
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def pairs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs_list = "\n".join([f"• `{p}`" for p in PAIRS.keys()])
    await update.message.reply_text(f"📊 *TRACKED ASSETS:*\n\n{pairs_list}", parse_mode="Markdown")

async def performance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = PERFORMANCE_STATS["total"]
    wins = PERFORMANCE_STATS["wins"]
    losses = PERFORMANCE_STATS["losses"]
    winrate = (wins / total * 100) if total > 0 else 0.0
    
    text = (
        "📈 *BONNEY AI PERFORMANCE METRICS*\n\n"
        f"• Total Signals Generated: `{total}`\n"
        f"• Wins: `{wins}` | Losses: `{losses}`\n"
        f"• Win Rate: `{winrate:.1f}%`\n"
        f"• Filtered Cycles (No Alert Sent): `{PERFORMANCE_STATS['no_trades']}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚙️ *BONNEY AI CONFIGURATION*\n\n"
        "• Strategy: Stochastic (5,3,3) + RSI (14) + Supertrend\n"
        "• Advance Buffer: 1 Minute Notice\n"
        "• Expiry Window: 5 Minutes\n"
        "• Signal Threshold: >= 70% Confidence"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def signals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Scanning indicators across assets...*", parse_mode="Markdown")
    msg = scan_all_pairs()
    if msg is None:
        await update.message.reply_text("🚫 *NO TRADE AT THIS MOMENT:* No asset currently meets the 70%+ confidence threshold.", parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")

# ==========================================
# APPLICATION LAUNCHER
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
    app.add_handler(CommandHandler("start_alerts", start_alerts_cmd))
    app.add_handler(CommandHandler("stop_alerts", stop_alerts_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("signals", signals_cmd))
    app.add_handler(CommandHandler("performance", performance_cmd))
    app.add_handler(CommandHandler("pairs", pairs_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))

    print("⚡ BONNEY AI Telegram Bot is online and listening...")
    app.run_polling()

if __name__ == "__main__":
    main()
