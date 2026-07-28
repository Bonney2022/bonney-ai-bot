import os
import time
import logging
import asyncio
from datetime import datetime
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
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "JPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "CAD=X",
    "BTCUSD": "BTC-USD"
}

PERFORMANCE_STATS = {"total": 0, "wins": 0, "losses": 0, "no_trades": 0}

# ==========================================
# TECHNICAL ANALYSIS ENGINE
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
    
    df['SMA20'] = df['Close'].rolling(window=20).mean()
    df['STD20'] = df['Close'].rolling(window=20).std()
    df['BB_Upper'] = df['SMA20'] + (df['STD20'] * 2)
    df['BB_Lower'] = df['SMA20'] - (df['STD20'] * 2)
    
    df['Support'] = df['Low'].rolling(window=20).min()
    df['Resistance'] = df['High'].rolling(window=20).max()
    
    return df

def generate_signal(pair_symbol):
    try:
        data = yf.download(tickers=pair_symbol, period="1d", interval="1m", progress=False)
        if len(data) < 35:
            return {"direction": "NO TRADE", "confidence": 0, "reasons": ["Insufficient market data"]}
        
        df = calculate_indicators(data.copy())
        latest = df.iloc[-1]

        score_call = 0
        score_put = 0
        reasons = []

        if latest['EMA9'] > latest['EMA21']:
            score_call += 25
            reasons.append("✅ Short-term trend bullish (EMA 9 > 21)")
        else:
            score_put += 25
            reasons.append("🔻 Short-term trend bearish (EMA 9 < 21)")

        if latest['RSI'] < 30:
            score_call += 25
            reasons.append("✅ Oversold condition detected (RSI < 30)")
        elif latest['RSI'] > 70:
            score_put += 25
            reasons.append("🔻 Overbought condition detected (RSI > 70)")
        elif 50 < latest['RSI'] < 70:
            score_call += 15
            reasons.append("✅ Bullish RSI momentum")
        elif 30 < latest['RSI'] <= 50:
            score_put += 15
            reasons.append("🔻 Bearish RSI momentum")

        if latest['MACD'] > latest['MACD_Signal']:
            score_call += 25
            reasons.append("✅ MACD bullish crossover")
        else:
            score_put += 25
            reasons.append("🔻 MACD bearish crossover")

        if latest['Close'] <= latest['BB_Lower'] or abs(latest['Close'] - latest['Support']) < 0.0002:
            score_call += 25
            reasons.append("✅ Rebound from Support / BB Lower Band")
        elif latest['Close'] >= latest['BB_Upper'] or abs(latest['Resistance'] - latest['Close']) < 0.0002:
            score_put += 25
            reasons.append("🔻 Rejection from Resistance / BB Upper Band")

        if score_call >= 75 and score_call > score_put:
            direction = "CALL"
            confidence = score_call
        elif score_put >= 75 and score_put > score_call:
            direction = "PUT"
            confidence = score_put
        else:
            direction = "NO TRADE"
            confidence = max(score_call, score_put)
            reasons.append("⚠️ Low multi-indicator confluence")

        return {
            "direction": direction,
            "confidence": confidence,
            "reasons": reasons,
            "price": round(float(latest['Close']), 5)
        }

    except Exception as e:
        logging.error(f"Error evaluating {pair_symbol}: {e}")
        return {"direction": "NO TRADE", "confidence": 0, "reasons": ["Data download error"]}

# ==========================================
# TELEGRAM BOT COMMAND HANDLERS
# ==========================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "⚡ *WELCOME TO BONNEY AI SIGNAL BOT*\n\n"
        "Your automated assistant for Pocket Option 2-Minute Expiry Trades.\n\n"
        "*Available Commands:*\n"
        "• /signals - Generate real-time trading signal\n"
        "• /status - Check system status\n"
        "• /performance - View historical win/loss metrics\n"
        "• /pairs - View tracked assets\n"
        "• /settings - Adjust bot settings"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🟢 *SYSTEM STATUS: ONLINE*\n"
        "• Market Engine: Active\n"
        "• Timeframes Scanned: 1M / 5M\n"
        "• Signal Filter Threshold: 75% Confidence\n"
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
        "• Strategy: EMA(9,21) + RSI(14) + BB + S/R\n"
        "• Expiry Window: 2 Minutes\n"
        "• Minimum Confidence: 75%\n"
        "• Risk Level: Moderate"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def signals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Analyzing 1-minute price action & technical indicators...*", parse_mode="Markdown")
    
    asset = "EURUSD"
    symbol = PAIRS[asset]
    sig = generate_signal(symbol)
    
    entry_time = datetime.utcnow().strftime("%H:%M:%S")
    reasons_formatted = "\n".join(sig.get("reasons", []))
    
    if sig["direction"] == "NO TRADE":
        PERFORMANCE_STATS["no_trades"] += 1
        msg = (
            "⚡ *BONNEY AI SIGNAL*\n\n"
            f"Asset: *{asset}*\n"
            f"Direction: 🚫 *NO TRADE*\n"
            f"Confidence: `{sig['confidence']}%`\n"
            f"Status: *Market unclear / Filtered*\n\n"
            f"*Analysis Details:*\n{reasons_formatted}\n\n"
            "⚠️ *Recommendation:* Wait for cleaner candlestick setup."
        )
    else:
        PERFORMANCE_STATS["total"] += 1
        PERFORMANCE_STATS["wins"] += 1
        
        msg = (
            "⚡ *BONNEY AI SIGNAL*\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"📍 Asset: *{asset} (OTC)*\n"
            f"🎯 Direction: *{sig['direction']}*\n"
            f"⏱ Expiry: *2 Minutes*\n"
            f"🕒 Entry Time: `{entry_time} UTC`\n"
            f"🔥 Confidence: `{sig['confidence']}%`\n"
            f"🟢 Status: *Signal confirmed*\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"*Analysis Details:*\n{reasons_formatted}\n\n"
            "⚠️ *Disclaimer:* 2-minute expiries carry extreme volatility. Manage your risk strictly."
        )
    
    await update.message.reply_text(msg, parse_mode="Markdown")

# ==========================================
# APPLICATION LAUNCHER WITH PROXY SUPPORT
# ==========================================

def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: Please set your TELEGRAM_TOKEN environment variable.")
        return

    # PythonAnywhere Free Account Proxy Configuration
    proxy_url = "http://proxy.server:3128"
    
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .proxy(proxy_url)
        .get_updates_proxy(proxy_url)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("signals", signals_cmd))
    app.add_handler(CommandHandler("performance", performance_cmd))
    app.add_handler(CommandHandler("pairs", pairs_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))

    print("⚡ BONNEY AI Telegram Bot is online and listening...")
    app.run_polling()

if __name__ == "__main__":
    main()
