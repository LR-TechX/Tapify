#!/usr/bin/env python3
# app.py — Tapify Game Mini App for Render deployment
# Requirements:
#   pip install flask psycopg[binary] python-dotenv uvicorn requests
#
# Start:
#   python app.py
#
# Environment (.env):
#   BOT_TOKEN=7645079949:AAEkgyy1GTzXXy45LtouLVRaLIGM4g_3WyM
#   ADMIN_ID=5646269450
#   WEBAPP_URL=https://tapify.onrender.com/app
#   DATABASE_URL=postgres://user:pass@internal-host:5432/dbname  # Use Internal DB URL
#   BANK_ACCOUNTS=Bank1:1234567890,Bank2:0987654321  # Comma-separated
#   FOOTBALL_API_KEY=your_api_key_here
#   AI_BOOST_LINK=#
#   DAILY_TASK_LINK=#
#   GROUP_LINK=#
#   SITE_LINK=#

import os
import sys
import json
import hmac
import base64
import hashlib
import logging
import asyncio
import typing as t
from urllib.parse import parse_qsl, quote_plus
from datetime import datetime, timedelta, timezone, date
from collections import deque, defaultdict
import random
import time
import requests  # For football API

from flask import Flask, request, jsonify, Response
import uvicorn
from uvicorn.middleware.wsgi import WSGIMiddleware
from telegram import Bot  # For admin notifications

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# --- Config & Globals ---------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BANK_ACCOUNTS = os.getenv("BANK_ACCOUNTS", "").strip()  # Format: "Name1:Account1,Name2:Account2"
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "").strip()

AI_BOOST_LINK = os.getenv("AI_BOOST_LINK", "#")
DAILY_TASK_LINK = os.getenv("DAILY_TASK_LINK", "#")
GROUP_LINK = os.getenv("GROUP_LINK", "#")
SITE_LINK = os.getenv("SITE_LINK", "#")

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN is required in environment (.env).", file=sys.stderr)
    sys.exit(1)
if not ADMIN_ID:
    print("ERROR: ADMIN_ID is required in environment (.env).", file=sys.stderr)
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

psycopg = None
conn = None  # type: ignore

def _connect_postgres(url: str):
    global psycopg
    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception as e:
        log.error("psycopg (v3) is required for Postgres: pip install 'psycopg[binary]'\n%s", e)
        raise
    if "sslmode=" not in url:
        if "?" in url:
            url += "&sslmode=require"
        else:
            url += "?sslmode=require"
    try:
        conn_pg = psycopg.connect(url, row_factory=dict_row)
        conn_pg.autocommit = True
        log.info("Postgres connected successfully")
        return conn_pg
    except Exception as e:
        log.error(f"Postgres connection failed: {e}")
        raise

def db_execute(query: str, params: t.Tuple = ()):
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            log.debug(f"DB execute: {query} with params {params}")
    except Exception as e:
        log.error(f"DB execute failed: {query} | Error: {e}")
        raise

def db_fetchone(query: str, params: t.Tuple = ()):
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            result = cur.fetchone()
            log.debug(f"DB fetchone: {query} with params {params} -> {result}")
            return result
    except Exception as e:
        log.error(f"DB fetchone failed: {query} | Error: {e}")
        raise

def db_fetchall(query: str, params: t.Tuple = ()):
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            result = cur.fetchall()
            log.debug(f"DB fetchall: {query} with params {params} -> {result}")
            return result
    except Exception as e:
        log.error(f"DB fetchall failed: {query} | Error: {e}")
        raise

def db_init():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL must be set for PostgreSQL")
    global conn
    conn = _connect_postgres(DATABASE_URL)
    db_execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id BIGINT PRIMARY KEY,
        username TEXT,
        payment_status TEXT DEFAULT NULL,
        invites INTEGER DEFAULT 0
    );
    """)
    db_execute("""
    CREATE TABLE IF NOT EXISTS game_users (
        chat_id BIGINT PRIMARY KEY,
        coins BIGINT DEFAULT 0,
        energy INT DEFAULT 500,
        max_energy INT DEFAULT 500,
        energy_updated_at TIMESTAMP,
        multitap_until TIMESTAMP,
        autotap_until TIMESTAMP,
        regen_rate_seconds INT DEFAULT 3,
        last_tap_at TIMESTAMP,
        daily_streak INT DEFAULT 0,
        last_streak_at DATE,
        last_daily_reward DATE
    );
    """)
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

def db_now() -> datetime:
    return datetime.now(timezone.utc)

def db_date_utc() -> date:
    return db_now().date()

def upsert_user_if_missing(chat_id: int, username: str | None):
    existing = db_fetchone("SELECT chat_id FROM users WHERE chat_id = %s", (chat_id,))
    if not existing:
        db_execute("INSERT INTO users (chat_id, username, payment_status, invites) VALUES (%s,%s,%s,%s)",
                   (chat_id, username, None, 0))
    existing_g = db_fetchone("SELECT chat_id FROM game_users WHERE chat_id = %s", (chat_id,))
    if not existing_g:
        now = db_now()
        db_execute("""INSERT INTO game_users
            (chat_id, coins, energy, max_energy, energy_updated_at, regen_rate_seconds, daily_streak, last_daily_reward)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (chat_id, 0, 500, 500, now, 3, 0, None))

def is_registered(chat_id: int) -> bool:
    try:
        row = db_fetchone("SELECT payment_status FROM users WHERE chat_id = %s", (chat_id,))
        log.info(f"Checked registration for {chat_id}: {row}")
        if not row:
            return False
        return (row.get("payment_status") or "").lower() == "registered"
    except Exception as e:
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
    row = db_fetchone("SELECT * FROM game_users WHERE chat_id = %s", (chat_id,))
    if not row:
        upsert_user_if_missing(chat_id, None)
        row = db_fetchone("SELECT * FROM game_users WHERE chat_id = %s", (chat_id,))
    return row or {}

def update_game_user_fields(chat_id: int, fields: dict):
    keys = list(fields.keys())
    if not keys:
        return
    set_clause = ", ".join(f"{k}=%s" for k in keys)
    params = tuple(fields[k] for k in keys) + (chat_id,)
    db_execute(f"UPDATE game_users SET {set_clause} WHERE chat_id=%s", params)

def add_tap(chat_id: int, delta: int, nonce: str):
    now = db_now()
    db_execute("INSERT INTO game_taps (chat_id, ts, delta, nonce) VALUES (%s,%s,%s,%s)",
               (chat_id, now, delta, nonce))
    db_execute("UPDATE game_users SET coins=COALESCE(coins,0)+%s, last_tap_at=%s WHERE chat_id=%s",
               (delta, now, chat_id))

def leaderboard(range_: str = "all", limit: int = 50):
    if range_ == "all":
        q = "SELECT u.username, g.chat_id, g.coins AS score FROM game_users g LEFT JOIN users u ON u.chat_id=g.chat_id ORDER BY score DESC LIMIT %s"
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
            WHERE t.ts >= %s
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

def generate_prediction(match_id):
    # Placeholder: Random prediction (e.g., 60% home win)
    return f"Prediction for match {match_id}: 60% chance of home team win"

# --- Flask App ---
flask_app = Flask(__name__)

INDEX_HEALTH = "Tapify is alive!"

WEBAPP_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Tapify</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body { 
      background: radial-gradient(1200px 600px at 50% -100px, rgba(255,215,0,0.12), transparent 70%), #0b0f14; 
      font-family: 'Arial', sans-serif;
    }
    .coin { ... } /* Existing styles unchanged */
    /* Add plane animation for Aviator */
    @keyframes fly {
      0% { transform: translateY(0) rotate(45deg); }
      100% { transform: translateY(-200px) rotate(45deg); }
    }
    #plane.active {
      animation: fly 5s linear;
    }
  </style>
</head>
<body class="text-white">
  <div id="root" class="max-w-sm mx-auto px-4 pt-6 pb-24">
    <div class="flex items-center justify-between">
      <div class="text-2xl font-bold text-orange-500">Tapify Adventure</div>
      <div id="streak" class="text-sm opacity-80">🔥 Streak: 0</div>
    </div>
    <div id="locked" class="hidden mt-10 text-center">
      <div class="text-3xl font-bold mb-3">Access Locked</div>
      <div class="opacity-80 mb-6">Complete registration in the bot to start playing.</div>
      <button id="btnCheck" class="px-4 py-2 rounded-lg bg-orange-500 text-black font-semibold">Check again</button>
      <div class="mt-6 text-xs opacity-60">If this persists, close and reopen the webapp.</div>
    </div>
    <div id="game" class="mt-8">
      <div class="flex items-center justify-center relative">
        <div id="energyRing" class="relative glow">
          <div id="tapBtn" class="coin select-none flex items-center justify-center text-4xl font-extrabold text-white">TAP!</div>
        </div>
      </div>
      <div class="mt-6 text-center">
        <div id="balance" class="text-5xl font-extrabold text-orange-400">0</div>
        <div id="energy" class="mt-1 text-sm opacity-80">⚡ 0 / 0</div>
      </div>
      <div class="mt-8 grid grid-cols-5 gap-2 text-center text-sm">
        <button class="tab active py-2" data-tab="play">Play</button>
        <button class="tab py-2" data-tab="boosts">Boosts</button>
        <button class="tab py-2" data-tab="board">Leaderboard</button>
        <button class="tab py-2" data-tab="refer">Refer</button>
        <button class="tab py-2" data-tab="aviator">Aviator</button>
        <button class="tab py-2" data-tab="predict">Predict</button>
      </div>
      <div id="panelPlay" class="mt-6">
        <button id="dailyRewardBtn" class="px-4 py-2 rounded-lg bg-orange-500 text-black font-semibold w-full mb-4">Claim Daily Reward</button>
      </div>
      <div id="panelBoosts" class="hidden mt-6 space-y-3"> <!-- Existing Boosts --> </div>
      <div id="panelBoard" class="hidden mt-6"> <!-- Existing Leaderboard --> </div>
      <div id="panelRefer" class="hidden mt-6"> <!-- Existing Refer --> </div>
      <div id="panelAviator" class="hidden mt-6">
        <div class="bg-white/5 p-4 rounded-xl shadow-lg text-center">
          <div id="plane" class="w-20 h-20 mx-auto bg-orange-500 rotate-45 mb-4"></div>
          <div id="multiplier" class="text-4xl font-bold text-orange-400">1.00x</div>
          <input id="betAmount" type="number" placeholder="Bet amount" class="w-full px-3 py-2 rounded bg-black/30 border border-white/10 mt-4" />
          <button id="placeBet" class="mt-2 px-3 py-2 rounded bg-orange-500 text-black w-full">Place Bet</button>
          <button id="cashOut" class="mt-2 px-3 py-2 rounded bg-green-500 text-black w-full hidden">Cash Out</button>
          <div class="mt-4">
            <button id="fundBtn" class="px-3 py-1 rounded bg-blue-500 text-white">Fund Account</button>
            <button id="withdrawBtn" class="ml-2 px-3 py-1 rounded bg-red-500 text-white">Withdraw</button>
          </div>
        </div>
      </div>
      <div id="panelPredict" class="hidden mt-6">
        <div class="bg-white/5 p-4 rounded-xl shadow-lg">
          <div class="font-semibold text-orange-400 mb-2">Football Predictions</div>
          <input id="matchSearch" class="w-full px-3 py-2 rounded bg-black/30 border border-white/10" placeholder="Search match (e.g., Arsenal vs Chelsea)" />
          <div id="matchList" class="mt-4 space-y-2"></div>
          <button id="predictBtn" class="mt-4 px-3 py-2 rounded bg-orange-500 text-black w-full">Get Prediction (500 coins)</button>
        </div>
      </div>
    </div>
  </div>
<script>
const tg = window.Telegram?.WebApp;
if (tg) tg.expand();
const $ = (q) => document.querySelector(q);
const $$ = (q) => Array.from(document.querySelectorAll(q));
function setTab(name) {
  $$(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $("#panelPlay").classList.toggle("hidden", name !== "play");
  $("#panelBoosts").classList.toggle("hidden", name !== "boosts");
  $("#panelBoard").classList.toggle("hidden", name !== "board");
  $("#panelRefer").classList.toggle("hidden", name !== "refer");
  $("#panelAviator").classList.toggle("hidden", name !== "aviator");
  $("#panelPredict").classList.toggle("hidden", name !== "predict");
  if (name === "predict") loadMatches();
}
$$(".tab").forEach(b => b.addEventListener("click", () => setTab(b.dataset.tab)));
const haptics = (type = "light") => {
  try { tg?.HapticFeedback?.impactOccurred(type); } catch (e) {}
};
let USER = null;
let LOCKED = false;
let RANGE = "all";
async function api(path, body) {
  const initData = tg?.initData || "";
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(Object.assign({ initData }, body || {}))
  });
  return await res.json();
}
async function resolveAuth() {
  const out = await api("/api/auth/resolve");
  if (!out.ok) {
    $("#locked").classList.remove("hidden");
    $("#game").classList.add("lock");
    LOCKED = true;
    return;
  }
  USER = out.user;
  LOCKED = !out.allowed;
  $("#locked").classList.toggle("hidden", out.allowed);
  $("#game").classList.toggle("lock", !out.allowed);
  $("#refLink").value = out.refLink;
  $("#aiLink").href = out.aiLink;
  $("#dailyLink").href = out.dailyLink;
  $("#groupLink").href = out.groupLink;
  $("#siteLink").href = out.siteLink;
  if (out.allowed) await refreshState();
}
$("#btnCheck").addEventListener("click", resolveAuth);
async function refreshState() {
  const out = await api("/api/state");
  if (!out.ok) return;
  $("#balance").textContent = out.coins;
  $("#energy").textContent = `⚡ ${out.energy} / ${out.max_energy}`;
  $("#streak").textContent = `🔥 Streak: ${out.daily_streak || 0}`;
}
// ... existing JS for tap, boosts, leaderboard, refer ...
let aviatorInterval;
$("#placeBet").addEventListener("click", async () => {
  const amount = parseInt($("#betAmount").value);
  if (amount <= 0) return alert("Invalid bet");
  const out = await api("/api/aviator/bet", {amount});
  if (out.ok) {
    $("#placeBet").classList.add("hidden");
    $("#cashOut").classList.remove("hidden");
    $("#plane").classList.add("active");
    aviatorInterval = setInterval(updateAviator, 100);
  } else alert(out.error);
});
$("#cashOut").addEventListener("click", async () => {
  const out = await api("/api/aviator/cashout");
  if (out.ok) {
    clearInterval(aviatorInterval);
    $("#plane").classList.remove("active");
    alert(`Cashed out! Winnings: ${out.winnings}`);
    $("#cashOut").classList.add("hidden");
    $("#placeBet").classList.remove("hidden");
    await refreshState();
  } else alert(out.error);
});
async function updateAviator() {
  const out = await api("/api/aviator/state");
  if (out.ok) {
    $("#multiplier").textContent = `${out.multiplier}x`;
    if (out.crashed) {
      clearInterval(aviatorInterval);
      $("#plane").classList.remove("active");
      $("#cashOut").classList.add("hidden");
      $("#placeBet").classList.remove("hidden");
      alert("Crashed! Lost bet.");
    }
  }
}
$("#fundBtn").addEventListener("click", async () => {
  const accounts = await (await fetch("/api/deposit/accounts")).json();
  if (!accounts.ok) return alert(accounts.error);
  const bank = prompt(`Select bank account:\n${accounts.accounts.join("\n")}`);
  if (!bank) return;
  const amount = parseInt(prompt("Enter amount (min 1000 Naira):"));
  if (amount < 1000) return alert("Minimum deposit 1000 Naira");
  const out = await api("/api/deposit/request", {amount, bank});
  if (out.ok) alert(out.message);
  else alert(out.error);
});
$("#withdrawBtn").addEventListener("click", async () => {
  const amount = parseInt(prompt("Enter amount (min 50000 Naira):"));
  if (amount < 50000) return alert("Minimum withdrawal 50000 Naira");
  const out = await api("/api/aviator/withdraw", {amount});
  if (out.ok) alert(out.message);
  else alert(out.error);
});
async function loadMatches() {
  const out = await api("/api/prediction/matches");
  const list = $("#matchList");
  list.innerHTML = "";
  if (!out.ok) {
    list.innerHTML = `<div class="text-red-500">${out.error}</div>`;
    return;
  }
  out.matches.forEach(m => {
    const div = document.createElement("div");
    div.className = "bg-white/10 p-2 rounded";
    div.innerHTML = `${m.homeTeam} vs ${m.awayTeam} (${m.date})`;
    div.dataset.matchId = m.id;
    div.addEventListener("click", () => $("#matchSearch").value = `${m.homeTeam} vs ${m.awayTeam}`);
    list.appendChild(div);
  });
}
$("#predictBtn").addEventListener("click", async () => {
  const query = $("#matchSearch").value;
  if (!query) return alert("Enter a match to predict");
  const out = await api("/api/prediction/request", {query});
  if (out.ok) {
    alert(out.prediction);
    await refreshState();
  } else alert(out.error);
});
$("#matchSearch").addEventListener("input", loadMatches);
setTab("play");
resolveAuth();
setInterval(refreshState, 4000);
</script>
</body>
</html>
"""

@flask_app.get("/")
def health():
    return Response(INDEX_HEALTH, mimetype="text/plain")

@flask_app.get("/app")
def app_page():
    return Response(WEBAPP_HTML, mimetype="text/html")

@flask_app.post("/api/auth/resolve")
def api_auth_resolve():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    ok, user, err = _resolve_user_from_init(init_data)
    if notarında

System: * Today's date and time is 04:32 AM WAT on Thursday, August 21, 2025.
