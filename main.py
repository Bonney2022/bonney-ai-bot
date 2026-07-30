import os
import time
import logging
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import numpy as np
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from pocketoptionapi_async import AsyncPocketOptionClient, OrderDirection

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Global Settings
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
LOCAL_TZ = ZoneInfo("America/Toronto")  # Eastern Time

# Direct Pocket Option Session ID
SSID = '42["auth",{"sessionToken":"5a7323ef8734bd71e48067c789845cf2","uid":"93728316","lang":"en","currentUrl":"cabinet","isChart":1}]'

# Pocket Option OTC & Live Asset Symbols
PAIRS = {
    "EUR/USD OTC": "EURUSD_otc",
    "GBP/USD OTC": "GBPUSD_otc",
    "EUR/JPY OTC": "EURJPY_otc",
    "GBP/JPY OTC": "GBPJPY_otc",
    "AUD/CAD OTC": "AUDCAD_otc",
    "USD/CAD OTC": "USDCAD_otc",
    "USD/JPY OTC": "USDJPY_otc",
    "NZD/USD OTC": "NZDUSD_otc",
    "EUR/CAD OTC": "EURCAD_otc"
}

ACTIVE_SUBSCRIBERS = set()
po_client = None

# ==========================================
# POCKET OPTION CLIENT MANAGEMENT
# ==========================================

async def get_po_client():
    global po_client
    if po_client is None:
        po_client = AsyncPocketOptionClient(ssid=SSID, is_demo=True, enable_logging=False)
        await po_client.connect()
    return po_client

# ==========================================
# TECHNICAL ANALYSIS ENGINE
# ==========================================

def calculate_supertrend(df, period=10, multiplier=3):
    high = df['high'].squeeze()
    low = df['low'].squeeze()
    close = df['close'].squeeze()
    
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
    close = df['close'].squeeze()
    low = df['low'].squeeze()
    high = df['high'].squeeze()

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
    
    # 5. Supertrend (10, 3) + ATR
    df = calculate_supertrend(df, period=10, multiplier=3)
    df['Volatility_Score'] = (df['ATR'] / close) * 100
    
    return df

async def generate_signal(pair_name, pair_symbol):
    try:
        client = await get_po_client()
        # Fetch 300-second (5-minute) candle dataframe directly from Pocket Option
        df = await client.get_candles_dataframe(pair_symbol, 300)
        
        if df is None or len(df) < 200:
            return {"direction": "NO TRADE", "volatility": 0}

        df = calculate_indicators(df.copy())
        latest = df.iloc[-1]

        close_price = float(latest['close'])
        ema200 = float(latest['EMA200'])
        st_dir = int(latest['Supertrend_Dir'])
        stoch_k = float(latest['Stoch_K'])
        stoch_d = float(latest['Stoch_D'])
        rsi_val = float(latest['RSI'])
        macd_val = float(latest['MACD'])
        macd_sig = float(latest['MACD_Signal'])
        vol_score = float(latest['Volatility_Score'])

        call_condition = (
            close_price > ema200 and 
            st_dir == 1 and 
            stoch_k > stoch_d and stoch_k < 80 and 
            macd_val > macd_sig and 
            rsi_val >= 50
        )

        put_condition = (
            close_price < ema200 and 
            st_dir == -1 and 
            stoch_k < stoch_d and stoch_k > 20 and 
            macd_val < macd_sig and 
            rsi_val <= 50
        )

        if call_condition:
            return {
                "asset": pair_name,
                "direction": "CALL (BUY ⬆️)",
                "volatility": vol_score,
                "reasons": [
                    "✅ Price ABOVE EMA 200",
                    "✅ Supertrend: Green",
                    f"✅ Stochastic (5,3,3): Bullish Cross (%K={stoch_k:.1f})",
                    "✅ MACD: Bullish Crossover",
                    f"✅ RSI (14): Bullish Momentum ({rsi_val:.1f})"
                ],
                "price": round(close_price, 5)
            }
        elif put_condition:
            return {
                "asset": pair_name,
                "direction": "PUT (SELL ⬇️)",
                "volatility": vol_score,
                "reasons": [
                    "🔻 Price BELOW EMA 200",
                    "🔻 Supertrend: Red",
                    f"🔻 Stochastic (5,3,3): Bearish Cross (%K={stoch_k:.1f})",
                    "🔻 MACD: Bearish Crossover",
                    f"🔻 RSI (14): Bearish Weakness ({rsi_val:.1f})"
                ],
                "price": round(close_price, 5)
            }
        else:
            return {"direction": "NO TRADE", "volatility": 0}

    except Exception as e:
        logging.error(f"Error evaluating {pair_symbol}: {e}")
        return {"direction": "NO TRADE", "volatility": 0}

async def scan_all_pairs():
    candidate_signals = []

    for asset_name, symbol in PAIRS.items():
        sig = await generate_signal(asset_name, symbol)
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
        "⚡ *DIRECT POCKET OPTION OTC SIGNAL*\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"📍 Asset: *{best_signal['asset']}*\n"
        f"🎯 Action: *{best_signal['direction']}*\n"
        f"📩 Alert Sent: `{alert_time_str}`\n"
        f"🚀 *EXACT ENTRY TIME:* `{entry_time_str}` (2 Mins Notice)\n"
        f"⏱ Expiry Window: `{expiry_time_str}` (5 Mins Expiry)\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"*Indicator Breakdown:*\n{reasons_formatted}\n\n"
        "⚠️ *Preparation:* Open Pocket Option, select asset, set 5-min trade timer (`00:05:00`), and execute at EXACT ENTRY TIME!"
    )
    return msg

# ==========================================
# BACKGROUND LOOP
# ==========================================

async def auto_signal_loop(app):
    while True:
        await asyncio.sleep(40)
        if ACTIVE_SUBSCRIBERS:
            signal_msg = await scan_all_pairs()
            if signal_msg is not None:
                for chat_id in list(ACTIVE_SUBSCRIBERS):
                    try:
                        await app.bot.send_message(chat_id=chat_id, text=signal_msg, parse_mode="Markdown")
                    except Exception as e:
                        logging.error(f"Failed to send alert to {chat_id}: {e}")

# ==========================================
# TELEGRAM COMMANDS
# ==========================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.add(chat_id)
    welcome_text = (
        "⚡ *POCKET OPTION DIRECT OTC SIGNAL BOT*\n\n"
        "🟢 *Live Stream Engine:* Connected directly to Pocket Option OTC servers via WebSocket.\n\n"
        "⏳ *Prep Window:* 2 minutes advance notice.\n\n"
        "*Commands:*\n"
        "• `/start_alerts` - Turn ON alerts\n"
        "• `/stop_alerts` - Turn OFF alerts\n"
        "• `/signals` - Force immediate OTC scan\n"
        "• `/balance` - Check connected Pocket Option balance"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        client = await get_po_client()
        bal = await client.get_balance()
        await update.message.reply_text(f"💰 *Demo Balance:* `${bal.balance:.2f} {bal.currency}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Connection error: {e}")

async def start_alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.add(chat_id)
    await update.message.reply_text("🟢 *OTC Signal Alerts Activated!*", parse_mode="Markdown")

async def stop_alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.discard(chat_id)
    await update.message.reply_text("🔴 *Alerts Paused.*", parse_mode="Markdown")

async def signals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Scanning live Pocket Option OTC feeds...*", parse_mode="Markdown")
    msg = await scan_all_pairs()
    if msg is None:
        await update.message.reply_text("🚫 *NO TRADE:* Indicators do not 100% agree across active OTC pairs.", parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")

# ==========================================
# APPLICATION LAUNCHER
# ==========================================

async def post_init(app):
    asyncio.create_task(auto_signal_loop(app))

def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: Please set your TELEGRAM_TOKEN environment variable or paste your token into TELEGRAM_BOT_TOKEN.")
        return

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("start_alerts", start_alerts_cmd))
    app.add_handler(CommandHandler("stop_alerts", stop_alerts_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("signals", signals_cmd))

    print("⚡ Direct Pocket Option Bot is online...")
    app.run_polling()

if __name__ == "__main__":
    main()
  
