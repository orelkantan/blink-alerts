"""
Blink Pro — Price Alert Server
Checks Yahoo Finance every 60 seconds, sends Telegram messages to @orelk24
"""

import os, time, json, logging, requests
from datetime import datetime
from flask import Flask, request, jsonify
from threading import Thread

# ── CONFIG ────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")   # set in Render env vars
CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")     # set after /start with bot
CHECK_EVERY = 60   # seconds between price checks
LOG_LEVEL   = logging.INFO

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app   = Flask(__name__)

# ── IN-MEMORY ALERT STORE ─────────────────────────────────────────────
# Each alert: { id, ticker, targetPrice, direction:'above'|'below', triggered:False }
alerts: list[dict] = []

# ── TELEGRAM ──────────────────────────────────────────────────────────
def send_telegram(chat_id: str, text: str):
    if not BOT_TOKEN:
        log.warning("No BOT_TOKEN set — skipping Telegram send")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
        if not r.ok:
            log.error(f"Telegram error: {r.text}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")

# ── YAHOO FINANCE PRICE FETCH ─────────────────────────────────────────
def fetch_prices(tickers: list[str]) -> dict[str, float]:
    if not tickers:
        return {}
    symbols = ",".join(set(tickers))
    url = (f"https://query1.finance.yahoo.com/v7/finance/quote"
           f"?symbols={symbols}"
           f"&fields=regularMarketPrice,regularMarketChange,regularMarketChangePercent")
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; BlinkAlertBot/1.0)",
        "Accept": "application/json"
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        quotes = r.json().get("quoteResponse", {}).get("result", [])
        return {q["symbol"]: round(q.get("regularMarketPrice", 0), 4) for q in quotes}
    except Exception as e:
        log.error(f"Yahoo Finance fetch failed: {e}")
        return {}

# ── ALERT CHECK LOOP ──────────────────────────────────────────────────
def alert_loop():
    log.info("Alert loop started — checking every %ds", CHECK_EVERY)
    while True:
        try:
            active = [a for a in alerts if not a["triggered"]]
            if active:
                tickers = list({a["ticker"] for a in active})
                prices  = fetch_prices(tickers)
                log.info(f"Checked {len(tickers)} tickers: {prices}")
                for a in active:
                    price = prices.get(a["ticker"])
                    if price is None:
                        continue
                    hit = (a["direction"] == "above" and price >= a["targetPrice"]) or \
                          (a["direction"] == "below" and price <= a["targetPrice"])
                    if hit:
                        a["triggered"] = True
                        direction_emoji = "🟢⬆" if a["direction"] == "above" else "🔴⬇"
                        direction_txt   = "עלתה מעל" if a["direction"] == "above" else "ירדה מתחת"
                        msg = (
                            f"🔔 <b>התראת מחיר — {a['ticker']}</b>\n\n"
                            f"{direction_emoji} {a['ticker']} {direction_txt} "
                            f"<b>${a['targetPrice']:.2f}</b>\n"
                            f"💰 מחיר נוכחי: <b>${price:.2f}</b>\n"
                            f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
                        )
                        log.info(f"ALERT TRIGGERED: {a['ticker']} @ {price}")
                        chat = CHAT_ID or a.get("chatId", "")
                        if chat:
                            send_telegram(chat, msg)
                        else:
                            log.warning("No CHAT_ID — cannot send Telegram message")
        except Exception as e:
            log.error(f"Alert loop error: {e}")
        time.sleep(CHECK_EVERY)

# ── REST API ──────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "running",
        "alerts": len(alerts),
        "active": len([a for a in alerts if not a["triggered"]]),
        "time": datetime.now().isoformat()
    })

@app.route("/alerts", methods=["GET"])
def get_alerts():
    return jsonify(alerts)

@app.route("/alerts", methods=["POST"])
def add_alert():
    """
    Body: { ticker, targetPrice, direction, chatId? }
    """
    data = request.get_json(force=True) or {}
    ticker = str(data.get("ticker", "")).strip().upper()
    try:
        target = float(data.get("targetPrice", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "targetPrice must be a number"}), 400
    direction = data.get("direction", "above")
    chat_id   = data.get("chatId", CHAT_ID)

    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    if target <= 0:
        return jsonify({"error": "targetPrice must be > 0"}), 400
    if direction not in ("above", "below"):
        return jsonify({"error": "direction must be 'above' or 'below'"}), 400

    alert = {
        "id":          int(time.time() * 1000),
        "ticker":      ticker,
        "targetPrice": round(target, 4),
        "direction":   direction,
        "triggered":   False,
        "chatId":      chat_id,
        "addedAt":     datetime.now().isoformat()
    }
    alerts.append(alert)
    log.info(f"Alert added: {ticker} {direction} ${target}")

    # Confirm to user via Telegram
    dir_txt = "עלה מעל" if direction == "above" else "ירד מתחת"
    dir_emoji = "⬆️" if direction == "above" else "⬇️"
    if chat_id:
        send_telegram(chat_id,
            f"✅ <b>התראה נוספה!</b>\n\n"
            f"📈 מניה: <b>{ticker}</b>\n"
            f"{dir_emoji} יעד: <b>${target:.2f}</b> ({dir_txt})\n"
            f"⏱ בדיקה כל {CHECK_EVERY} שניות"
        )
    return jsonify(alert), 201

@app.route("/alerts/<int:alert_id>", methods=["DELETE"])
def delete_alert(alert_id):
    global alerts
    before = len(alerts)
    alerts = [a for a in alerts if a["id"] != alert_id]
    if len(alerts) < before:
        return jsonify({"deleted": True})
    return jsonify({"error": "not found"}), 404

@app.route("/alerts/reset", methods=["POST"])
def reset_triggered():
    """Reset triggered status so alerts can fire again"""
    for a in alerts:
        a["triggered"] = False
    return jsonify({"reset": len(alerts)})

@app.route("/price/<ticker>", methods=["GET"])
def get_price(ticker):
    prices = fetch_prices([ticker.upper()])
    p = prices.get(ticker.upper())
    if p is None:
        return jsonify({"error": "ticker not found"}), 404
    return jsonify({"ticker": ticker.upper(), "price": p})

# Telegram webhook — lets users interact via bot chat
@app.route(f"/webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True) or {}
    msg  = data.get("message", {})
    text = msg.get("text", "").strip()
    cid  = str(msg.get("chat", {}).get("id", ""))
    if not cid:
        return "ok"
    if text == "/start":
        send_telegram(cid,
            "👋 <b>ברוך הבא ל-Blink Pro Alert Bot!</b>\n\n"
            "אני אשלח לך התראה כשמניה מגיעה למחיר היעד שלך.\n\n"
            f"🆔 ה-Chat ID שלך: <code>{cid}</code>\n\n"
            "📌 העתק את ה-ID הזה והכנס אותו בהגדרות האפליקציה."
        )
    elif text == "/status":
        active = [a for a in alerts if not a["triggered"]]
        lines  = [f"• {a['ticker']} {'מעל' if a['direction']=='above' else 'מתחת'} ${a['targetPrice']:.2f}" for a in active]
        body   = "\n".join(lines) if lines else "אין התראות פעילות"
        send_telegram(cid, f"📊 <b>התראות פעילות ({len(active)}):</b>\n{body}")
    elif text == "/help":
        send_telegram(cid,
            "📖 <b>פקודות זמינות:</b>\n\n"
            "/start — קבל את ה-Chat ID שלך\n"
            "/status — רשימת התראות פעילות\n"
            "/help — עזרה"
        )
    return "ok"

# ── MAIN ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    Thread(target=alert_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    log.info(f"Server starting on port {port}")
    app.run(host="0.0.0.0", port=port)
