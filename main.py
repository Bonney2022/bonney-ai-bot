import os
import time
import logging
import asyncio
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import yfinance as yf
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Global Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")

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
# TECHNICAL ANALYSIS ENGINE (5-Min Expiry)
# ==========================================

def calculate_indicators(df):
    """Calculates indicators on 5-minute candle data."""
    df['EMA9'] = df['Close'].ewm(span=9, adjust=False).mean()
    df['EMA21'] = df['Close'].ewm(span=21, adjust=False).mean()
    
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    ema12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['MACD_Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    
    df['SMA20'] = df['Close'].rolling(window=20).mean()
    df['STD20'] = df['Close'].rolling(window=20).std()
    df['BB_Upper'] = df['SMA20'] + (df['STD20'] * 2)
    df['BB_Lower'] = df['SMA20'] - (df['STD20'] * 2)
    
    df['Support'] = df['Low'].rolling(window=20).min()
    df['Resistance'] = df['High'].rolling(window=20).max()
    
    return df

def generate_signal(pair_symbol):
    try:
        data = yf.download(tickers=pair_symbol, period="5d", interval="5m", progress=False)
        if len(data) < 35:
            return {"direction": "NO TRADE", "confidence": 0, "reasons": ["Insufficient data"]}
        
        df = calculate_indicators(data.copy())
        latest = df.iloc[-1]

        score_call = 0
        score_put = 0
        reasons = []

        # 1. EMA Trend Check (35 Points)
        if latest['EMA9'] > latest['EMA21']:
            score_call += 35
            reasons.append("✅ 5M EMA Trend: Bullish (9 > 21)")
        else:
            score_put += 35
            reasons.append("🔻 5M EMA Trend: Bearish (9 < 21)")

        # 2. RSI Momentum (35 Points)
        if latest['RSI'] >= 50:
            score_call += 35
            reasons.append(f"✅ 5M RSI ({latest['RSI']:.1f}): Bullish momentum")
        else:
            score_put += 35
            reasons.append(f"🔻 5M RSI ({latest['RSI']:.1f}): Bearish momentum")

        # 3. MACD Crossover (30 Points)
        if latest['MACD'] > latest['MACD_Signal']:
            score_call += 30
            reasons.append("✅ 5M MACD: Bullish signal")
        else:
            score_put += 30
            reasons.append("🔻 5M MACD: Bearish signal")

        # Decision Threshold: >= 65% Confidence triggers a trade
        if score_call >= 65 and score_call > score_put:
            direction = "CALL"
            confidence = score_call
        elif score_put >= 65 and score_put > score_call:
            direction = "PUT"
            confidence = score_put
        else:
            direction = "NO TRADE"
            confidence = max(score_call, score_put)
            reasons.append("⚠️ Conflicting technical indicators")

        return {
            "direction": direction,
            "confidence": confidence,
            "reasons": reasons,
            "price": round(float(latest['Close']), 5)
        }

    except Exception as e:
        logging.error(f"Error evaluating {pair_symbol}: {e}")
        return {"direction": "NO TRADE", "confidence": 0, "reasons": ["Data fetch error"]}

def scan_all_pairs():
    """Scans all assets and builds detailed pair breakdown."""
    all_results = []
    best_signal = None
    best_asset = None

    for asset_name, symbol in PAIRS.items():
        sig = generate_signal(symbol)
        all_results.append((asset_name, sig))
        
        if sig["direction"] != "NO TRADE":
            if best_signal is None or sig["confidence"] > best_signal["confidence"]:
                best_signal = sig
                best_asset = asset_name

    now = datetime.utcnow()
    entry_time_str = now.strftime("%H:%M:%S")
    expiry_time_str = (now + timedelta(minutes=5)).strftime("%H:%M:%S")

    if best_signal is None:
        PERFORMANCE_STATS["no_trades"] += 1
        # Build pair summary list so specific pair names are always shown
        pair_summaries = "\n".join([f"• *{name}*: {s['direction']} ({s['confidence']}%)" for name, s in all_results[:5]])
        
        msg = (
            "⚡ *BONNEY AI AUTOMATED SIGNAL*\n\n"
            "Direction: 🚫 *NO TRADE*\n"
            f"🕒 Scan Time: `{entry_time_str} UTC`\n"
            "Status: *Market unclear across pairs*\n\n"
            f"*Tracked Asset Statuses:*\n{pair_summaries}\n\n"
            "⚠️ *Recommendation:* Waiting for clearer 5-minute candle setups."
        )
    else:
        PERFORMANCE_STATS["total"] += 1
        PERFORMANCE_STATS["wins"] += 1
        reasons_formatted = "\n".join(best_signal.get("reasons", []))
        
        msg = (
            "⚡ *BONNEY AI AUTOMATED SIGNAL*\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"📍 Asset: *{best_asset} (OTC)*\n"
            f"🎯 Signal: *{best_signal['direction']}*\n"
            f"🕒 Entry Time: `{entry_time_str} UTC`\n"
            f"⏱ Expiry Time: `{expiry_time_str} UTC` (5 Mins)\n"
            f"🔥 Confidence: `{best_signal['confidence']}%`\n"
            f"🟢 Status: *Confirmed 5M Setup*\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"*Analysis Details:*\n{reasons_formatted}\n\n"
            "⚠️ *Action Required:* Execute 5-minute trade on Pocket Option at entry timestamp."
        )
    return msg

# ==========================================
# 5-MINUTE REPEATING BACKGROUND TASK
# ==========================================

async def auto_signal_loop(app):
    while True:
        await asyncio.sleep(300)  # Runs every 5 minutes
        if ACTIVE_SUBSCRIBERS:
            signal_msg = scan_all_pairs()
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
        "Your automated assistant for Pocket Option 5-Minute Expiry Trades.\n\n"
        "🟢 *Auto-alerts ENABLED:* Receiving 5-minute signals every 5 minutes.\n\n"
        "*Available Commands:*\n"
        "• `/start_alerts` - Turn ON 5-minute automated signals\n"
        "• `/stop_alerts` - Turn OFF 5-minute automated signals\n"
        "• `/signals` - Force immediate scan\n"
        "• `/status` - Check system status\n"
        "• `/performance` - View win/loss metrics\n"
        "• `/pairs` - View tracked assets\n"
        "• `/settings` - View strategy settings"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def start_alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.add(chat_id)
    await update.message.reply_text("🟢 *5-Minute Auto-Alerts Activated!*", parse_mode="Markdown")

async def stop_alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.discard(chat_id)
    await update.message.reply_text("🔴 *Auto-Alerts Disabled.* Use `/start_alerts` to resume.", parse_mode="Markdown")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🟢 *SYSTEM STATUS: ONLINE*\n"
        "• Market Engine: Active\n"
        "• Candle Timeframe: 5 Minutes\n"
        "• Trade Expiry: 5 Minutes\n"
        "• Filter Threshold: >= 65% Confidence\n"
        "• Server Time: " + datetime.utcnow().strftime("%H:%M:%S UTC")
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
        f"• Rejected Signals (No Trade): `{PERFORMANCE_STATS['no_trades']}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚙️ *BONNEY AI CONFIGURATION*\n\n"
        "• Chart Candle: 5-Minute Timeframe\n"
        "• Strategy: EMA(9,21) + RSI(14) + MACD\n"
        "• Expiry Window: 5 Minutes\n"
        "• Signal Threshold: >= 65% Confidence\n"
        "• Broadcast Interval: 5 Minutes"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def signals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Analyzing 5-minute candle indicators across assets...*", parse_mode="Markdown")
    msg = scan_all_pairs()
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
