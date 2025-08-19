#!/usr/bin/env python3
# app.py — Notcoin-style Telegram Mini App for Render deployment
# Requirements:
#   pip install flask python-telegram-bot psycopg[binary] python-dotenv uvicorn
#
# Start:
#   python app.py
#
# Environment (.env):
#   BOT_TOKEN=7645079949:AAEkgyy1GTzXXy45LtouLVRaLIGM4g_3WyM
#   ADMIN_ID=5646269450
#   WEBAPP_URL=https://tapify.onrender.com/app
#   DATABASE_URL=postgres://user:pass@host:5432/dbname   # optional; if absent -> SQLite
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
from urllib.parse import urlparse, parse_qsl, quote_plus
from datetime import datetime, timedelta, timezone, date
from collections import deque, defaultdict
from flask import Flask, request, jsonify, Response
import uvicorn
from uvicorn.middleware.wsgi import WSGIMiddleware

# Optional dotenv
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

AI_BOOST_LINK = os.getenv("AI_BOOST_LINK", "#")
DAILY_TASK_LINK = os.getenv("DAILY_TASK_LINK", "#")
GROUP_LINK = os.getenv("GROUP_LINK", "#")
SITE_LINK = os.getenv("SITE_LINK", "#")

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN is required in environment (.env).", file=sys.stderr)
    sys.exit(1)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("tapify")

# --- Database Layer (psycopg v3 if DATABASE_URL else SQLite) ------------------

USE_POSTGRES = False
psycopg = None
conn = None  # type: ignore

import sqlite3

def _connect_sqlite():
    db = sqlite3.connect("tapify.db", check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    return db

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
    return psycopg.connect(url, row_factory=dict_row)

def db_execute(query: str, params: t.Tuple = ()):
    if USE_POSTGRES:
        with conn.cursor() as cur:
            cur.execute(query, params)
    else:
        conn.execute(query, params)

def db_fetchone(query: str, params: t.Tuple = ()):
    if USE_POSTGRES:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()
    else:
        cur = conn.execute(query, params)
        row = cur.fetchone()
        return dict(row) if row else None

def db_fetchall(query: str, params: t.Tuple = ()):
    if USE_POSTGRES:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()
    else:
        cur = conn.execute(query, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]

def db_init():
    if USE_POSTGRES:
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
    else:
        db_execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            payment_status TEXT DEFAULT NULL,
            invites INTEGER DEFAULT 0
        );
        """)
        db_execute("""
        CREATE TABLE IF NOT EXISTS game_users (
            chat_id INTEGER PRIMARY KEY,
            coins INTEGER DEFAULT 0,
            energy INTEGER DEFAULT 500,
            max_energy INTEGER DEFAULT 500,
            energy_updated_at TEXT,
            multitap_until TEXT,
            autotap_until TEXT,
            regen_rate_seconds INTEGER DEFAULT 3,
            last_tap_at TEXT,
            daily_streak INTEGER DEFAULT 0,
            last_streak_at TEXT,
            last_daily_reward TEXT
        );
        """)
        db_execute("""
        CREATE TABLE IF NOT EXISTS game_taps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            delta INTEGER NOT NULL,
            nonce TEXT NOT NULL
        );
        """)
        db_execute("""
        CREATE TABLE IF NOT EXISTS game_referrals (
            referrer INTEGER NOT NULL,
            referee INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (referrer, referee)
        );
        """)

def db_now() -> datetime:
    return datetime.now(timezone.utc)

def db_date_utc() -> date:
    return db_now().date()

def upsert_user_if_missing(chat_id: int, username: str | None):
    existing = db_fetchone("SELECT chat_id FROM users WHERE chat_id = %s" if USE_POSTGRES else "SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,))
    if not existing:
        db_execute("INSERT INTO users (chat_id, username, payment_status, invites) VALUES (%s,%s,%s,%s)" if USE_POSTGRES else "INSERT INTO users (chat_id, username, payment_status, invites) VALUES (?,?,?,?)",
                   (chat_id, username, None, 0))
    existing_g = db_fetchone("SELECT chat_id FROM game_users WHERE chat_id = %s" if USE_POSTGRES else "SELECT chat_id FROM game_users WHERE chat_id = ?", (chat_id,))
    if not existing_g:
        now = db_now()
        db_execute("""INSERT INTO game_users
            (chat_id, coins, energy, max_energy, energy_updated_at, regen_rate_seconds, daily_streak, last_daily_reward)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""" if USE_POSTGRES else """INSERT INTO game_users
            (chat_id, coins, energy, max_energy, energy_updated_at, regen_rate_seconds, daily_streak, last_daily_reward)
            VALUES (?,?,?,?,?,?,?,?)""",
            (chat_id, 0, 500, 500, now if USE_POSTGRES else now.isoformat(), 3, 0, None))

def is_registered(chat_id: int) -> bool:
    row = db_fetchone("SELECT payment_status FROM users WHERE chat_id = %s" if USE_POSTGRES else "SELECT payment_status FROM users WHERE chat_id = ?", (chat_id,))
    if not row:
        return False
    return (row.get("payment_status") or "").lower() == "registered"

def add_referral_if_absent(referrer: int, referee: int):
    if referrer == referee or referee <= 0:
        return
    r = db_fetchone("SELECT 1 FROM game_referrals WHERE referrer=%s AND referee=%s" if USE_POSTGRES else "SELECT 1 FROM game_referrals WHERE referrer=? AND referee=?", (referrer, referee))
    if r:
        return
    now = db_now()
    db_execute("INSERT INTO game_referrals (referrer, referee, created_at) VALUES (%s,%s,%s)" if USE_POSTGRES else "INSERT INTO game_referrals (referrer, referee, created_at) VALUES (?,?,?)",
               (referrer, referee, now if USE_POSTGRES else now.isoformat()))
    db_execute("UPDATE users SET invites=COALESCE(invites,0)+1 WHERE chat_id=%s" if USE_POSTGRES else "UPDATE users SET invites=COALESCE(invites,0)+1 WHERE chat_id=?", (referrer,))

def get_game_user(chat_id: int) -> dict:
    row = db_fetchone("SELECT * FROM game_users WHERE chat_id = %s" if USE_POSTGRES else "SELECT * FROM game_users WHERE chat_id = ?", (chat_id,))
    if not row:
        upsert_user_if_missing(chat_id, None)
        row = db_fetchone("SELECT * FROM game_users WHERE chat_id = %s" if USE_POSTGRES else "SELECT * FROM game_users WHERE chat_id = ?", (chat_id,))
    return row or {}

def update_game_user_fields(chat_id: int, fields: dict):
    keys = list(fields.keys())
    if not keys:
        return
    set_clause = ", ".join(f"{k}=%s" if USE_POSTGRES else f"{k}=?" for k in keys)
    params = tuple(fields[k] for k in keys) + (chat_id,)
    db_execute(f"UPDATE game_users SET {set_clause} WHERE chat_id={'%s' if USE_POSTGRES else '?'}", params)

def add_tap(chat_id: int, delta: int, nonce: str):
    now = db_now()
    db_execute("INSERT INTO game_taps (chat_id, ts, delta, nonce) VALUES (%s,%s,%s,%s)" if USE_POSTGRES else "INSERT INTO game_taps (chat_id, ts, delta, nonce) VALUES (?,?,?,?)",
               (chat_id, now if USE_POSTGRES else now.isoformat(), delta, nonce))
    db_execute("UPDATE game_users SET coins=COALESCE(coins,0)+%s, last_tap_at=%s WHERE chat_id=%s" if USE_POSTGRES else "UPDATE game_users SET coins=COALESCE(coins,0)+?, last_tap_at=? WHERE chat_id=?",
               (delta, now if USE_POSTGRES else now.isoformat(), chat_id))

def leaderboard(range_: str = "all", limit: int = 50):
    if range_ == "all":
        q = "SELECT u.username, g.chat_id, g.coins AS score FROM game_users g LEFT JOIN users u ON u.chat_id=g.chat_id ORDER BY score DESC LIMIT %s" if USE_POSTGRES else "SELECT u.username, g.chat_id, g.coins AS score FROM game_users g LEFT JOIN users u ON u.chat_id=g.chat_id ORDER BY score DESC LIMIT ?"
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
        """ if USE_POSTGRES else """
            SELECT u.username, t.chat_id, COALESCE(SUM(t.delta),0) AS score
            FROM game_taps t
            LEFT JOIN users u ON u.chat_id=t.chat_id
            WHERE t.ts >= ?
            GROUP BY t.chat_id, u.username
            ORDER BY score DESC
            LIMIT ?
        """
        return db_fetchall(q, (since if USE_POSTGRES else since.isoformat(), limit))

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
        field: until if USE_POSTGRES else until.isoformat()
    })
    return True, f"{boost} activated!"

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
    .boostBtn {
      transition: background-color 0.3s ease, transform 0.2s ease;
    }
    .boostBtn:hover {
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
      <div class="mt-8 grid grid-cols-4 gap-2 text-center text-sm">
        <button class="tab active py-2" data-tab="play">Play</button>
        <button class="tab py-2" data-tab="boosts">Boosts</button>
        <button class="tab py-2" data-tab="board">Leaderboard</button>
        <button class="tab py-2" data-tab="refer">Refer</button>
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
    </div>
  </div>
<script>
const tg = window.Telegram?.WebApp;
if (tg) tg.expand();
const $ = (q) => document.querySelector(q);
const $$ = (q) => Array.from(document.querySelectorAll(q));
function setTab(name) {
  $$(".tab").forEach(b => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  $("#panelPlay").classList.toggle("hidden", name !== "play");
  $("#panelBoosts").classList.toggle("hidden", name !== "boosts");
  $("#panelBoard").classList.toggle("hidden", name !== "board");
  $("#panelRefer").classList.toggle("hidden", name !== "refer");
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
    update_game_user_fields(chat_id, {"energy": energy, "energy_updated_at": energy_ts if USE_POSTGRES else energy_ts.isoformat()})
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
    update_game_user_fields(chat_id, {"energy": energy - 1, "energy_updated_at": energy_ts if USE_POSTGRES else energy_ts.isoformat()})
    new_streak, streak_date = streak_update(gu, tapped_today=True)
    update_game_user_fields(chat_id, {"daily_streak": new_streak, "last_streak_at": streak_date if USE_POSTGRES else streak_date.isoformat()})
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
    update_game_user_fields(chat_id, {"coins": coins, "last_daily_reward": today if USE_POSTGRES else today.isoformat()})
    return jsonify({"ok": True, "coins": coins})

@flask_app.get("/api/leaderboard")
def api_leaderboard():
    rng = request.args.get("range", "all")
    if rng not in ("day", "week", "all"):
        rng = "all"
    items = leaderboard(rng, 50)
    return jsonify({"ok": True, "items": items})

from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BOT_USERNAME = ""

def deep_link_ref(chat_id: int) -> str:
    if not BOT_USERNAME:
        return ""
    return f"https://t.me/{BOT_USERNAME}?start=ref_{chat_id}"

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = user.id
    username = user.username or ""
    upsert_user_if_missing(chat_id, username)
    args = context.args or []
    if args and len(args) >= 1 and args[0].startswith("ref_"):
        try:
            referrer = int(args[0][4:])
            add_referral_if_absent(referrer, chat_id)
        except Exception:
            pass
    kb = [[KeyboardButton(text="Open Game", web_app=WebAppInfo(url=WEBAPP_URL))]]
    text = (
        "Welcome to <b>Tapify</b>!\n\n"
        "Tap to earn coins, activate boosts, climb the leaderboard.\n"
        "If you haven't completed registration yet, please do so in the bot.\n\n"
        f"<i>WebApp:</i> {WEBAPP_URL}"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )

async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_user.id
    gu = get_game_user(chat_id)
    energy, _ = compute_energy(gu)
    text = (
        f"👤 <b>You</b>\n"
        f"Coins: <b>{int(gu.get('coins') or 0)}</b>\n"
        f"Energy: <b>{energy}/{int(gu.get('max_energy') or 500)}</b>\n"
        f"Streak: <b>{int(gu.get('daily_streak') or 0)}</b>\n"
        f"Referral link: {deep_link_ref(chat_id)}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    msg = " ".join(context.args)
    rows = db_fetchall("SELECT chat_id FROM game_users", ())
    sent = 0
    for r in rows:
        try:
            await context.bot.send_message(chat_id=r["chat_id"], text=msg)
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"Broadcast sent to {sent} players.")

async def cmd_setcoins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setcoins <chat_id> <amount>")
        return
    try:
        cid = int(context.args[0]); amount = int(context.args[1])
    except Exception:
        await update.message.reply_text("Invalid numbers.")
        return
    update_game_user_fields(cid, {"coins": amount})
    await update.message.reply_text("OK")

async def cmd_addcoins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addcoins <chat_id> <delta>")
        return
    try:
        cid = int(context.args[0]); delta = int(context.args[1])
    except Exception:
        await update.message.reply_text("Invalid numbers.")
        return
    gu = get_game_user(cid)
    coins = int(gu.get("coins") or 0) + delta
    update_game_user_fields(cid, {"coins": coins})
    await update.message.reply_text("OK")

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = leaderboard("all", 10)
    lines = []
    for i, r in enumerate(top, start=1):
        lines.append(f"{i}. @{r.get('username') or r.get('chat_id')} — {r.get('score')}")
    await update.message.reply_text("🏆 Top 10 (all time)\n" + "\n".join(lines))

async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Open the game here:\n{WEBAPP_URL}")

def _is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID

def _resolve_user_from_init(init_data: str) -> tuple[bool, dict | None, str]:
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return False, None, "Invalid auth"
    tg_user = auth["user"]
    chat_id = int(tg_user.get("id"))
    username = tg_user.get("username")
    upsert_user_if_missing(chat_id, username)
    return True, {"chat_id": chat_id, "username": username}, ""

async def start_bot():
    global BOT_USERNAME
    application = Application.builder().token(BOT_TOKEN).build()
    me = await application.bot.get_me()
    BOT_USERNAME = me.username
    log.info("Bot username: @%s", BOT_USERNAME)

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("me", cmd_me))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    application.add_handler(CommandHandler("setcoins", cmd_setcoins))
    application.add_handler(CommandHandler("addcoins", cmd_addcoins))
    application.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

    log.info("Starting bot polling...")
    await application.initialize()
    await application.start()
    # Run polling manually to avoid starting a new event loop
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    # Keep the bot running until stopped
    try:
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()

async def start_flask():
    log.info("Starting Flask via uvicorn on 0.0.0.0:%s", PORT)
    config = uvicorn.Config(
        WSGIMiddleware(flask_app),
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        lifespan="off"  # Disable ASGI lifespan events
    )
    server = uvicorn.Server(config)
    await server.serve()

def print_checklist():
    print("=== Tapify Startup Checklist ===")
    print(f"BOT_TOKEN: {'OK' if BOT_TOKEN else 'MISSING'}")
    print(f"ADMIN_ID:  {ADMIN_ID if ADMIN_ID else 'MISSING'}")
    print(f"DB:        {'Postgres' if USE_POSTGRES else 'SQLite'}")
    print(f"WEBAPP:    {WEBAPP_URL or '(derive from host)'}")
    print("================================")

async def main():
    global conn, USE_POSTGRES
    try:
        if DATABASE_URL:
            conn_pg = _connect_postgres(DATABASE_URL)
            conn_pg.autocommit = True
            conn = conn_pg
            USE_POSTGRES = True
            log.info("Connected to Postgres")
        else:
            conn = _connect_sqlite()
            log.info("Connected to SQLite")
    except Exception as e:
        log.error("Database connection failed: %s", e)
        sys.exit(1)

    db_init()
    print_checklist()

    # Run Flask and Telegram bot concurrently in the same event loop
    await asyncio.gather(start_flask(), start_bot())

if __name__ == "__main__":
    asyncio.run(main())
