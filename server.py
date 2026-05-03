"""
Blink Pro — Price Alert Server (Stable Version)
- Checks Yahoo Finance every 60s
- Sends Telegram alerts
- Allows adding alerts via Website (API) OR Telegram chat
"""

import os, time, json, logging, requests
from datetime import datetime
from flask import Flask, request, jsonify, make_response
from threading import Thread, Lock

# ── CONFIG ────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_EVERY = 60
RENDER_URL  = os.environ.get("RENDER_EXTERNAL_URL", "")
ALERTS_FILE = "alerts.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app         = Flask(__name__)
alerts      = []
alerts_lock = Lock()

# ── PERSIST ALERTS ────────────────────────────────────
def load_alerts():
    global alerts
    try:
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    alerts = data
                    log.info(f"✅ Loaded {len(alerts)} alerts from file")
    except Exception as e:
        log.error(f"Failed to load alerts: {e}")

def save_alerts():
    try:
        with alerts_lock:
            with open(ALERTS_FILE, "w") as f:
                json.dump(alerts, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Failed to save alerts: {e}")

# ── CORS ──────────────────────────────────────────────
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        r = make_response("", 204)
        r.headers.update({
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type"
        })
        return r

# ── TELEGRAM API ──────────────────────────────────────
def tg(method, payload):
    if not BOT_TOKEN: return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload, timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"Telegram {method} failed: {e}")
        return None

def send_telegram(chat_id, text):
    tg("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})

def register_webhook():
    if not BOT_TOKEN or not RENDER_URL: return
    webhook_url = RENDER_URL.rstrip("/") + "/webhook"
    tg("setWebhook", {"url": webhook_url, "drop_pending_updates": True})
    log.info(f"✅ Webhook set to: {webhook_url}")

# ── YAHOO FINANCE ─────────────────────────────────────
def fetch_prices(tickers):
    if not tickers: return {}
    symbols = ",".join(set(tickers))
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        quotes = r.json().get("quoteResponse", {}).get("result", [])
        return {q["symbol"]: round(q.get("regularMarketPrice", 0), 4) for q in quotes}
    except Exception as e:
        log.error(f"Yahoo fetch failed: {e}")
        return {}

# ── ALERT LOOP (RUNS 24/7) ────────────────────────────
def alert_loop():
    log.info("Alert loop started")
    while True:
        try:
            with alerts_lock:
                active = [a for a in alerts if not a["triggered"]]
            if active:
                tickers = list({a["ticker"] for a in active})
                prices  = fetch_prices(tickers)
                changed = False
                for a in active:
                    price = prices.get(a["ticker"])
                    if price is None: continue
                    
                    hit = (a["direction"] == "above" and price >= a["targetPrice"]) or \
                          (a["direction"] == "below" and price <= a["targetPrice"])
                    
                    if hit:
                        a["triggered"] = True
                        changed = True
                        dir_txt = "עלתה מעל" if a["direction"] == "above" else "ירדה מתחת"
                        msg = (f"🔔 <b>התראה — {a['ticker']}</b>\n\n"
                               f"{a['ticker']} {dir_txt} <b>${a['targetPrice']:.2f}</b>\n"
                               f"💰 מחיר נוכחי: <b>${price:.2f}</b>")
                        send_telegram(a.get("chatId", CHAT_ID), msg)
                if changed: save_alerts()
        except Exception as e:
            log.error(f"Loop error: {e}")
        time.sleep(CHECK_EVERY)

# ── ROUTES ────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "online", "active_alerts": len([a for a in alerts if not a["triggered"]])})

@app.route("/alerts", methods=["GET", "POST"])
def manage_alerts():
    if request.method == "POST":
        data = request.get_json(force=True)
        return add_new_alert(data)
    return jsonify(alerts)

def add_new_alert(data):
    ticker = str(data.get("ticker", "")).upper().strip()
    try:
        target = float(data.get("targetPrice", 0))
    except: return jsonify({"error": "Invalid price"}), 400
    
    alert = {
        "id": int(time.time() * 1000),
        "ticker": ticker,
        "targetPrice": target,
        "direction": data.get("direction", "above"),
        "triggered": False,
        "chatId": data.get("chatId", CHAT_ID),
        "addedAt": datetime.now().isoformat()
    }
    with alerts_lock:
        alerts.append(alert)
    save_alerts()
    return jsonify(alert), 201

# ── TELEGRAM WEBHOOK (HANDLES COMMANDS & TEXT) ────────
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True) or {}
    msg = data.get("message", {})
    text = msg.get("text", "").strip()
    cid = str(msg.get("chat", {}).get("id", ""))
    
    if not text or not cid: return "ok"

    # פקודות רגילות
    if text.startswith("/start"):
        send_telegram(cid, f"👋 שלום! אני בוט ההתראות שלך.\nה-Chat ID שלך הוא: <code>{cid}</code>")
    elif text.startswith("/status"):
        active = [a for a in alerts if not a["triggered"]]
        msg_text = "📊 התראות פעילות:\n" + "\n".join([f"• {a['ticker']} {a['direction']} {a['targetPrice']}" for a in active])
        send_telegram(cid, msg_text if active else "אין התראות פעילות.")
    
    # הוספת התראה ישירות בטקסט (למשל: AAPL above 150)
    elif any(word in text.lower() for word in ["above", "below"]):
        try:
            parts = text.split()
            ticker = parts[0].upper()
            direction = parts[1].lower()
            price = float(parts[2])
            add_new_alert({"ticker": ticker, "direction": direction, "targetPrice": price, "chatId": cid})
            send_telegram(cid, f"✅ הוספתי התראה: {ticker} כשיעלה מעל {price}" if direction=="above" else f"✅ הוספתי התראה: {ticker} כשירד מתחת {price}")
        except:
            send_telegram(cid, "❌ טעות בפורמט. נסה: AAPL above 200")
            
    return "ok"

# ── STARTUP ───────────────────────────────────────────
if __name__ == "__main__":
    load_alerts()
    Thread(target=alert_loop, daemon=True).start()
    
    # הגדרת ה-Webhook בטלגרם לאחר עליה
    def init_webhook():
        time.sleep(5)
        register_webhook()
    Thread(target=init_webhook, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
