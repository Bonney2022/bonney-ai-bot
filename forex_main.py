import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
import yfinance as yf
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Setup Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# Hardcoded Telegram Token
TELEGRAM_TOKEN = "8744703805:AAGFvp1mTQgojIf_HpP4P5okPi3uRWLyCng"

# Pairs Config
PAIRS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "JPY=X",
    "AUD/USD": "AUDUSD=X",
    "USD/CAD": "CAD=X",
    "GOLD (XAU/USD)": "GC=F"
}

ACTIVE_SUBSCRIBERS = set()
LOCAL_TZ = timezone(timedelta(hours=-4)) # EST timezone offset
LAST_ALERT_TIMES = {} # Tracks duplicate alerts per asset

# ==========================================
# INDICATOR CALCULATIONS & SIGNAL GENERATOR
# ==========================================

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_atr(df, period=14):
    high_low = df['High'] - df['Low']
    high_close = (df['High'] - df['Close'].shift()).abs()
    low_close = (df['Low'] - df['Close'].shift()).abs()
    tr = high_low.combine(high_close, max).combine(low_close, max)
    return tr.rolling(window=period).mean()

def generate_forex_signal(pair_name, pair_symbol):
    try:
        df = yf.download(pair_symbol, period="5d", interval="15m", progress=False)
        if df.empty or len(df) < 50:
            return None

        # Flatten multi-index columns if returned by yfinance
        if isinstance(df.columns, tuple) or getattr(df.columns, 'nlevels', 1) > 1:
            df.columns = df.columns.get_level_values(0)

        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
        df['RSI'] = calculate_rsi(df['Close'])
        df['ATR'] = calculate_atr(df)

        latest = df.iloc[-1]

        score_buy = 0
        score_sell = 0

        # Technical Scoring
        if latest['EMA20'] > latest['EMA50']:
            score_buy += 40
        else:
            score_sell += 40

        if latest['RSI'] > 50 and latest['RSI'] < 70:
            score_buy += 40
        elif latest['RSI'] < 50 and latest['RSI'] > 30:
            score_sell += 40

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
        sl_distance = atr_val * 1.5 if not os.isna(atr_val) else entry_price * 0.002
        
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
    now_time = datetime.now()

    for asset_name, symbol in PAIRS.items():
        # Prevent repeating the same signal within 15 minutes
        if asset_name in LAST_ALERT_TIMES and (now_time - LAST_ALERT_TIMES[asset_name]).total_seconds() < 900:
            continue

        sig = generate_forex_signal(asset_name, symbol)
        if sig is not None:
            if best_signal is None or sig["confidence"] > best_signal["confidence"]:
                best_signal = sig

    if best_signal is None:
        return None

    LAST_ALERT_TIMES[best_signal['asset']] = now_time

    now_local = datetime.now(LOCAL_TZ)
    entry_time_target = now_local + timedelta(minutes=5)

    alert_time_str = now_local.strftime("%I:%M:%S %p EST")
    entry_time_str = entry_time_target.strftime("%I:%M:00 %p EST")

    # Formatted using clean HTML to completely avoid Telegram parsing crashes
    msg = (
        "📊 <b>BONNEY FOREX SIGNAL — 5-MIN ADVANCE NOTICE</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"📍 Asset: <b>{best_signal['asset']}</b>\n"
        f"🎯 Order Type: <b>{best_signal['action']}</b>\n"
        f"📩 Signal Issued: <code>{alert_time_str}</code>\n"
        f"⏳ <b>Target Entry Time:</b> <code>{entry_time_str}</code>\n"
        f"🔥 Confidence: <code>{best_signal['confidence']}%</code>\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Entry Price:</b> <code>{best_signal['entry']}</code>\n"
        f"🛑 <b>Stop Loss (SL):</b> <code>{best_signal['sl']}</code>\n"
        f"🎯 <b>Take Profit 1 (TP1):</b> <code>{best_signal['tp1']}</code> (Safe Exit)\n"
        f"🎯 <b>Take Profit 2 (TP2):</b> <code>{best_signal['tp2']}</code> (Standard)\n"
        f"🎯 <b>Take Profit 3 (TP3):</b> <code>{best_signal['tp3']}</code> (Extended Run)\n"
        "━━━━━━━━━━━━━━━━━\n"
        "⚠️ <b>Execution:</b> Set your Buy/Sell Limit or Market Order 5 minutes after receiving this alert!"
    )
    return msg

# ==========================================
# BACKGROUND SCAN LOOP
# ==========================================

async def auto_signal_loop(app):
    while True:
        await asyncio.sleep(60) # Checks every 60 seconds
        if ACTIVE_SUBSCRIBERS:
            signal_msg = scan_forex_pairs()
            if signal_msg is not None:
                for chat_id in list(ACTIVE_SUBSCRIBERS):
                    try:
                        await app.bot.send_message(chat_id=chat_id, text=signal_msg, parse_mode="HTML")
                    except Exception as e:
                        logging.error(f"Failed to send alert to {chat_id}: {e}")

# ==========================================
# TELEGRAM BOT COMMAND HANDLERS
# ==========================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.add(chat_id)
    welcome_text = (
        "📊 <b>WELCOME TO BONNEY FOREX SIGNAL BOT</b>\n\n"
        "🟢 <b>5-Minute Advance Alerts ACTIVE:</b> Provides Entry, SL, TP1, TP2, and TP3.\n\n"
        "<b>Commands:</b>\n"
        "• /signals - Force immediate Forex market scan\n"
        "• /stop_alerts - Turn off alerts"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")

async def stop_alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.discard(chat_id)
    await update.message.reply_text("🔴 <b>Forex Alerts Disabled.</b>", parse_mode="HTML")

async def signals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 <b>Scanning Forex markets for Entry, SL, and TPs...</b>", parse_mode="HTML")
    msg = scan_forex_pairs()
    if msg is None:
        await update.message.reply_text("🚫 <b>NO FOREX TRADE:</b> No high-probability setup found right now.", parse_mode="HTML")
    else:
        await update.message.reply_text(msg, parse_mode="HTML")

# ==========================================
# LAUNCHER
# ==========================================

async def post_init(app):
    asyncio.create_task(auto_signal_loop(app))

def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("stop_alerts", stop_alerts_cmd))
    app.add_handler(CommandHandler("signals", signals_cmd))

    print(" BONNEY Forex Bot is online...")
    app.run_polling()

if __name__ == "__main__":
    main()
