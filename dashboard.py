"""
Live Dashboard Server
=====================
Run alongside bot.py to get a live auto-updating portfolio dashboard.

Usage:
  python3 dashboard.py

Then open: http://localhost:5000
Auto-refreshes every 10 seconds.
"""

from flask import Flask, jsonify, send_from_directory
import json, os, time, hmac, hashlib
import requests as req

app = Flask(__name__)

LOG_FILE      = os.getenv("LOG_FILE", "logs/bot_trades.jsonl")
INITIAL_VALUE = float(os.getenv("INITIAL_VALUE", "50000"))
API_KEY       = os.getenv("ROOSTOO_API_KEY", "")
SECRET_KEY    = os.getenv("ROOSTOO_SECRET_KEY", "")
BASE_URL      = os.getenv("ROOSTOO_BASE_URL", "https://mock-api.roostoo.com")


def _sign_get(path):
    ts     = str(int(time.time() * 1000))
    params = f"timestamp={ts}"
    sig    = hmac.new(SECRET_KEY.encode(), params.encode(), hashlib.sha256).hexdigest()
    headers = {"RST-API-KEY": API_KEY, "MSG-SIGNATURE": sig}
    return f"{BASE_URL}{path}?{params}", headers


@app.route("/")
def index():
    return send_from_directory(".", "dashboard.html")


@app.route("/api/trades")
def trades():
    if not os.path.exists(LOG_FILE):
        return jsonify({"trades": [], "initial": INITIAL_VALUE})
    rows = []
    with open(LOG_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return jsonify({"trades": rows, "initial": INITIAL_VALUE})


@app.route("/api/ticker")
def ticker():
    try:
        ts = str(int(time.time() * 1000))
        r  = req.get(f"{BASE_URL}/v3/ticker?timestamp={ts}", timeout=5)
        return jsonify(r.json())
    except Exception:
        return jsonify({"Success": False})


@app.route("/api/balance")
def balance():
    try:
        url, headers = _sign_get("/v3/balance")
        r = req.get(url, headers=headers, timeout=5)
        return jsonify(r.json())
    except Exception:
        return jsonify({"Success": False})


if __name__ == "__main__":
    print("Dashboard running at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
