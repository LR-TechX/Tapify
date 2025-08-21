#!/usr/bin/env python3
# app.py — Tapify Game Mini App for Render deployment
# Requirements:
#   pip install flask psycopg[binary] python-dotenv uvicorn
#
# Environment (.env):
#   BOT_TOKEN=your_bot_token
#   ADMIN_ID=your_admin_id
#   WEBAPP_URL=https://tapify.onrender.com/app
#   DATABASE_URL=postgres://user:pass@host:port/dbname
#   BANK_ACCOUNTS=FirstBank:1234567890,GTBank:0987654321
#   FOOTBALL_API_KEY=your_api_key_here
#
# Start:
#   python app.py

import os
import sys
import json
import hmac
import hashlib
import logging
from urllib.parse import parse_qsl
from datetime import datetime, timedelta, timezone, date
from collections import deque, defaultdict
import random
import time
import requests
from flask import Flask, request, jsonify, Response, redirect
from dotenv import load_dotenv
import threading
from telegram import Bot

# --- Config & Globals ---------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BANK_ACCOUNTS = os.getenv("BANK_ACCOUNTS", "").strip()
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "").strip()

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN is required in environment (.env).", file=sys.stderr)
    sys.exit(1)
if not ADMIN_ID:
    print("ERROR: ADMIN_ID is required in environment (.env).", file=sys.stderr)
    sys.exit(1)
if not DATABASE_URL:
    print("ERROR: DATABASE_URL is required in environment (.env).", file=sys.stderr)
    sys.exit(1)
if not BANK_ACCOUNTS:
    print("WARNING: BANK_ACCOUNTS not set; deposits may fail.", file=sys.stderr)
if not FOOTBALL_API_KEY:
    print("WARNING: FOOTBALL_API_KEY not set; predictions may fail.", file=sys.stderr)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("tapify")

# Initialize Telegram Bot for admin notifications
bot = Bot(token=BOT_TOKEN)

# --- Database Layer (PostgreSQL only) ------------------

import psycopg
from psycopg.rows import dict_row

try:
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    conn.autocommit = True
    cursor = conn.cursor()
    log.info("Postgres connected successfully")
except psycopg.Error as e:
    log.error(f"Database connection failed: {e}")
    raise

def db_execute(query: str, params: tuple = ()):
    try:
        cursor.execute(query, params)
        log.debug(f"DB execute: {query} with params {params}")
    except psycopg.Error as e:
        log.error(f"DB execute failed: {query} | Error: {e}")
        raise

def db_fetchone(query: str, params: tuple = ()):
    try:
        cursor.execute(query, params)
        result = cursor.fetchone()
        log.debug(f"DB fetchone: {query} with params {params} -> {result}")
        return result
    except psycopg.Error as e:
        log.error(f"DB fetchone failed: {query} | Error: {e}")
        raise

def db_fetchall(query: str, params: tuple = ()):
    try:
        cursor.execute(query, params)
        result = cursor.fetchall()
        log.debug(f"DB fetchall: {query} with params {params} -> {result}")
        return result
    except psycopg.Error as e:
        log.error(f"DB fetchall failed: {query} | Error: {e}")
        raise

def db_init():
    db_execute("""
    CREATE TABLE IF NOT EXISTS game_taps (
        id BIGSERIAL PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        ts TIMESTAMP NOT NULL,
        delta INT NOT NULL,
        nonce TEXT NOT NULL
    );
    """)
    db_execute("""
    CREATE TABLE IF NOT EXISTS game_referrals (
        referrer BIGINT NOT NULL,
        referee BIGINT NOT NULL,
        created_at TIMESTAMP NOT NULL,
        PRIMARY KEY (referrer, referee)
    );
    """)
    db_execute("""
    CREATE TABLE IF NOT EXISTS withdrawals (
        id BIGSERIAL PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        amount BIGINT NOT NULL,
        status TEXT DEFAULT 'pending',
        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    db_execute("""
    CREATE TABLE IF NOT EXISTS deposits (
        id BIGSERIAL PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        amount BIGINT NOT NULL,
        bank_account TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    # Add game-related columns to users table from main.py
    db_execute("""
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS coins BIGINT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS energy INT DEFAULT 500,
    ADD COLUMN IF NOT EXISTS max_energy INT DEFAULT 500,
    ADD COLUMN IF NOT EXISTS energy_updated_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS multitap_until TIMESTAMP,
    ADD COLUMN IF NOT EXISTS autotap_until TIMESTAMP,
    ADD COLUMN IF NOT EXISTS regen_rate_seconds INT DEFAULT 3,
    ADD COLUMN IF NOT EXISTS last_tap_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS daily_streak INT DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_streak_at DATE,
    ADD COLUMN IF NOT EXISTS last_daily_reward DATE
    """)

def db_now() -> datetime:
    return datetime.now(timezone.utc)

def db_date_utc() -> date:
    return db_now().date()

def is_registered(chat_id: int) -> bool:
    try:
        row = db_fetchone("SELECT payment_status FROM users WHERE chat_id = %s", (chat_id,))
        if not row:
            log.warning(f"No user found for chat_id {chat_id}")
            return False
        status = (row.get("payment_status") or "").lower()
        is_reg = status == "registered"
        log.info(f"User {chat_id} registration status: {status}, is_registered: {is_reg}")
        return is_reg
    except psycopg.Error as e:
        log.error(f"DB query failed for is_registered {chat_id}: {e}")
        return False

def add_referral_if_absent(referrer: int, referee: int):
    if referrer == referee or referee <= 0:
        return
    r = db_fetchone("SELECT 1 FROM game_referrals WHERE referrer=%s AND referee=%s", (referrer, referee))
    if r:
        return
    now = db_now()
    db_execute("INSERT INTO game_referrals (referrer, referee, created_at) VALUES (%s,%s,%s)",
               (referrer, referee, now))
    db_execute("UPDATE users SET invites=COALESCE(invites,0)+1 WHERE chat_id=%s", (referrer,))

def get_game_user(chat_id: int) -> dict:
    row = db_fetchone("SELECT * FROM users WHERE chat_id = %s", (chat_id,))
    if not row:
        return {}
    return row

def update_game_user_fields(chat_id: int, fields: dict):
    keys = list(fields.keys())
    if not keys:
        return
    set_clause = ", ".join(f"{k}=%s" for k in keys)
    params = tuple(fields[k] for k in keys) + (chat_id,)
    db_execute(f"UPDATE users SET {set_clause} WHERE chat_id=%s", params)

def add_tap(chat_id: int, delta: int, nonce: str):
    now = db_now()
    db_execute("INSERT INTO game_taps (chat_id, ts, delta, nonce) VALUES (%s,%s,%s,%s)",
               (chat_id, now, delta, nonce))
    db_execute("UPDATE users SET coins=COALESCE(coins,0)+%s, last_tap_at=%s WHERE chat_id=%s",
               (delta, now, chat_id))

def leaderboard(range_: str = "all", limit: int = 50):
    if range_ == "all":
        q = "SELECT username, chat_id, coins AS score FROM users WHERE payment_status = 'registered' ORDER BY score DESC LIMIT %s"
        return db_fetchall(q, (limit,))
    else:
        now = db_now()
        if range_ == "day":
            since = now - timedelta(days=1)
        else:
            since = now - timedelta(days=7)
        q = """
            SELECT u.username, t.chat_id, COALESCE(SUM(t.delta),0) AS score
            FROM game_taps t
            LEFT JOIN users u ON u.chat_id=t.chat_id
            WHERE t.ts >= %s AND u.payment_status = 'registered'
            GROUP BY t.chat_id, u.username
            ORDER BY score DESC
            LIMIT %s
        """
        return db_fetchall(q, (since, limit))

def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()

def verify_init_data(init_data: str, bot_token: str) -> dict | None:
    try:
        items = dict(parse_qsl(init_data, strict_parsing=True))
        provided_hash = items.pop("hash", "")
        pairs = []
        for k in sorted(items.keys()):
            pairs.append(f"{k}={items[k]}")
        data_check_string = "\n".join(pairs).encode("utf-8")
        secret_key = _hmac_sha256(bot_token.encode("utf-8"), b"WebAppData")
        calc_hash = hmac.new(secret_key, data_check_string, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, provided_hash):
            return None
        user_payload = {}
        if "user" in items:
            user_payload = json.loads(items["user"])
        return {
            "ok": True,
            "user": user_payload,
            "query": items
        }
    except Exception as e:
        log.warning("verify_init_data error: %s", e)
        return None

# --- Aviator Game Logic ---
aviator_games = {}  # chat_id: {'bet': amount, 'start_time': time, 'cashed_out': False, 'crash_point': float}

def generate_crash_point():
    seed = str(random.randint(0, 1000000))
    random.seed(seed)
    return 1 / random.random()

def get_aviator_multiplier(start_time):
    elapsed = time.time() - start_time
    return 1 + (elapsed / 2)

# --- Football Prediction Logic ---
def get_football_matches():
    try:
        headers = {"X-Auth-Token": FOOTBALL_API_KEY}
        response = requests.get("https://api.football-data.org/v4/matches", headers=headers)
        if response.status_code != 200:
            log.error(f"Football API failed: {response.status_code} {response.text}")
            return []
        data = response.json()
        matches = []
        for match in data.get("matches", []):
            matches.append({
                "id": match["id"],
                "homeTeam": match["homeTeam"]["name"],
                "awayTeam": match["awayTeam"]["name"],
                "date": match["utcDate"],
                "status": match["status"]
            })
        return matches
    except Exception as e:
        log.error(f"Football API error: {e}")
        return []

def search_matches(query: str):
    matches = get_football_matches()
    query = query.lower().strip()
    if not query:
        return matches[:10]
    return [m for m in matches if query in m["homeTeam"].lower() or query in m["awayTeam"].lower()]

def generate_prediction(match_id):
    return f"Prediction for match {match_id}: 60% chance of home team win"

# --- Flask App ---
flask_app = Flask(__name__, template_folder='templates')

INDEX_HEALTH = "Tapify is alive!"

@flask_app.get("/")
def health():
    return Response(INDEX_HEALTH, mimetype="text/plain")

@flask_app.get("/app")
def app_page():
    chat_id = request.args.get('chat_id', type=int)
    if not chat_id:
        log.warning("Missing chat_id in /app request")
        return redirect('/error')
    log.info(f"Game accessed by chat_id: {chat_id}")
    return Response(WEBAPP_HTML, mimetype="text/html")

@flask_app.get("/error")
def error_page():
    return render_template('error.html', message="Unable to identify user. Please access the game through the Telegram bot.")

@flask_app.post("/api/auth/resolve")
def api_auth_resolve():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return jsonify({"ok": False, "error": "Invalid auth"})
    chat_id = int(auth["user"]["id"])
    username = auth["user"].get("username")
    if not is_registered(chat_id):
        return jsonify({"ok": True, "allowed": False, "user": {"chat_id": chat_id, "username": username}})
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{chat_id}" if BOT_USERNAME else ""
    return jsonify({
        "ok": True,
        "user": {"chat_id": chat_id, "username": username},
        "allowed": True,
        "refLink": ref_link,
        "aiLink": os.getenv("AI_BOOST_LINK", "#"),
        "dailyLink": os.getenv("DAILY_TASK_LINK", "#"),
        "groupLink": os.getenv("GROUP_LINK", "#"),
        "siteLink": os.getenv("SITE_LINK", "#"),
    })

@flask_app.post("/api/state")
def api_state():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return jsonify({"ok": False, "error": "Invalid auth"})
    chat_id = int(auth["user"]["id"])
    if not is_registered(chat_id):
        return jsonify({"ok": False, "error": "Not registered"})
    gu = get_game_user(chat_id)
    energy, energy_ts = compute_energy(gu)
    out = {
        "ok": True,
        "coins": int(gu.get("coins") or 0),
        "energy": energy,
        "max_energy": int(gu.get("max_energy") or 500),
        "daily_streak": int(gu.get("daily_streak") or 0),
    }
    update_game_user_fields(chat_id, {"energy": energy, "energy_updated_at": energy_ts})
    return jsonify(out)

@flask_app.post("/api/boost")
def api_boost():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    name = data.get("name", "")
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return jsonify({"ok": False, "error": "Invalid auth"})
    chat_id = int(auth["user"]["id"])
    if not is_registered(chat_id):
        return jsonify({"ok": False, "error": "Not registered"})
    ok, msg = activate_boost(chat_id, name)
    return jsonify({"ok": ok, "error": None if ok else msg})

@flask_app.post("/api/tap")
def api_tap():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    nonce = data.get("nonce", "")
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return jsonify({"ok": False, "error": "Invalid auth"})
    chat_id = int(auth["user"]["id"])
    if not is_registered(chat_id):
        return jsonify({"ok": False, "error": "Not registered"})
    if not can_tap_now(chat_id):
        return jsonify({"ok": False, "error": "Rate limited"})
    if not nonce or len(nonce) > 200:
        return jsonify({"ok": False, "error": "Bad nonce"})
    if nonce in _recent_nonces[chat_id]:
        return jsonify({"ok": False, "error": "Replay blocked"})
    _recent_nonces[chat_id].add(nonce)
    _clean_old_nonces(chat_id)
    gu = get_game_user(chat_id)
    energy, energy_ts = compute_energy(gu)
    if energy < 1:
        return jsonify({"ok": False, "error": "No energy", "coins": int(gu.get("coins") or 0),
                        "energy": energy, "max_energy": int(gu.get("max_energy") or 500)})
    mult = boost_multiplier(gu)
    delta = 2 * mult
    add_tap(chat_id, delta, nonce)
    update_game_user_fields(chat_id, {"energy": energy - 1, "energy_updated_at": energy_ts})
    new_streak, streak_date = streak_update(gu, tapped_today=True)
    update_game_user_fields(chat_id, {"daily_streak": new_streak, "last_streak_at": streak_date})
    gu2 = get_game_user(chat_id)
    energy2, _ = compute_energy(gu2)
    return jsonify({
        "ok": True,
        "coins": int(gu2.get("coins") or 0),
        "energy": energy2,
        "max_energy": int(gu2.get("max_energy") or 500),
    })

@flask_app.post("/api/daily_reward")
def api_daily_reward():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return jsonify({"ok": False, "error": "Invalid auth"})
    chat_id = int(auth["user"]["id"])
    if not is_registered(chat_id):
        return jsonify({"ok": False, "error": "Not registered"})
    gu = get_game_user(chat_id)
    last_reward = gu.get("last_daily_reward")
    today = db_date_utc()
    if last_reward and (isinstance(last_reward, date) and last_reward == today):
        return jsonify({"ok": False, "error": "Already claimed today"})
    coins = int(gu.get("coins") or 0) + 100
    update_game_user_fields(chat_id, {"coins": coins, "last_daily_reward": today})
    return jsonify({"ok": True, "coins": coins})

@flask_app.get("/api/leaderboard")
def api_leaderboard():
    rng = request.args.get("range", "all")
    if rng not in ("day", "week", "all"):
        rng = "all"
    items = leaderboard(rng, 50)
    return jsonify({"ok": True, "items": items})

@flask_app.post("/api/aviator/bet")
def api_aviator_bet():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    bet_amount = data.get("amount", 0)
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return jsonify({"ok": False, "error": "Invalid auth"})
    chat_id = int(auth["user"]["id"])
    if not is_registered(chat_id):
        return jsonify({"ok": False, "error": "Not registered"})
    gu = get_game_user(chat_id)
    coins = int(gu.get("coins") or 0)
    if bet_amount <= 0 or bet_amount > coins:
        return jsonify({"ok": False, "error": "Invalid bet"})
    update_game_user_fields(chat_id, {"coins": coins - bet_amount})
    crash_point = generate_crash_point()
    aviator_games[chat_id] = {'bet': bet_amount, 'start_time': time.time(), 'cashed_out': False, 'crash_point': crash_point}
    return jsonify({"ok": True, "started": True})

@flask_app.post("/api/aviator/state")
def api_aviator_state():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return jsonify({"ok": False, "error": "Invalid auth"})
    chat_id = int(auth["user"]["id"])
    if chat_id not in aviator_games:
        return jsonify({"ok": True, "active": False})
    game = aviator_games[chat_id]
    multiplier = get_aviator_multiplier(game['start_time'])
    crashed = multiplier >= game['crash_point']
    if crashed and not game['cashed_out']:
        del aviator_games[chat_id]
        return jsonify({"ok": True, "active": False, "crashed": True, "winnings": 0})
    return jsonify({"ok": True, "active": True, "multiplier": round(multiplier, 2), "crashed": crashed})

@flask_app.post("/api/aviator/cashout")
def api_aviator_cashout():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return jsonify({"ok": False, "error": "Invalid auth"})
    chat_id = int(auth["user"]["id"])
    if chat_id not in aviator_games:
        return jsonify({"ok": False, "error": "No active game"})
    game = aviator_games[chat_id]
    if game['cashed_out']:
        return jsonify({"ok": False, "error": "Already cashed out"})
    multiplier = get_aviator_multiplier(game['start_time'])
    if multiplier >= game['crash_point']:
        del aviator_games[chat_id]
        return jsonify({"ok": False, "error": "Crashed", "winnings": 0})
    winnings = int(game['bet'] * multiplier)
    gu = get_game_user(chat_id)
    coins = int(gu.get("coins") or 0) + winnings
    update_game_user_fields(chat_id, {"coins": coins})
    game['cashed_out'] = True
    del aviator_games[chat_id]
    return jsonify({"ok": True, "winnings": winnings})

@flask_app.post("/api/aviator/withdraw")
def api_aviator_withdraw():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    amount = data.get("amount", 0)
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return jsonify({"ok": False, "error": "Invalid auth"})
    chat_id = int(auth["user"]["id"])
    if not is_registered(chat_id):
        return jsonify({"ok": False, "error": "Not registered"})
    if amount < 50000:
        return jsonify({"ok": False, "error": "Minimum withdrawal 50,000 Naira"})
    gu = get_game_user(chat_id)
    coins = int(gu.get("coins") or 0)
    if amount > coins:
        return jsonify({"ok": False, "error": "Insufficient balance"})
    db_execute("INSERT INTO withdrawals (chat_id, amount) VALUES (%s, %s)", (chat_id, amount))
    try:
        bot.send_message(
            chat_id=ADMIN_ID,
            text=f"Withdrawal request from @{auth['user'].get('username', chat_id)}: {amount} Naira",
        )
    except Exception as e:
        log.error(f"Failed to notify admin for withdrawal {chat_id}: {e}")
    return jsonify({"ok": True, "message": "Withdrawal requested, awaiting approval"})

@flask_app.get("/api/deposit/accounts")
def api_deposit_accounts():
    if not BANK_ACCOUNTS:
        return jsonify({"ok": False, "error": "No bank accounts configured"})
    accounts = [acc.strip() for acc in BANK_ACCOUNTS.split(",")]
    return jsonify({"ok": True, "accounts": accounts})

@flask_app.post("/api/deposit/request")
def api_deposit_request():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    amount = data.get("amount", 0)
    bank_account = data.get("bank", "").strip()
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return jsonify({"ok": False, "error": "Invalid auth"})
    chat_id = int(auth["user"]["id"])
    if not is_registered(chat_id):
        return jsonify({"ok": False, "error": "Not registered"})
    if amount < 1000:
        return jsonify({"ok": False, "error": "Minimum deposit 1000 Naira"})
    accounts = [acc.strip() for acc in BANK_ACCOUNTS.split(",")]
    if bank_account not in accounts:
        return jsonify({"ok": False, "error": "Invalid bank account"})
    db_execute("INSERT INTO deposits (chat_id, amount, bank_account) VALUES (%s, %s, %s)", 
               (chat_id, amount, bank_account))
    deposit_id = db_fetchone("SELECT id FROM deposits WHERE chat_id = %s ORDER BY requested_at DESC LIMIT 1", (chat_id,))["id"]
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve", callback_data=f"deposit_approve_{deposit_id}_{chat_id}_{amount}"),
            InlineKeyboardButton("Reject", callback_data=f"deposit_reject_{deposit_id}_{chat_id}")
        ]
    ])
    try:
        bot.send_message(
            chat_id=ADMIN_ID,
            text=f"Deposit request from @{auth['user'].get('username', chat_id)}: {amount} Naira to {bank_account}",
            reply_markup=keyboard
        )
    except Exception as e:
        log.error(f"Failed to notify admin for deposit {chat_id}: {e}")
    return jsonify({"ok": True, "message": "Deposit requested, please make payment and await approval"})

@flask_app.post("/api/prediction/matches")
def api_prediction_matches():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    query = data.get("query", "").strip()
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return jsonify({"ok": False, "error": "Invalid auth"})
    chat_id = int(auth["user"]["id"])
    if not is_registered(chat_id):
        return jsonify({"ok": False, "error": "Not registered"})
    matches = search_matches(query)
    return jsonify({"ok": True, "matches": matches})

@flask_app.post("/api/prediction/request")
def api_prediction_request():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    query = data.get("query", "").strip()
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return jsonify({"ok": False, "error": "Invalid auth"})
    chat_id = int(auth["user"]["id"])
    if not is_registered(chat_id):
        return jsonify({"ok": False, "error": "Not registered"})
    gu = get_game_user(chat_id)
    coins = int(gu.get("coins") or 0)
    if coins < 500:
        return jsonify({"ok": False, "error": "Need 500 coins for prediction"})
    matches = search_matches(query)
    if not matches:
        return jsonify({"ok": False, "error": "Match not available"})
    match = matches[0]
    update_game_user_fields(chat_id, {"coins": coins - 500})
    prediction = generate_prediction(match["id"])
    return jsonify({"ok": True, "prediction": prediction})

# --- Bot Username Setup ---
BOT_USERNAME = ""

def set_bot_username():
    global BOT_USERNAME
    try:
        me = bot.get_me()
        BOT_USERNAME = me.username
        log.info("Bot username: @%s", BOT_USERNAME)
    except Exception as e:
        log.error(f"Failed to get bot username: {e}")

# --- Helper Functions ---
MAX_TAPS_PER_SEC = 20
RATE_WINDOW_SEC = 1.0

_rate_windows: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=MAX_TAPS_PER_SEC * 3))
_recent_nonces: dict[int, set[str]] = defaultdict(set)

def _clean_old_nonces(chat_id: int):
    s = _recent_nonces[chat_id]
    if len(s) > 200:
        _recent_nonces[chat_id] = set(list(s)[-100:])

def can_tap_now(chat_id: int) -> bool:
    now = time.monotonic()
    dq = _rate_windows[chat_id]
    while dq and now - dq[0] > RATE_WINDOW_SEC:
        dq.popleft()
    if len(dq) >= MAX_TAPS_PER_SEC:
        return False
    dq.append(now)
    return True

def compute_energy(user_row: dict) -> tuple[int, datetime]:
    max_energy = int(user_row.get("max_energy") or 500)
    regen_rate_seconds = int(user_row.get("regen_rate_seconds") or 3)
    raw = user_row.get("energy_updated_at")
    if isinstance(raw, str):
        try:
            last = datetime.fromisoformat(raw)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except Exception:
            last = db_now()
    else:
        last = raw or db_now()
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    stored_energy = int(user_row.get("energy") or 0)
    now = db_now()
    elapsed = int((now - last).total_seconds())
    regen = elapsed // max(1, regen_rate_seconds)
    energy = min(max_energy, stored_energy + regen)
    if regen > 0:
        last = last + timedelta(seconds=regen * regen_rate_seconds)
    return energy, last

def streak_update(gu: dict, tapped_today: bool) -> tuple[int, date]:
    today = db_date_utc()
    last_str = gu.get("last_streak_at")
    last_date: date | None = None
    if isinstance(last_str, str) and last_str:
        try:
            last_date = datetime.fromisoformat(last_str).date()
        except Exception:
            last_date = None
    elif isinstance(last_str, datetime):
        last_date = last_str.date()
    elif isinstance(last_str, date):
        last_date = last_str
    streak = int(gu.get("daily_streak") or 0)
    if not tapped_today:
        return streak, last_date or today
    if last_date == today - timedelta(days=1):
        streak += 1
    elif last_date == today:
        pass
    else:
        streak = 1
    return streak, today

def boost_multiplier(gu: dict) -> int:
    mult = 1
    mt = gu.get("multitap_until")
    at = gu.get("autotap_until")
    now = db_now()
    if isinstance(mt, str) and mt:
        try: mt = datetime.fromisoformat(mt)
        except: mt = None
    if isinstance(at, str) and at:
        try: at = datetime.fromisoformat(at)
        except: at = None
    if isinstance(mt, datetime) and mt.replace(tzinfo=timezone.utc) > now:
        mult = max(mult, 2)
    if isinstance(at, datetime) and at.replace(tzinfo=timezone.utc) > now:
        mult = max(mult, 2)
    return mult

def activate_boost(chat_id: int, boost: str) -> tuple[bool, str]:
    gu = get_game_user(chat_id)
    coins = int(gu.get("coins") or 0)
    now = db_now()
    cost = 0
    field = None
    duration = timedelta(minutes=15)
    if boost == "multitap":
        cost = 500
        field = "multitap_until"
        duration = timedelta(minutes=30)
    elif boost == "autotap":
        cost = 3000
        field = "autotap_until"
        duration = timedelta(minutes=10)
    elif boost == "maxenergy":
        cost = 2500
        if coins < cost:
            return False, "Not enough coins"
        update_game_user_fields(chat_id, {
            "coins": coins - cost,
            "max_energy": int(gu.get("max_energy") or 500) + 100
        })
        return True, "Max energy increased by +100!"
    else:
        return False, "Unknown boost"
    if coins < cost:
        return False, "Not enough coins"
    until = now + duration
    update_game_user_fields(chat_id, {
        "coins": coins - cost,
        field: until
    })
    return True, f"{boost} activated!"

# --- Run Flask App ---
def run_flask():
    db_init()
    set_bot_username()
    flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
