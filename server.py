"""
Blink Pro — Price Alert Server
Checks Yahoo Finance every 60 seconds, sends Telegram alerts to @orelk24
Auto-registers Telegram webhook on startup.
"""

import os, time, json, logging, requests
from datetime import datetime
from flask import Flask, request, jsonify
from threading import Thread

# ── CONFIG ────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_EVERY = 60   # seconds
RENDER_URL  = os.environ.get("RENDER_EXTERNAL_URL", "")  # auto-set by Render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app    = Flask(__name__)
alerts = []   # in-memory alert store

# ── TELEGRAM HELPERS ──────────────────────────────────
def tg(method: str, payload: dict):
    if not BOT_TOKEN:
        return None
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            json=payload, timeout=10
        )
        return r.json()
    except Exception as e:
        log.error(f"Telegram {method} failed: {e}")
        return None

def send_telegram(chat_id: str, text: str):
    tg("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})

def register_webhook():
    """Register this server's URL as the Telegram webhook."""
    if not BOT_TOKEN or not RENDER_URL:
        log.warning("Cannot register webhook — BOT_TOKEN or RENDER_EXTERNAL_URL missing")
        return
    webhook_url = RENDER_URL.rstrip("/") + "/webhook"
    result = tg("setWebhook", {"url": webhook_url, "drop_pending_updates": True})
    if result and result.get("ok"):
        log.info(f"✅ Webhook registered: {webhook_url}")
    else:
        log.error(f"❌ Webhook registration failed: {result}")

def get_webhook_info():
    return tg("getWebhookInfo", {})

# ── YAHOO FINANCE ─────────────────────────────────────
def fetch_prices(tickers: list) -> dict:
    if not tickers:
        return {}
    symbols = ",".join(set(tickers))
    url = (
        f"https://query1.finance.yahoo.com/v7/finance/quote"
        f"?symbols={symbols}"
        f"&fields=regularMarketPrice,regularMarketChange"
    )
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BlinkAlertBot/1.0)"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        quotes = r.json().get("quoteResponse", {}).get("result", [])
        return {q["symbol"]: round(q.get("regularMarketPrice", 0), 4) for q in quotes}
    except Exception as e:
        log.error(f"Yahoo fetch failed: {e}")
        return {}

# ── ALERT CHECK LOOP ──────────────────────────────────
def alert_loop():
    log.info(f"Alert loop started — checking every {CHECK_EVERY}s")
    while True:
        try:
            active = [a for a in alerts if not a["triggered"]]
            if active:
                tickers = list({a["ticker"] for a in active})
                prices  = fetch_prices(tickers)
                log.info(f"Checked {tickers} → {prices}")
                for a in active:
                    price = prices.get(a["ticker"])
                    if price is None:
                        continue
                    hit = (
                        (a["direction"] == "above" and price >= a["targetPrice"]) or
                        (a["direction"] == "below" and price <= a["targetPrice"])
                    )
                    if hit:
                        a["triggered"] = True
                        dir_emoji = "🟢⬆️" if a["direction"] == "above" else "🔴⬇️"
                        dir_txt   = "עלתה מעל" if a["direction"] == "above" else "ירדה מתחת"
                        msg = (
                            f"🔔 <b>התראת מחיר — {a['ticker']}</b>\n\n"
                            f"{dir_emoji} {a['ticker']} {dir_txt} "
                            f"<b>${a['targetPrice']:.2f}</b>\n"
                            f"💰 מחיר נוכחי: <b>${price:.2f}</b>\n"
                            f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
                        )
                        log.info(f"🔔 ALERT FIRED: {a['ticker']} @ {price}")
                        chat = CHAT_ID or a.get("chatId", "")
                        if chat:
                            send_telegram(chat, msg)
        except Exception as e:
            log.error(f"Alert loop error: {e}")
        time.sleep(CHECK_EVERY)

# ── REST API ──────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "running",
        "alerts_total": len(alerts),
        "alerts_active": len([a for a in alerts if not a["triggered"]]),
        "webhook": get_webhook_info(),
        "time": datetime.now().isoformat()
    })

@app.route("/alerts", methods=["GET"])
def get_alerts():
    return jsonify(alerts)

@app.route("/alerts", methods=["POST"])
def add_alert():
    data      = request.get_json(force=True) or {}
    ticker    = str(data.get("ticker", "")).strip().upper()
    direction = data.get("direction", "above")
    chat_id   = data.get("chatId", CHAT_ID)
    try:
        target = float(data.get("targetPrice", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "targetPrice must be a number"}), 400

    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    if target <= 0:
        return jsonify({"error": "targetPrice must be > 0"}), 400
    if direction not in ("above", "below"):
        return jsonify({"error": "direction must be above or below"}), 400

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

    # Confirm via Telegram
    dir_txt   = "יעלה מעל" if direction == "above" else "ירד מתחת"
    dir_emoji = "⬆️" if direction == "above" else "⬇️"
    if chat_id:
        send_telegram(chat_id,
            f"✅ <b>התראה נוספה!</b>\n\n"
            f"📈 מניה: <b>{ticker}</b>\n"
            f"{dir_emoji} יעד: <b>${target:.2f}</b> ({dir_txt})\n"
            f"⏱ בדיקה כל {CHECK_EVERY} שניות\n\n"
            f"תשלח הודעה ברגע שהמחיר יגיע ליעד!"
        )
    return jsonify(alert), 201

@app.route("/alerts/<int:alert_id>", methods=["DELETE"])
def delete_alert(alert_id):
    global alerts
    before = len(alerts)
    alerts = [a for a in alerts if a["id"] != alert_id]
    return jsonify({"deleted": len(alerts) < before})

@app.route("/alerts/reset", methods=["POST"])
def reset_alerts():
    for a in alerts:
        a["triggered"] = False
    return jsonify({"reset": len(alerts)})

@app.route("/price/<ticker>", methods=["GET"])
def get_price(ticker):
    prices = fetch_prices([ticker.upper()])
    p = prices.get(ticker.upper())
    if p is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ticker": ticker.upper(), "price": p})

@app.route("/setup-webhook", methods=["GET"])
def setup_webhook():
    """Call this URL once to register the Telegram webhook manually."""
    register_webhook()
    info = get_webhook_info()
    return jsonify({"webhook_info": info})

# ── TELEGRAM WEBHOOK HANDLER ──────────────────────────
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True) or {}
    msg  = data.get("message", {})
    text = msg.get("text", "").strip()
    cid  = str(msg.get("chat", {}).get("id", ""))
    if not cid:
        return "ok"

    if text.startswith("/start"):
        send_telegram(cid,
            f"👋 <b>שלום! Blink Pro Alert Bot פעיל</b>\n\n"
            f"🔔 אני אשלח לך התראה כשמניה מגיעה למחיר היעד שלך.\n\n"
            f"🆔 ה-Chat ID שלך: <code>{cid}</code>\n\n"
            f"📊 התראות פעילות כרגע: <b>{len([a for a in alerts if not a['triggered']])}</b>\n\n"
            f"הקלד /status לרשימת ההתראות\nהקלד /help לעזרה"
        )
    elif text.startswith("/status"):
        active = [a for a in alerts if not a["triggered"]]
        if not active:
            send_telegram(cid, "📭 אין התראות פעילות כרגע\n\nהוסף התראה מהאפליקציה!")
        else:
            lines = []
            for a in active:
                d = "⬆️ מעל" if a["direction"] == "above" else "⬇️ מתחת"
                lines.append(f"• <b>{a['ticker']}</b> {d} ${a['targetPrice']:.2f}")
            send_telegram(cid,
                f"📊 <b>התראות פעילות ({len(active)}):</b>\n\n" +
                "\n".join(lines) +
                f"\n\n⏱ בדיקה כל {CHECK_EVERY} שניות"
            )
    elif text.startswith("/help"):
        send_telegram(cid,
            "📖 <b>פקודות זמינות:</b>\n\n"
            "/start — ברכות + Chat ID\n"
            "/status — רשימת התראות פעילות\n"
            "/help — עזרה\n\n"
            "💡 להוסיף התראה — פתח את האפליקציה ולך ל-🔔 התראות מחיר"
        )
    else:
        send_telegram(cid, "הקלד /help לרשימת הפקודות 😊")
    return "ok"

# ── STARTUP ───────────────────────────────────────────
def startup():
    Thread(target=alert_loop, daemon=True).start()
    # Register webhook with Telegram after short delay (let server start first)
    def delayed_webhook():
        time.sleep(5)
        register_webhook()
        # Send startup notification
        if CHAT_ID:
            send_telegram(CHAT_ID,
                "🟢 <b>Blink Pro Alert Bot הופעל!</b>\n\n"
                f"⏱ בודק מחירים כל {CHECK_EVERY} שניות\n"
                "הקלד /status לרשימת ההתראות"
            )
    Thread(target=delayed_webhook, daemon=True).start()

startup()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"Server starting on port {port}")
    app.run(host="0.0.0.0", port=port)
