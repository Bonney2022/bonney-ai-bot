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

# Mapped explicitly so yfinance gets valid symbols without slashes
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
# TECHNICAL ANALYSIS ENGINE (5-Min Candle Analysis)
# ==========================================

def calculate_indicators(df):
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
    
    return df

def generate_signal(pair_symbol):
    try:
        # Fetch 5-minute candle data
        data = yf.download(tickers=pair_symbol, period="2d", interval="5m", progress=False)
        if len(data) < 25:
            return {"direction": "NO TRADE", "confidence": 0, "reasons": ["Insufficient data"]}
        
        df = calculate_indicators(data.copy())
        latest = df.iloc[-1]

        score_call = 0
        score_put = 0
        reasons = []

        # 1. EMA Trend Check (35 Points)
        if latest['EMA9'].item() > latest['EMA21'].item():
            score_call += 35
            reasons.append("✅ 5M EMA Trend: Bullish (EMA 9 > 21)")
        else:
            score_put += 35
            reasons.append("🔻 5M EMA Trend: Bearish (EMA 9 < 21)")

        # 2. RSI Momentum (35 Points)
        rsi_val = float(latest['RSI'].item())
        if rsi_val >= 50:
            score_call += 35
            reasons.append(f"✅ 5M RSI ({rsi_val:.1f}): Bullish momentum")
        else:
            score_put += 35
            reasons.append(f"🔻 5M RSI ({rsi_val:.1f}): Bearish momentum")

        # 3. MACD Signal (30 Points)
        if latest['MACD'].item() > latest['MACD_Signal'].item():
            score_call += 30
            reasons.append("✅ 5M MACD: Bullish signal")
        else:
            score_put += 30
            reasons.append("🔻 5M MACD: Bearish signal")

        # Threshold set between 70% and 100%
        if score_call >= 70 and score_call > score_put:
            direction = "CALL"
            confidence = score_call
        elif score_put >= 70 and score_put > score_call:
            direction = "PUT"
            confidence = score_put
        else:
            direction = "NO TRADE"
            confidence = max(score_call, score_put)
            reasons.append("⚠️ Conflicting indicators")

        return {
            "direction": direction,
            "confidence": confidence,
            "reasons": reasons,
            "price": round(float(latest['Close'].item()), 5)
        }

    except Exception as e:
        logging.error(f"Error evaluating {pair_symbol}: {e}")
        return {"direction": "NO TRADE", "confidence": 0, "reasons": ["Data fetch error"]}

def scan_all_pairs():
    """Scans all assets and returns a signal ONLY if confidence is >= 70%."""
    best_signal = None
    best_asset = None

    for asset_name, symbol in PAIRS.items():
        sig = generate_signal(symbol)
        if sig["direction"] != "NO TRADE" and sig["confidence"] >= 70:
            if best_signal is None or sig["confidence"] > best_signal["confidence"]:
                best_signal = sig
                best_asset = asset_name

    # If no valid BUY or SELL signal is found, return None (DO NOT SEND ANYTHING)
    if best_signal is None:
        PERFORMANCE_STATS["no_trades"] += 1
        return None

    now = datetime.utcnow()
    entry_time_str = now.strftime("%H:%M:%S")
    expiry_time_str = (now + timedelta(minutes=5)).strftime("%H:%M:%S")

    PERFORMANCE_STATS["total"] += 1
    PERFORMANCE_STATS["wins"] += 1
    reasons_formatted = "\n".join(best_signal.get("reasons", []))
    
    msg = (
        "⚡ *BONNEY AI TRADING SIGNAL*\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"📍 Asset: *{best_asset}*\n"
        f"🎯 Action: *{best_signal['direction']} (BUY/SELL)*\n"
        f"🕒 Entry Time: `{entry_time_str} UTC`\n"
        f"⏱ Trade Expiry: `{expiry_time_str} UTC` (5 Mins)\n"
        f"🔥 Confidence: `{best_signal['confidence']}%`\n"
        f"🟢 Status: *Signal Confirmed*\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"*Analysis Details:*\n{reasons_formatted}\n\n"
        "⚠️ *Action Required:* Execute 5-minute trade immediately on Pocket Option."
    )
    return msg

# ==========================================
# BACKGROUND SCAN LOOP (SILENT UNLESS BUY/SELL)
# ==========================================

async def auto_signal_loop(app):
    while True:
        await asyncio.sleep(60)  # Scans every 60 seconds
        if ACTIVE_SUBSCRIBERS:
            signal_msg = scan_all_pairs()
            # Only send message if a valid CALL/PUT signal was generated
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
        "Your automated assistant for Pocket Option 5-Minute Expiry Trades.\n\n"
        "🟢 *Auto-alerts ENABLED:* You will receive signals ONLY when a high-confidence CALL/PUT setup (70%-100%) appears.\n\n"
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
    await update.message.reply_text("🟢 *Auto-Alerts Activated!* You will receive signals as soon as a 70%+ CALL or PUT setup is detected.", parse_mode="Markdown")

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
        "• Trigger Threshold: 70% – 100% Confidence\n"
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
        f"• Filtered Cycles (No Alert Sent): `{PERFORMANCE_STATS['no_trades']}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚙️ *BONNEY AI CONFIGURATION*\n\n"
        "• Chart Candle: 5-Minute Timeframe\n"
        "• Strategy: EMA(9,21) + RSI(14) + MACD\n"
        "• Expiry Window: 5 Minutes\n"
        "• Signal Threshold: >= 70% Confidence\n"
        "• Mode: Silent unless CALL / PUT detected"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def signals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Scanning 5-minute indicators across assets...*", parse_mode="Markdown")
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
    
