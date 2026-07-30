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

# Expanded High-Volume Currency Pairs List
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
    "AUD/JPY": "AUDJPY=X"
}

PERFORMANCE_STATS = {"total": 0, "wins": 0, "losses": 0, "no_trades": 0}
ACTIVE_SUBSCRIBERS = set()

# ==========================================
# TECHNICAL ANALYSIS ENGINE (Vol/Volatility Weighted)
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
    volume = df['Volume'].squeeze() if 'Volume' in df.columns else pd.Series(1, index=df.index)

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
    
    # 6. Volatility / Volume Metric (Relative ATR)
    df['Volatility_Score'] = (df['ATR'] / close) * 100
    
    return df

def generate_signal(pair_name, pair_symbol):
    try:
        data = yf.download(tickers=pair_symbol, period="5d", interval="5m", progress=False)
        if len(data) < 200:
            return {"direction": "NO TRADE", "volatility": 0}
        
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
        vol_score = float(latest['Volatility_Score'])

        # QUAD-LOCK STRICT UNANIMITY CONDITIONS
        call_condition = (
            close_price > ema200 and         # Price ABOVE EMA200
            st_dir == 1 and                  # Supertrend is Green
            stoch_k > stoch_d and stoch_k < 80 and  # Stochastic Bullish Crossover
            macd_val > macd_sig and          # MACD Bullish Crossover
            rsi_val >= 52                    # RSI Momentum
        )

        put_condition = (
            close_price < ema200 and         # Price BELOW EMA200
            st_dir == -1 and                 # Supertrend is Red
            stoch_k < stoch_d and stoch_k > 20 and  # Stochastic Bearish Crossover
            macd_val < macd_sig and          # MACD Bearish Crossover
            rsi_val <= 48                    # RSI Bearish Weakness
        )

        if call_condition:
            return {
                "asset": pair_name,
                "direction": "CALL (BUY ⬆️)",
                "volatility": vol_score,
                "reasons": [
                    "✅ Trend: Price ABOVE EMA 200",
                    "✅ Supertrend: Bullish (Green)",
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
                    "🔻 Trend: Price BELOW EMA 200",
                    "🔻 Supertrend: Bearish (Red)",
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

def scan_all_pairs():
    """Scans all currency pairs and ranks by highest active volume and volatility."""
    candidate_signals = []

    for asset_name, symbol in PAIRS.items():
        sig = generate_signal(asset_name, symbol)
        if sig["direction"] != "NO TRADE":
            candidate_signals.append(sig)

    if not candidate_signals:
        PERFORMANCE_STATS["no_trades"] += 1
        return None

    # Sort candidates so the pair with the HIGHEST active volatility/volume gets selected
    candidate_signals.sort(key=lambda x: x["volatility"], reverse=True)
    best_signal = candidate_signals[0]

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
        "⚡ *QUAD-LOCK SIGNAL — HIGH-VOLUME PAIR*\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"📍 Asset: *{best_signal['asset']}*\n"
        f"🎯 Action: *{best_signal['direction']}*\n"
        f"📩 Alert Sent: `{alert_time_str}`\n"
        f"🚀 *EXACT ENTRY TIME:* `{entry_time_str}` (2 Mins Notice)\n"
        f"⏱ Expiry Window: `{expiry_time_str}` (5 Mins Expiry)\n"
        f"🔥 Dynamic Weight: `High Market Volatility`\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"*Indicator Breakdown:*\n{reasons_formatted}\n\n"
        "⚠️ *Preparation:* Open Pocket Option, select asset, set 5-min trade timer, and execute at EXACT ENTRY TIME!"
    )
    return msg

# ==========================================
# BACKGROUND SCAN LOOP
# ==========================================

async def auto_signal_loop(app):
    while True:
        await asyncio.sleep(40)  # Scan every 40 seconds
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
        "⚡ *WELCOME TO BONNEY MULTI-CURRENCY SIGNAL BOT*\n\n"
        "🟢 *Currencies Active:* EUR/CAD, EUR/JPY, CAD/JPY, GBP/JPY, GBP/USD, EUR/USD, GBP/CAD, USD/JPY, AUD/USD, USD/CAD, USD/CHF, NZD/USD, AUD/JPY.\n\n"
        "📊 *Dynamic Selection:* Automatically prioritizes the most active, high-volume market pairs.\n\n"
        "⏳ *Prep Window:* 2 minutes advance notice.\n\n"
        "*Commands:*\n"
        "• `/start_alerts` - Turn ON alerts\n"
        "• `/stop_alerts` - Turn OFF alerts\n"
        "• `/signals` - Force immediate market scan\n"
        "• `/status` - Check bot status"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def start_alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.add(chat_id)
    await update.message.reply_text("🟢 *Multi-Currency Alerts Activated!*", parse_mode="Markdown")

async def stop_alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ACTIVE_SUBSCRIBERS.discard(chat_id)
    await update.message.reply_text("🔴 *Alerts Paused.* Use `/start_alerts` to resume.", parse_mode="Markdown")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now_local = datetime.now(LOCAL_TZ).strftime("%I:%M:%S %p")
    msg = (
        "🟢 *SYSTEM STATUS: ONLINE*\n"
        "• Tracked Pairs: 13 Major & Cross Forex Pairs\n"
        "• Filter: High Volatility & Relative Volume First\n"
        "• Strategy: EMA 200 + Supertrend + Stoch (5,3,3) + MACD + RSI\n"
        "• Timeframe: 5-Minute Candle Analysis\n"
        "• Prep Window: 2 Minutes Advance Notice\n"
        "• Local Time: " + now_local
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def pairs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs_list = "\n".join([f"• `{p}`" for p in PAIRS.keys()])
    await update.message.reply_text(f"📊 *ACTIVE TRACKED PAIRS:*\n\n{pairs_list}", parse_mode="Markdown")

async def signals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Scanning high-volume currency pairs...*", parse_mode="Markdown")
    msg = scan_all_pairs()
    if msg is None:
        await update.message.reply_text("🚫 *NO TRADE:* Markets are quiet or indicators do not 100% agree.", parse_mode="Markdown")
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
    app.add_handler(CommandHandler("pairs", pairs_cmd))

    print("⚡ BONNEY Multi-Currency Bot is online...")
    app.run_polling()

if __name__ == "__main__":
    main()
          
