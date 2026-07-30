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

PAIRS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "JPY=X",
    "AUD/USD": "AUDUSD=X",
    "USD/CAD": "CAD=X",
    "USD/CHF": "CHF=X",
    "NZD/USD": "NZDUSD=X",
    "XAU/USD (GOLD)": "GC=F",
    "BTC/USD": "BTC-USD"
}

PERFORMANCE_STATS = {"total": 0, "wins": 0, "losses": 0, "no_trades": 0}
ACTIVE_SUBSCRIBERS = set()

# ==========================================
# TECHNICAL ENGINE (EMA200 + Supertrend + Stoch + MACD + RSI)
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
    return df

def calculate_indicators(df):
    close = df['Close'].squeeze()
    low = df['Low'].squeeze()
    high = df['High'].squeeze()

    # 1. EMA 200 Macro Trend Baseline
    df['EMA200'] = close.ewm(span=200, adjust=False).mean()

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
    df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']
    
    # 5. Supertrend (10, 3)
    df = calculate_supertrend(df, period=10, multiplier=3)
    
    return df

def generate_signal(pair_symbol):
    try:
        # Use 5-minute candles to filter out high-frequency noise
        data = yf.download(tickers=pair_symbol, period="5d", interval="5m", progress=False)
        if len(data) < 200:
            return {"direction": "NO TRADE", "confidence": 0, "reasons": ["Insufficient history for EMA200"]}
        
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        df = calculate_indicators(data.copy())
        latest = df.iloc[-1]

        close_price = float(latest['Close'])
        ema200 = float(latest['EMA200'])
        st_dir = int(latest['Supertrend_Dir'])
        stoch_k = float(latest['Stoch_K'])
        stoch_d = float(latest['Stoch_D'])
        rsi_val = float(latest['RSI'])
        macd_val = float(latest['MACD'])
        macd_sig = float(latest['MACD_Signal'])

        # QUAD-LOCK STRICT UNANIMITY CONDITIONS
        call_condition = (
            close_price > ema200 and         # Price ABOVE EMA200
            st_dir == 1 and                  # Supertrend is Green
            stoch_k > stoch_d and stoch_k < 80 and  # Stochastic Bullish Crossover
            macd_val > macd_sig and          # MACD Bullish Crossover (Green Line above Signal)
            rsi_val >= 52                    # RSI Momentum
        )

        put_condition = (
            close_price < ema200 and         # Price BELOW EMA200
            st_dir == -1 and                 # Supertrend is Red
            stoch_k < stoch_d and stoch_k > 20 and  # Stochastic Bearish Crossover
            macd_val < macd_sig and          # MACD Bearish Crossover (Green Line below Signal)
            rsi_val <= 48                    # RSI Bearish Weakness
        )

        if call_condition:
            return {
                "direction": "CALL (BUY ⬆️)",
                "confidence": 100,
                "reasons": [
                    "✅ Trend: Price ABOVE EMA 200",
                    "✅ Supertrend: Bullish (Green)",
                    f"✅ Stochastic (5,3,3): Bullish Cross (%K={stoch_k:.1f})",
                    "✅ MACD: Green Line ABOVE Signal Line",
                    f"✅ RSI (14): Bullish Momentum ({rsi_val:.1f})"
                ],
                "price": round(close_price, 5)
            }
        elif put_condition:
            return {
                "direction": "PUT (SELL ⬇️)",
                "confidence": 100,
                "reasons": [
                    "🔻 Trend: Price BELOW EMA 200",
                    "🔻 Supertrend: Bearish (Red)",
                    f"🔻 Stochastic (5,3,3): Bearish Cross (%K={stoch_k:.1f})",
                    "🔻 MACD: Green Line BELOW Signal Line",
                    f"🔻 RSI (14): Bearish Weakness ({rsi_val:.1f})"
                ],
                "price": round(close_price, 5)
            }
        else:
            return {"direction": "NO TRADE", "confidence": 0, "reasons": ["Indicators not in 100% alignment"]}

    except Exception as e:
        logging.error(f"Error evaluating {pair_symbol}: {e}")
        return {"direction": "NO TRADE", "confidence": 0, "reasons": [f"Error: {e}"]}

def scan_all_pairs():
    """Scans tracked pairs and gives EXACT 2-MINUTE PREPARATION ADVANCE NOTICE."""
    best_signal = None
    best_asset = None

    for asset_name, symbol in PAIRS.items():
        sig = generate_signal(symbol)
        if sig["direction"] != "NO TRADE":
            best_signal = sig
            best_asset = asset_name
            break  # Pick first 100% flawless setup

    if best_signal is None:
        PERFORMANCE_STATS["no_trades"] += 1
        return None

    now_local = datetime.now(LOCAL_TZ)
    
    # EXACT 2-MINUTE ADVANCE PREPARATION TIME
    entry_local = (now_local + timedelta(minutes=2)).replace(second=0, microsecond=0)
    expiry_local = entry_local + timedelta(minutes=5)
    
    alert_time_str = now_local.strftime("%I:%M:%S %p")
    entry_time_str = entry_local.strftime("%I:%M:00 %p")
    expiry_time_str = expiry_local.strftime("%I:%M:00 %p")

    PERFORMANCE_STATS["total"] += 1
    reasons_formatted = "\n".join(best_signal.get("reasons", []))
    
    msg = (
        "⚡ *QUAD-LOCK HIGH-ACCURACY SIGNAL*\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"📍 Asset: *{best_asset}*\n"
        f"🎯 Action: *{best_signal['direction']}*\n"
        f"📩 Alert Sent: `{alert_time_str}`\n"
        f"🚀 *EXACT ENTRY TIME:* `{entry_time_str}` (2 Mins Notice)\n"
        f"⏱ Expiry Window: `{expiry_time_str}` (5 Mins Expiry)\n"
        f"🔥 Alignment: `100% Unanimous Agreement`\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"*Indicator Breakdown:*\n{reasons_formatted}\n\n"
        "⚠️ *Preparation:* Open Pocket Option now, select the pair, set 5-min trade timer, and tap BUY/SELL at EXACT ENTRY TIME!"
    )
    return msg

# ==========================================
# BACKGROUND SCAN LOOP
# ==========================================

async def auto_signal_loop(app):
    while True:
        await asyncio.sleep(40)  # Continuous background scan
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
        "⚡ *WELCOME TO BONNEY QUAD-LOCK SIGNAL BOT*\n\n"
        "🟢 *Strict Mode Active:* Requires 100% alignment across EMA 200 + Supertrend + Stoch + MACD + RSI.\n\n"
        "⏳ *Prep Time:* Gives 2 minutes advance notice before entry.\n\n"
        "*Commands:*\n"
        "• `/start_alerts` - Turn ON alerts\n"
        "• `/stop_alerts` - Turn OFF alerts\n"
        "• `/signals` - Force immediate scan\n"
        "• `/status` - Check bot status"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def start_alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.add(chat_id)
    await update.message.reply_text("🟢 *Quad-Lock Alerts Activated!*", parse_mode="Markdown")

async def stop_alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.discard(chat_id)
    await update.message.reply_text("🔴 *Alerts Paused.* Use `/start_alerts` to resume.", parse_mode="Markdown")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now_local = datetime.now(LOCAL_TZ).strftime("%I:%M:%S %p")
    msg = (
        "🟢 *SYSTEM STATUS: ONLINE*\n"
        "• Strategy: EMA 200 + Supertrend (10,3) + Stoch (5,3,3) + MACD (12,26,9) + RSI (14)\n"
        "• Timeframe: 5-Minute Candle Analysis\n"
        "• Prep Window: 2 Minutes Advance Notice\n"
        "• Requirement: 100% Unanimity\n"
        "• Local Time: " + now_local
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def signals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Scanning 5M candles for 100% indicator unanimity...*", parse_mode="Markdown")
    msg = scan_all_pairs()
    if msg is None:
        await update.message.reply_text("🚫 *NO TRADE:* Market is choppy or indicators do not 100% agree.", parse_mode="Markdown")
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

    print("⚡ BONNEY Quad-Lock Bot is online...")
    app.run_polling()

if __name__ == "__main__":
    main()
                           
