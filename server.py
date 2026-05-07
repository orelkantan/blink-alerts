import os, time, json, logging, requests
from datetime import datetime
from flask import Flask, request, jsonify
from threading import Thread, Lock

# ── CONFIG ────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
RENDER_URL  = os.environ.get("RENDER_EXTERNAL_URL", "")
ALERTS_FILE = "alerts.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app         = Flask(__name__)
alerts      = []
alerts_lock = Lock()
user_states = {}  # שומר את השלב שבו נמצא המשתמש בתהליך ההוספה

# ── PERSIST ALERTS ────────────────────────────────────
def load_alerts():
    global alerts
    try:
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, list): alerts = data
    except Exception as e: log.error(f"Load failed: {e}")

def save_alerts():
    with alerts_lock:
        with open(ALERTS_FILE, "w") as f:
            json.dump(alerts, f, ensure_ascii=False, indent=2)

# ── TELEGRAM API ──────────────────────────────────────
def tg(method, payload):
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload, timeout=10)
        return r.json()
    except Exception as e: return None

def send_telegram(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = reply_markup
    tg("sendMessage", payload)

# ── YAHOO FINANCE ─────────────────────────────────────
def fetch_prices(tickers):
    if not tickers: return {}
    symbols = ",".join(set(tickers))
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols}"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        quotes = r.json().get("quoteResponse", {}).get("result", [])
        return {q["symbol"]: round(q.get("regularMarketPrice", 0), 4) for q in quotes}
    except: return {}

# ── ALERT LOOP ────────────────────────────────────────
def alert_loop():
    while True:
        try:
            with alerts_lock:
                active = [a for a in alerts if not a.get("triggered")]
            if active:
                prices = fetch_prices([a["ticker"] for a in active])
                for a in active:
                    price = prices.get(a["ticker"])
                    if price is None: continue
                    hit = (a["direction"] == "above" and price >= a["targetPrice"]) or \
                          (a["direction"] == "below" and price <= a["targetPrice"])
                    if hit:
                        a["triggered"] = True
                        save_alerts()
                        dir_txt = "🚀 פריצה מעל" if a["direction"] == "above" else "📉 ירידה מתחת"
                        msg = f"🔔 <b>התראה עבור {a['ticker']}</b>\n\n{dir_txt} <b>${a['targetPrice']}</b>\nמחיר נוכחי: <b>${price}</b>"
                        markup = {"inline_keyboard": [[{"text": "🔄 הפעל מחדש", "callback_data": f"reactivate_{a['id']}"}]]}
                        send_telegram(a["chatId"], msg, markup)
        except Exception as e: log.error(f"Loop error: {e}")
        time.sleep(60)

# ── WEBHOOK (THE INTERVIEW LOGIC) ─────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True) or {}
    
    # כפתורי אינליין (לסעיף 3 של הראיון ולחידוש התראות)
    if "callback_query" in data:
        cb = data["callback_query"]
        cid = cb["message"]["chat"]["id"]
        cb_data = cb["data"]

        if cb_data.startswith("reactivate_"):
            aid = int(cb_data.split("_")[1])
            for a in alerts:
                if a["id"] == aid:
                    a["triggered"] = False
                    save_alerts()
                    send_telegram(cid, f"✅ התראה על {a['ticker']} חזרה לפעולה.")
        
        elif cb_data.startswith("dir_"):
            direction = "above" if "break" in cb_data else "below"
            state = user_states.get(cid)
            if state and state['step'] == 'direction':
                new_alert = {
                    "id": int(time.time()),
                    "ticker": state['ticker'],
                    "targetPrice": state['price'],
                    "direction": direction,
                    "triggered": False,
                    "chatId": cid
                }
                alerts.append(new_alert)
                save_alerts()
                txt = "פריצה (מעל)" if direction == "above" else "התנגדות (מתחת)"
                send_telegram(cid, f"✅ <b>התראה נשמרה!</b>\nמנייה: {state['ticker']}\nמחיר יעד: {state['price']}\nסוג: {txt}")
                user_states.pop(cid, None) # סיום הראיון
        
        tg("answerCallbackQuery", {"callback_query_id": cb["id"]})
        return "ok"

    msg = data.get("message", {})
    text = msg.get("text", "").strip()
    cid = msg.get("chat", {}).get("id")
    if not text or not cid: return "ok"

    # פקודות ניהול
    if text == "/list":
        active = [a for a in alerts if not a["triggered"]]
        if not active: send_telegram(cid, "אין התראות פעילות.")
        else:
            lines = [f"<code>{a['id']}</code> | {a['ticker']} ({a['direction']}) {a['targetPrice']}" for a in active]
            send_telegram(cid, "📊 <b>רשימת התראות:</b>\n" + "\n".join(lines))
        return "ok"

    # תהליך השאלות (The Interview)
    state = user_states.get(cid)

    if not state or text == "/add":
        user_states[cid] = {'step': 'ticker'}
        send_telegram(cid, "📈 מה הטיקר של המנייה? (למשל: AAPL)")
    
    elif state['step'] == 'ticker':
        user_states[cid] = {'step': 'price', 'ticker': text.upper()}
        send_telegram(cid, f"💰 מה המחיר שתרצה להגדיר עבור {text.upper()}?")
    
    elif state['step'] == 'price':
        try:
            price = float(text)
            user_states[cid].update({'step': 'direction', 'price': price})
            markup = {
                "inline_keyboard": [[
                    {"text": "🚀 פריצה (מעל)", "callback_data": "dir_break"},
                    {"text": "📉 התנגדות (מתחת)", "callback_data": "dir_resist"}
                ]]
            }
            send_telegram(cid, "האם מדובר בפריצה או התנגדות?", markup)
        except:
            send_telegram(cid, "❌ נא להזין מספר תקין עבור המחיר.")

    return "ok"

if __name__ == "__main__":
    load_alerts()
    Thread(target=alert_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
