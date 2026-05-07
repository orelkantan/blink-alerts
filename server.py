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
                    log.info(f"✅ Loaded {len(alerts)} alerts")
    except Exception as e:
        log.error(f"Failed to load alerts: {e}")

def save_alerts():
    try:
        with alerts_lock:
            with open(ALERTS_FILE, "w") as f:
                json.dump(alerts, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Failed to save alerts: {e}")

# ── TELEGRAM API HELPERS ──────────────────────────────
def tg(method, payload):
    if not BOT_TOKEN: return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload, timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"Telegram {method} failed: {e}")
        return None

def send_telegram(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg("sendMessage", payload)

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

# ── ALERT LOOP (IMPROVED - POINT 5) ───────────────────
def alert_loop():
    log.info("Alert loop active")
    while True:
        try:
            with alerts_lock:
                # סינון התראות שטרם הופעלו
                active = [a for a in alerts if not a.get("triggered", False)]
            
            if active:
                tickers = list({a["ticker"] for a in active})
                prices  = fetch_prices(tickers)
                
                for a in active:
                    price = prices.get(a["ticker"])
                    if price is None: continue
                    
                    hit = (a["direction"] == "above" and price >= a["targetPrice"]) or \
                          (a["direction"] == "below" and price <= a["targetPrice"])
                    
                    if hit:
                        # שיפור סעיף 5: סימון כ-triggered לפני השליחה למניעת כפילויות
                        with alerts_lock:
                            a["triggered"] = True
                        
                        save_alerts()
                        
                        dir_txt = "עלתה מעל" if a["direction"] == "above" else "ירדה מתחת"
                        msg = (f"🔔 <b>התראה — {a['ticker']}</b>\n\n"
                               f"הנכס {dir_txt} <b>${a['targetPrice']:.2f}</b>\n"
                               f"💰 מחיר נוכחי: <b>${price:.2f}</b>")
                        
                        # שיפור סעיף 4: הוספת כפתור "הפעל מחדש"
                        markup = {
                            "inline_keyboard": [[
                                {"text": "🔄 הפעל מחדש", "callback_data": f"reactivate_{a['id']}"}
                            ]]
                        }
                        send_telegram(a.get("chatId", CHAT_ID), msg, reply_markup=markup)
                        
        except Exception as e:
            log.error(f"Loop error: {e}")
        time.sleep(CHECK_EVERY)

# ── WEBHOOK HANDLER (IMPROVED - POINTS 3 & 4) ─────────
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True) or {}
    
    # טיפול בלחיצה על כפתורי Inline (סעיף 4)
    if "callback_query" in data:
        cb = data["callback_query"]
        cb_data = cb.get("data", "")
        cid = cb["message"]["chat"]["id"]
        
        if cb_data.startswith("reactivate_"):
            alert_id = int(cb_data.split("_")[1])
            with alerts_lock:
                for a in alerts:
                    if a["id"] == alert_id:
                        a["triggered"] = False
                        save_alerts()
                        tg("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "✅ ההתראה הופעלה מחדש!"})
                        send_telegram(cid, f"🔄 ההתראה על {a['ticker']} הוחזרה לרשימה הפעילה.")
                        break
        return "ok"

    # טיפול בהודעות טקסט (סעיף 3)
    msg = data.get("message", {})
    text = msg.get("text", "").strip()
    cid = str(msg.get("chat", {}).get("id", ""))
    if not text: return "ok"

    # פקודת רשימה
    if text.startswith("/list"):
        with alerts_lock:
            active = [a for a in alerts if not a["triggered"]]
            if not active:
                send_telegram(cid, "אין התראות פעילות כרגע.")
            else:
                lines = [f"🆔 <code>{a['id']}</code> | <b>{a['ticker']}</b> {a['direction']} {a['targetPrice']}" for a in active]
                send_telegram(cid, "📊 <b>התראות פעילות:</b>\n\n" + "\n".join(lines) + "\n\nלמחיקה שלח: <code>/remove ID</code>")

    # פקודת מחיקה
    elif text.startswith("/remove"):
        try:
            target_id = int(text.split()[1])
            with alerts_lock:
                initial_len = len(alerts)
                alerts[:] = [a for a in alerts if a["id"] != target_id]
                if len(alerts) < initial_len:
                    save_alerts()
                    send_telegram(cid, f"✅ התראה {target_id} נמחקה.")
                else:
                    send_telegram(cid, "❌ לא נמצאה התראה עם ID כזה.")
        except:
            send_telegram(cid, "❌ שימוש שגוי. לדוגמה: <code>/remove 1715000000</code>")

    # הוספת התראה מהירה
    elif any(word in text.lower() for word in ["above", "below"]):
        try:
            parts = text.split()
            ticker, direction, price = parts[0].upper(), parts[1].lower(), float(parts[2])
            new_alert = {
                "id": int(time.time()),
                "ticker": ticker,
                "targetPrice": price,
                "direction": direction,
                "triggered": False,
                "chatId": cid,
                "addedAt": datetime.now().isoformat()
            }
            with alerts_lock:
                alerts.append(new_alert)
            save_alerts()
            send_telegram(cid, f"✅ הוספתי התראה ל-{ticker}")
        except:
            send_telegram(cid, "❌ פורמט לא תקין. נסה: <code>AAPL above 200</code>")

    return "ok"

# ── FLASK & STARTUP ───────────────────────────────────
@app.route("/")
def health(): return jsonify({"active": len([a for a in alerts if not a["triggered"]])})

if __name__ == "__main__":
    load_alerts()
    Thread(target=alert_loop, daemon=True).start()
    
    # רישום Webhook (מושהה מעט כדי לוודא שהשרת עלה)
    def init_webhook():
        time.sleep(5)
        if RENDER_URL and BOT_TOKEN:
            webhook_url = RENDER_URL.rstrip("/") + "/webhook"
            tg("setWebhook", {"url": webhook_url, "drop_pending_updates": True})
    Thread(target=init_webhook, daemon=True).start()

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
