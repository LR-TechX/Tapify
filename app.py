#!/usr/bin/env python3
# app.py — Tapify Game Mini App for Render deployment
# Requirements:
#   pip install flask psycopg[binary] python-dotenv uvicorn requests python-telegram-bot==20.7
#
# Start:
#   python app.py
#
# Environment (.env):
#   BOT_TOKEN=7645079949:AAEkgyy1GTzXXy45LtouLVRaLIGM4g_3WyM
#   ADMIN_ID=5646269450
#   WEBAPP_URL=https://tapify.onrender.com/app
#   DATABASE_URL=postgres://user:pass@internal-host:5432/dbname  # Use Internal DB URL
#   BANK_ACCOUNTS=FirstBank:1234567890,GTBank:0987654321
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
import requests
from flask import Flask, request, jsonify, Response
import uvicorn
from uvicorn.middleware.wsgi import WSGIMiddleware
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

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
BANK_ACCOUNTS = os.getenv("BANK_ACCOUNTS", "").strip()
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
conn = None

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
            log.warning(f"No user found for chat_id {chat_id}")
            return False
        status = (row.get("payment_status") or "").lower()
        is_reg = status == "registered"
        log.info(f"User {chat_id} registration status: {status}, is_registered: {is_reg}")
        return is_reg
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

def search_matches(query: str):
    matches = get_football_matches()
    query = query.lower().strip()
    if not query:
        return matches[:10]  # Top 10 if no query
    return [m for m in matches if query in m["homeTeam"].lower() or query in m["awayTeam"].lower()]

def generate_prediction(match_id):
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
    .coin {
      width: 220px; height: 220px; border-radius: 50%;
      background: radial-gradient(circle at 30% 30%, #ff4500, #8b0000);
      box-shadow: 0 0 30px rgba(255,69,0,0.6), inset 0 0 20px rgba(255,255,255,0.3);
      transition: transform 0.1s ease-in-out, box-shadow 0.3s ease;
      animation: pulse 2s infinite;
    }
    @keyframes pulse {
      0% { transform: scale(1); }
      50% { transform: scale(1.05); }
      100% { transform: scale(1); }
    }
    .coin:active { 
      transform: scale(0.95); 
      box-shadow: 0 0 50px rgba(255,69,0,0.8), inset 0 0 40px rgba(255,255,255,0.4); 
      animation: none;
    }
    .glow { filter: drop-shadow(0 0 20px rgba(255,69,0,0.6)); }
    .tab { 
      opacity: 0.6; 
      transition: opacity 0.3s ease, border-bottom 0.3s ease; 
    }
    .tab.active { 
      opacity: 1; 
      border-bottom: 3px solid #ff4500; 
      color: #ff4500;
    }
    .tab:hover {
      opacity: 0.9;
    }
    .lock { filter: grayscale(0.6); }
    .boostBtn, .actionBtn {
      transition: background-color 0.3s ease, transform 0.2s ease;
    }
    .boostBtn:hover, .actionBtn:hover {
      background-color: #ff4500 !important;
      transform: scale(1.05);
    }
    .particle {
      position: absolute;
      width: 10px;
      height: 10px;
      background: #ff4500;
      border-radius: 50%;
      opacity: 0;
      animation: particle-burst 1s ease-out forwards;
    }
    @keyframes particle-burst {
      0% { transform: translate(0, 0); opacity: 1; }
      100% { transform: translate(var(--dx), var(--dy)); opacity: 0; }
    }
    #balance {
      animation: balance-update 0.5s ease;
    }
    @keyframes balance-update {
      0% { transform: scale(1.2); color: #ff4500; }
      100% { transform: scale(1); color: white; }
    }
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
      <div class="mt-8 grid grid-cols-6 gap-2 text-center text-sm">
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
      <div id="panelBoosts" class="hidden mt-6 space-y-3">
        <div class="bg-white/5 p-4 rounded-xl shadow-lg">
          <div class="font-semibold text-orange-400">MultiTap x2 (30m)</div>
          <div class="text-xs opacity-70 mb-2">Cost: 500</div>
          <button data-boost="multitap" class="boostBtn px-3 py-2 rounded-lg bg-orange-500 text-black w-full">Activate</button>
        </div>
        <div class="bg-white/5 p-4 rounded-xl shadow-lg">
          <div class="font-semibold text-orange-400">AutoTap (10m)</div>
          <div class="text-xs opacity-70 mb-2">Cost: 3000</div>
          <button data-boost="autotap" class="boostBtn px-3 py-2 rounded-lg bg-orange-500 text-black w-full">Activate</button>
        </div>
        <div class="bg-white/5 p-4 rounded-xl shadow-lg">
          <div class="font-semibold text-orange-400">Increase Max Energy +100</div>
          <div class="text-xs opacity-70 mb-2">Cost: 2500</div>
          <button data-boost="maxenergy" class="boostBtn px-3 py-2 rounded-lg bg-orange-500 text-black w-full">Upgrade</button>
        </div>
      </div>
      <div id="panelBoard" class="hidden mt-6">
        <div class="flex gap-2 text-sm">
          <button class="lbBtn px-3 py-1 rounded bg-white/10" data-range="day">Today</button>
          <button class="lbBtn px-3 py-1 rounded bg-white/10" data-range="week">This Week</button>
          <button class="lbBtn px-3 py-1 rounded bg-white/10" data-range="all">All Time</button>
        </div>
        <ol id="lbList" class="mt-4 space-y-2"></ol>
      </div>
      <div id="panelRefer" class="hidden mt-6">
        <div class="bg-white/5 p-4 rounded-xl shadow-lg">
          <div class="font-semibold text-orange-400 mb-1">Invite Friends & Earn!</div>
          <div class="text-xs opacity-70 mb-2">Share your link to earn bonuses.</div>
          <input id="refLink" class="w-full px-3 py-2 rounded bg-black/30 border border-white/10" readonly />
          <button id="copyRef" class="mt-2 px-3 py-2 rounded bg-orange-500 text-black w-full">Copy Link</button>
        </div>
        <div class="mt-4 grid grid-cols-2 gap-3 text-sm">
          <a href="#" id="aiLink" class="text-center bg-white/5 p-3 rounded-lg shadow-lg">AI Boost Task</a>
          <a href="#" id="dailyLink" class="text-center bg-white/5 p-3 rounded-lg shadow-lg">Daily Task</a>
          <a href="#" id="groupLink" class="text-center bg-white/5 p-3 rounded-lg shadow-lg">Join Group</a>
          <a href="#" id="siteLink" class="text-center bg-white/5 p-3 rounded-lg shadow-lg">Visit Site</a>
        </div>
      </div>
      <div id="panelAviator" class="hidden mt-6">
        <div class="bg-white/5 p-4 rounded-xl shadow-lg text-center">
          <div id="plane" class="w-20 h-20 mx-auto bg-orange-500 rotate-45 mb-4"></div>
          <div id="multiplier" class="text-4xl font-bold text-orange-400">1.00x</div>
          <input id="betAmount" type="number" placeholder="Bet amount" class="w-full px-3 py-2 rounded bg-black/30 border border-white/10 mt-4" />
          <button id="placeBet" class="mt-2 px-3 py-2 rounded bg-orange-500 text-black w-full">Place Bet</button>
          <button id="cashOut" class="mt-2 px-3 py-2 rounded bg-green-500 text-black w-full hidden">Cash Out</button>
          <div class="mt-4">
            <button id="fundBtn" class="actionBtn px-3 py-1 rounded bg-blue-500 text-white">Fund Account</button>
            <button id="withdrawBtn" class="actionBtn ml-2 px-3 py-1 rounded bg-red-500 text-white">Withdraw</button>
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
function createParticles(x, y, count = 10) {
  for (let i = 0; i < count; i++) {
    const particle = document.createElement('div');
    particle.className = 'particle';
    particle.style.left = `${x}px`;
    particle.style.top = `${y}px`;
    const angle = Math.random() * 2 * Math.PI;
    const dist = Math.random() * 50 + 20;
    particle.style.setProperty('--dx', `${Math.cos(angle) * dist}px`);
    particle.style.setProperty('--dy', `${Math.sin(angle) * dist}px`);
    document.body.appendChild(particle);
    setTimeout(() => particle.remove(), 1000);
  }
}
async function doTap(e) {
  if (LOCKED) return;
  const nonce = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(12))));
  const out = await api("/api/tap", { nonce });
  if (!out.ok) {
    if (out.error) console.log(out.error);
    return;
  }
  haptics("light");
  $("#balance").textContent = out.coins;
  $("#balance").style.animation = 'balance-update 0.5s ease';
  setTimeout(() => $("#balance").style.animation = '', 500);
  $("#energy").textContent = `⚡ ${out.energy} / ${out.max_energy}`;
  const rect = $("#tapBtn").getBoundingClientRect();
  createParticles(rect.left + rect.width / 2, rect.top + rect.height / 2);
}
$("#tapBtn").addEventListener("click", doTap);
$$(".boostBtn").forEach(b => {
  b.addEventListener("click", async () => {
    const out = await api("/api/boost", { name: b.dataset.boost });
    if (out.ok) { await refreshState(); haptics("medium"); }
    else if (out.error) alert(out.error);
  });
});
$$(".lbBtn").forEach(b => {
  b.addEventListener("click", async () => {
    RANGE = b.dataset.range;
    const q = await fetch(`/api/leaderboard?range=${RANGE}`);
    const data = await q.json();
    const list = $("#lbList"); list.innerHTML = "";
    (data.items || []).forEach((r, i) => {
      const li = document.createElement("li");
      li.className = "flex justify-between bg-white/5 px-3 py-2 rounded-lg shadow-md";
      li.innerHTML = `<div>#${i+1} @${r.username || r.chat_id}</div><div>${r.score}</div>`;
      list.appendChild(li);
    });
  });
});
$("#copyRef").addEventListener("click", () => {
  navigator.clipboard.writeText($("#refLink").value);
  haptics("light");
});
$("#dailyRewardBtn").addEventListener("click", async () => {
  const out = await api("/api/daily_reward");
  if (out.ok) {
    await refreshState();
    haptics("medium");
    alert("Claimed 100 coins!");
  } else if (out.error) {
    alert(out.error);
  }
});
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
  const query = $("#matchSearch").value;
  const out = await api("/api/prediction/matches", {query});
  const list = $("#matchList");
  list.innerHTML = "";
  if (!out.ok) {
    list.innerHTML = `<div class="text-red-500">${out.error}</div>`;
    return;
  }
  if (out.matches.length === 0) {
    list.innerHTML = `<div class="text-yellow-500">No matches found</div>`;
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
    if not ok:
        return jsonify({"ok": False, "error": err})
    chat_id = user["chat_id"]
    allowed = is_registered(chat_id)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{chat_id}" if BOT_USERNAME else ""
    return jsonify({
        "ok": True,
        "user": user,
        "allowed": allowed,
        "refLink": ref_link,
        "aiLink": AI_BOOST_LINK,
        "dailyLink": DAILY_TASK_LINK,
        "groupLink": GROUP_LINK,
        "siteLink": SITE_LINK,
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
    if last_reward and (isinstance(last_reward, str) and last_reward == today.isoformat() or
                        isinstance(last_reward, date) and last_reward == today):
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

@flask_app.get("/api/debug_user/<int:chat_id>")
def debug_user(chat_id):
    try:
        result = db_fetchone("SELECT * FROM users WHERE chat_id = %s", (chat_id,))
        return jsonify(result or {"error": "User not found"})
    except Exception as e:
        log.error(f"Debug user failed for {chat_id}: {e}")
        return jsonify({"error": str(e)})

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
        asyncio.run(bot.send_message(
            chat_id=ADMIN_ID,
            text=f"Withdrawal request from @{auth['user'].get('username', chat_id)}: {amount} Naira",
        ))
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
        asyncio.run(bot.send_message(
            chat_id=ADMIN_ID,
            text=f"Deposit request from @{auth['user'].get('username', chat_id)}: {amount} Naira to {bank_account}",
            reply_markup=keyboard
        ))
    except Exception as e:
        log.error(f"Failed to notify admin for deposit {chat_id}: {e}")
    return jsonify({"ok": True, "message": "Deposit requested, please make payment and await approval"})

@flask_app.post("/api/deposit/approve")
def api_deposit_approve():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    deposit_id = data.get("deposit_id", 0)
    chat_id = data.get("chat_id", 0)
    amount = data.get("amount", 0)
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user") or int(auth["user"]["id"]) != ADMIN_ID:
        return jsonify({"ok": False, "error": "Unauthorized"})
    deposit = db_fetchone("SELECT * FROM deposits WHERE id = %s AND chat_id = %s AND status = 'pending'", 
                         (deposit_id, chat_id))
    if not deposit:
        return jsonify({"ok": False, "error": "Invalid or already processed deposit"})
    gu = get_game_user(chat_id)
    coins = int(gu.get("coins") or 0) + amount
    db_execute("UPDATE deposits SET status = 'approved' WHERE id = %s", (deposit_id,))
    update_game_user_fields(chat_id, {"coins": coins})
    db_execute("UPDATE users SET payment_status = 'registered' WHERE chat_id = %s", (chat_id,))
    try:
        asyncio.run(bot.send_message(
            chat_id=chat_id,
            text=f"Your deposit of {amount} Naira has been approved! Balance updated."
        ))
    except Exception as e:
        log.error(f"Failed to notify user {chat_id} for deposit approval: {e}")
    return jsonify({"ok": True})

@flask_app.post("/api/deposit/reject")
def api_deposit_reject():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    deposit_id = data.get("deposit_id", 0)
    chat_id = data.get("chat_id", 0)
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user") or int(auth["user"]["id"]) != ADMIN_ID:
        return jsonify({"ok": False, "error": "Unauthorized"})
    deposit = db_fetchone("SELECT * FROM deposits WHERE id = %s AND chat_id = %s AND status = 'pending'", 
                         (deposit_id, chat_id))
    if not deposit:
        return jsonify({"ok": False, "error": "Invalid or already processed deposit"})
    db_execute("UPDATE deposits SET status = 'rejected' WHERE id = %s", (deposit_id,))
    try:
        asyncio.run(bot.send_message(
            chat_id=chat_id,
            text="Your deposit request was rejected. Please contact support."
        ))
    except Exception as e:
        log.error(f"Failed to notify user {chat_id} for deposit rejection: {e}")
    return jsonify({"ok": True})

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
    match = matches[0]  # Take first match
    update_game_user_fields(chat_id, {"coins": coins - 500})
    prediction = generate_prediction(match["id"])
    return jsonify({"ok": True, "prediction": prediction})

# --- Bot Username Setup ---
BOT_USERNAME = ""

async def set_bot_username():
    global BOT_USERNAME
    try:
        me = await bot.get_me()
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

def _resolve_user_from_init(init_data: str) -> tuple[bool, dict | None, str]:
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return False, None, "Invalid auth"
    tg_user = auth["user"]
    chat_id = int(tg_user.get("id"))
    username = tg_user.get("username")
    upsert_user_if_missing(chat_id, username)
    return True, {"chat_id": chat_id, "username": username}, ""

async def main():
    try:
        db_init()
        await set_bot_username()
        print_checklist()
        log.info("Starting Flask via uvicorn on 0.0.0.0:%s", PORT)
        config = uvicorn.Config(
            WSGIMiddleware(flask_app),
            host="0.0.0.0",
            port=PORT,
            log_level="info",
            lifespan="off"
        )
        server = uvicorn.Server(config)
        await server.serve()
    except Exception as e:
        log.error("Startup failed: %s", e)
        sys.exit(1)

def print_checklist():
    print("=== Tapify Startup Checklist ===")
    print(f"BOT_TOKEN: {'OK' if BOT_TOKEN else 'MISSING'}")
    print(f"ADMIN_ID:  {ADMIN_ID if ADMIN_ID else 'MISSING'}")
    print(f"DB:        {'Postgres' if DATABASE_URL else 'MISSING'}")
    print(f"WEBAPP:    {WEBAPP_URL or '(derive from host)'}")
    print(f"BANK_ACCOUNTS: {'OK' if BANK_ACCOUNTS else 'MISSING'}")
    print(f"FOOTBALL_API_KEY: {'OK' if FOOTBALL_API_KEY else 'MISSING'}")
    print("================================")

if __name__ == "__main__":
    asyncio.run(main())
