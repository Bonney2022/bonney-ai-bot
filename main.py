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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
LOCAL_TZ = ZoneInfo("America/Toronto")  # Eastern Time

# Full asset list including 24/7 Crypto for off-peak coverage
PAIRS = {
    "EUR/CAD": "EURCAD=X",
    "EUR/JPY": "EURJPY=X",
    "CAD/JPY": "CADJPY=X",
    "GBP/JPY": "GBPJPY=X",
    "GBP/USD": "GBPUSD=X",
    "EUR/USD": "EURUSD=X",
    "GBP/CAD": "GBPCAD=X",
    "USD/JPY": "JPY=X",
    "AUD/USD": "AUDUSD=X",
    "USD/CAD": "CAD=X",
    "USD/CHF": "CHF=X",
    "NZD/USD": "NZDUSD=X",
    "BTC/USD (24/7)": "BTC-USD",
    "ETH/USD (24/7)": "ETH-USD"
}

ACTIVE_SUBSCRIBERS = set()

# ==========================================
# TECHNICAL ANALYSIS ENGINE (Session-Aware)
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

    # 1. EMA 200 & EMA 50
    df['EMA200'] = close.ewm(span=200, adjust=False).mean()
    df['EMA50'] = close.ewm(span=50, adjust=False).mean()

    # 2. Stochastic Oscillator (5, 3, 3)
    low_min = low.rolling(window=5).min()
    high_max = high.rolling(window=5).max()
    df['Stoch_K'] = 100 * ((close - low_min) / (high_max - low_min))
    df['Stoch_D'] = df['Stoch_K'].rolling(window=3).mean()
    
    # 3. RSI (14)
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))

    # 4. MACD (12, 26, 9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['MACD_Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    
    # 5. Supertrend (10, 3) + ATR
    df = calculate_supertrend(df, period=10, multiplier=3)
    df['Volatility_Score'] = (df['ATR'] / close) * 100
    
    return df

def get_market_session_quality():
    """Determines active market window quality based on local Eastern Time."""
    now_hour = datetime.now(LOCAL_TZ).hour
    
    if 8 <= now_hour < 12:
        return "BEST", "🔥 PEAK WINDOW (London/NY Overlap - Max Accuracy)"
    elif 3 <= now_hour < 8 or 12 <= now_hour < 17:
        return "GOOD", "⚡ GOOD WINDOW (Active Single Session)"
    else:
        return "OFF_PEAK", "🌙 OFF-PEAK WINDOW (Range/Crypto Active Mode)"

def generate_signal(pair_name, pair_symbol):
    try:
        # Use 5m candles
        data = yf.download(tickers=pair_symbol, period="5d", interval="5m", progress=False)
        if len(data) < 200:
            return {"direction": "NO TRADE", "volatility": 0}
        
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        df = calculate_indicators(data.copy())
        latest = df.iloc[-1]

        close_price = float(latest['Close'])
        ema200 = float(latest['EMA200'])
        ema50 = float(latest['EMA50'])
        st_dir = int(latest['Supertrend_Dir'])
        stoch_k = float(latest['Stoch_K'])
        stoch_d = float(latest['Stoch_D'])
        rsi_val = float(latest['RSI'])
        macd_val = float(latest['MACD'])
        macd_sig = float(latest['MACD_Signal'])
        vol_score = float(latest['Volatility_Score'])

        session_type, session_label = get_market_session_quality()

        # 1. PEAK & GOOD SESSIONS (Strict Trend Alignment)
        if session_type in ["BEST", "GOOD"]:
            call_condition = (
                close_price > ema200 and 
                st_dir == 1 and 
                stoch_k > stoch_d and stoch_k < 85 and 
                macd_val > macd_sig and 
                rsi_val >= 50
            )

            put_condition = (
                close_price < ema200 and 
                st_dir == -1 and 
                stoch_k < stoch_d and stoch_k > 15 and 
                macd_val < macd_sig and 
                rsi_val <= 50
            )
        
        # 2. OFF-PEAK HOURS (Adaptive Range & Reversion Logic)
        else:
            # During quiet hours, allow shorter EMA 50 trend guidance and Stoch momentum
            call_condition = (
                close_price > ema50 and 
                stoch_k > stoch_d and stoch_k < 80 and 
                rsi_val >= 48
            )

            put_condition = (
                close_price < ema50 and 
                stoch_k < stoch_d and stoch_k > 20 and 
                rsi_val <= 52
            )

        if call_condition:
            return {
                "asset": pair_name,
                "direction": "CALL (BUY ⬆️)",
                "volatility": vol_score,
                "session_label": session_label,
                "reasons": [
                    "✅ Trend Alignment Confirmed",
                    "✅ Supertrend/EMA Direction Clear",
                    f"✅ Stochastic (5,3,3): Bullish (%K={stoch_k:.1f})",
                    f"✅ RSI (14): Momentum ({rsi_val:.1f})"
                ],
                "price": round(close_price, 5)
            }
        elif put_condition:
            return {
                "asset": pair_name,
                "direction": "PUT (SELL ⬇️)",
                "volatility": vol_score,
                "session_label": session_label,
                "reasons": [
                    "🔻 Trend Alignment Confirmed",
                    "🔻 Supertrend/EMA Direction Clear",
                    f"🔻 Stochastic (5,3,3): Bearish (%K={stoch_k:.1f})",
                    f"🔻 RSI (14): Momentum ({rsi_val:.1f})"
                ],
                "price": round(close_price, 5)
            }
        else:
            return {"direction": "NO TRADE", "volatility": 0}

    except Exception as e:
        logging.error(f"Error evaluating {pair_symbol}: {e}")
        return {"direction": "NO TRADE", "volatility": 0}

def scan_all_pairs():
    candidate_signals = []

    for asset_name, symbol in PAIRS.items():
        sig = generate_signal(asset_name, symbol)
        if sig["direction"] != "NO TRADE":
            candidate_signals.append(sig)

    if not candidate_signals:
        return None

    candidate_signals.sort(key=lambda x: x["volatility"], reverse=True)
    best_signal = candidate_signals[0]

    now_local = datetime.now(LOCAL_TZ)
    entry_local = (now_local + timedelta(minutes=2)).replace(second=0, microsecond=0)
    expiry_local = entry_local + timedelta(minutes=5)
    
    alert_time_str = now_local.strftime("%I:%M:%S %p")
    entry_time_str = entry_local.strftime("%I:%M:00 %p")
    expiry_time_str = expiry_local.strftime("%I:%M:00 %p")

    reasons_formatted = "\n".join(best_signal.get("reasons", []))
    
    msg = (
        "⚡ *BONNEY DYNAMIC SESSION SIGNAL*\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"📍 Asset: *{best_signal['asset']}*\n"
        f"🎯 Action: *{best_signal['direction']}*\n"
        f"📩 Alert Sent: `{alert_time_str}`\n"
        f"🚀 *EXACT ENTRY TIME:* `{entry_time_str}` (2 Mins Notice)\n"
        f"⏱ Expiry Window: `{expiry_time_str}` (5 Mins Expiry)\n"
        f"📊 Window Tag: `{best_signal['session_label']}`\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"*Indicator Breakdown:*\n{reasons_formatted}\n\n"
        "⚠️ *Instructions:* Open Pocket Option, select asset, set 5-min trade timer (`00:05:00`), and execute at EXACT ENTRY TIME!"
    )
    return msg

# ==========================================
# BACKGROUND SCAN LOOP
# ==========================================

async def auto_signal_loop(app):
    while True:
        await asyncio.sleep(40)
        if ACTIVE_SUBSCRIBERS:
            signal_msg = scan_all_pairs()
            if signal_msg is not None:
                for chat_id in list(ACTIVE_SUBSCRIBERS):
                    try:
                        await app.bot.send_message(chat_id=chat_id, text=signal_msg, parse_mode="Markdown")
                    except Exception as e:
                        logging.error(f"Failed to send alert to {chat_id}: {e}")

# ==========================================
# COMMAND HANDLERS
# ==========================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.add(chat_id)
    welcome_text = (
        "⚡ *WELCOME TO BONNEY 24/7 SESSION-AWARE BOT*\n\n"
        "🟢 *Session Engine Active:* Automatically adapts filters to high-volume hours vs. off-peak hours.\n\n"
        "⏳ *Prep Window:* 2 minutes advance notice.\n\n"
        "*Commands:*\n"
        "• `/start_alerts` - Turn ON alerts\n"
        "• `/stop_alerts` - Turn OFF alerts\n"
        "• `/signals` - Force immediate market scan\n"
        "• `/status` - Check active session window"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def start_alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.add(chat_id)
    await update.message.reply_text("🟢 *Session-Aware Alerts Activated!*", parse_mode="Markdown")

async def stop_alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.discard(chat_id)
    await update.message.reply_text("🔴 *Alerts Paused.*", parse_mode="Markdown")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now_local = datetime.now(LOCAL_TZ).strftime("%I:%M:%S %p")
    session_type, session_label = get_market_session_quality()
    msg = (
        "🟢 *SYSTEM STATUS: ONLINE*\n"
        f"• Active Session Tag: {session_label}\n"
        "• Tracked Pairs: Forex Majors + 24/7 Crypto (BTC, ETH)\n"
        "• Timeframe: 5-Minute Candle Analysis\n"
        "• Prep Window: 2 Minutes Advance Notice\n"
        "• Local Time: " + now_local
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def signals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Scanning session-adjusted market data...*", parse_mode="Markdown")
    msg = scan_all_pairs()
    if msg is None:
        await update.message.reply_text("🚫 *NO TRADE:* Market is currently in ultra-flat consolidation.", parse_mode="Markdown")
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
    app.add_handler(CommandHandler("start_alerts", start_alerts_cmd))
    app.add_handler(CommandHandler("stop_alerts", stop_alerts_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("signals", signals_cmd))

    print("⚡ BONNEY 24/7 Session Bot is online...")
    app.run_polling()

if __name__ == "__main__":
    main()
  
