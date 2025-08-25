import os
import json
import hmac
import hashlib
import random
import requests
from datetime import datetime, date, timezone
from decimal import Decimal, ROUND_DOWN, getcontext

from flask import Flask, request, jsonify, render_template_string, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

# =========================
# Config / Constants
# =========================
getcontext().prec = 28

# Required env vars (set these on Render/hosting)
DATABASE_URL         = os.environ["DATABASE_URL"]
SECRET_KEY           = os.environ.get("SECRET_KEY", "dev-secret")
PAYSTACK_SECRET_KEY  = os.environ.get("PAYSTACK_SECRET_KEY")   # live/test secret key
ADMIN_TOKEN          = os.environ.get("ADMIN_TOKEN", "change-me")  # for admin endpoints

# Money model: $1 == ₦1000 (project convention)
USD_TO_NGN = Decimal("1000")

# Tap
TAP_REWARD = Decimal("0.001")     # $ per tap
MAX_TAP_PER_REQUEST = 50

# Walk upgrades (level -> {rate per step in USD, price in USD})
# Base: 1000 steps = $1 ⇒ rate = $0.001/step
WALK_UPGRADES = {
    1: {"rate": Decimal("0.001"), "price": Decimal("0.00")},
    2: {"rate": Decimal("0.002"), "price": Decimal("5.00")},
    3: {"rate": Decimal("0.005"), "price": Decimal("15.00")},
    4: {"rate": Decimal("0.010"), "price": Decimal("40.00")},
}

# Aviator (diagnosis: MIN_BET is very high vs UI)
AVIATOR_GROWTH_PER_SEC = Decimal("0.25")
MIN_BET = Decimal("1000.00")       # USD  (see diagnosis note)
MAX_BET = Decimal("1000000.00")    # USD

# Wallet
MIN_DEPOSIT_NGN = Decimal("100.00")   # ₦ minimum deposit
MIN_WITHDRAW_USD = Decimal("50.00")   # $ minimum withdrawal

# Walk daily cap (USD)
WALK_DAILY_CAP_USD = Decimal("5.00")

# =========================
# Utils
# =========================
def now_utc():
    return datetime.now(timezone.utc)

def to_cents(x: Decimal) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

def sample_crash_multiplier() -> Decimal:
    """Heavy-tailed crash multiplier."""
    r = random.random()
    if r < 0.80:
        return Decimal(str(round(random.uniform(1.10, 3.0), 2)))
    elif r < 0.98:
        return Decimal(str(round(random.uniform(3.0, 10.0), 2)))
    else:
        return Decimal(str(round(random.uniform(10.0, 50.0), 2)))

def paystack_signature_valid(raw_body: bytes, signature: str) -> bool:
    if not PAYSTACK_SECRET_KEY or not signature:
        return False
    digest = hmac.new(
        PAYSTACK_SECRET_KEY.encode("utf-8"),
        raw_body,
        hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(digest, signature)

def require_admin():
    token = request.headers.get("X-Admin-Token") or request.args.get("admin_token")
    if not token or token != ADMIN_TOKEN:
        abort(403, "Admin token required")

# =========================
# App & DB
# =========================
app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SQLALCHEMY_DATABASE_URI=DATABASE_URL,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
)

db = SQLAlchemy(app)

# =========================
# Models
# =========================
class User(db.Model):
    __tablename__ = "users"
    chat_id = db.Column(db.BigInteger, primary_key=True)   # Telegram chat_id
    username = db.Column(db.String(128))

    balance_usd = db.Column(db.Numeric(18, 2), default=Decimal("0.00"))
    balance_ngn = db.Column(db.Numeric(18, 2), default=Decimal("0.00"))

    walk_level = db.Column(db.Integer, default=1)
    walk_rate  = db.Column(db.Numeric(18, 4), default=Decimal("0.001"))
    total_steps = db.Column(db.Integer, default=0)

    # Track daily cap usage
    steps_usd_today = db.Column(db.Numeric(18, 2), default=Decimal("0.00"))
    steps_day = db.Column(db.Date, default=date.today)

class Transaction(db.Model):
    __tablename__ = "transactions"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    chat_id = db.Column(db.BigInteger, index=True, nullable=False)
    type = db.Column(db.String(64), nullable=False)
    amount_usd = db.Column(db.Numeric(18, 2), default=Decimal("0.00"))
    amount_ngn = db.Column(db.Numeric(18, 2), default=Decimal("0.00"))
    status = db.Column(db.String(32), default="pending")
    created_at = db.Column(db.DateTime, default=now_utc)
    meta_json = db.Column(db.Text, default="{}")

# =========================
# Helpers
# =========================
def get_or_create_user_from_query():
    chat_id = request.args.get("chat_id")
    username = request.args.get("username", "")[:128]
    if not chat_id:
        abort(400, "chat_id required")
    chat_id = int(chat_id)
    user = User.query.get(chat_id)
    if not user:
        user = User(chat_id=chat_id, username=username)
        db.session.add(user)
        db.session.commit()
    else:
        if username and user.username != username:
            user.username = username
            db.session.commit()
    # reset day if needed
    if user.steps_day != date.today():
        user.steps_day = date.today()
        user.steps_usd_today = Decimal("0.00")
        db.session.commit()
    return user

def add_tx(user: User, typ: str, usd: Decimal, ngn: Decimal, status="pending", meta=None):
    meta = meta or {}
    t = Transaction(
        chat_id=user.chat_id,
        type=typ,
        amount_usd=to_cents(usd),
        amount_ngn=to_cents(ngn),
        status=status,
        meta_json=json.dumps(meta),
    )
    db.session.add(t)
    # apply to balances if approved
    if status == "approved":
        user.balance_usd = to_cents(Decimal(user.balance_usd) + to_cents(usd))
        user.balance_ngn = to_cents(Decimal(user.balance_ngn) + to_cents(ngn))
    db.session.commit()
    return t

# =========================
# Base HTML (with your background + coin + mobile lock)
# =========================
BASE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<!-- Lock zoom for Telegram WebApp: width=device-width, maximum-scale=1, user-scalable=no -->
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no" />
<title>Tapify</title>
<script>
  // In Telegram WebApp, these can be injected; fallback to query params for testing
  const CHAT_ID = new URLSearchParams(location.search).get('chat_id') || '';
  const USERNAME = new URLSearchParams(location.search).get('username') || '';
</script>
<style>
  :root{
    --bg-red:#8b0000;
    --glass: rgba(255,255,255,.08);
    --glass-b: rgba(255,255,255,.14);
  }
  *{box-sizing:border-box}
  html, body { height:100%; }
  body{
    margin:0;
    font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, "Noto Sans", "Apple Color Emoji";
    color:#fff;
    overflow-x:hidden;
    -webkit-tap-highlight-color: transparent;
  }
  /* === Glass cards === */
  .glass{ background:var(--glass); backdrop-filter: blur(10px); border:1px solid var(--glass-b); }
  .card{ border-radius:16px; box-shadow: 0 10px 30px rgba(0,0,0,.25); }
  .container{ max-width:720px; margin: 0 auto; padding:16px; display: grid; gap:16px; }

  /* Header / nav */
  header{ display:flex; align-items:center; justify-content:space-between; gap:12px; }
  .btn{ border-radius:12px; padding:10px 12px; border:1px solid rgba(255,255,255,.12); }
  .hidden{ display:none; }

  /* Energy bar */
  .energy-wrap{ height:14px; background:rgba(255,255,255,.15); border-radius:999px; overflow:hidden; border:1px solid rgba(255,255,255,.2); position:relative; }
  .energy-fill{ height:100%; width:100%; background:linear-gradient(90deg,#22c55e,#84cc16); transition:width .2s ease; }
  .energy-gloss{ position:absolute; inset:0; background:linear-gradient(180deg,rgba(255,255,255,.35),rgba(255,255,255,0)); mix-blend:soft-light; pointer-events:none; }

  /* Coin button wrapper + animation */
  .coin{ border:0; background:transparent; padding:0; width:220px; height:220px; cursor:pointer; position:relative; outline:none; }
  .coin svg{ width:100%; height:100%; display:block; filter: drop-shadow(0 12px 16px rgba(0,0,0,.35)); }
  .bounce{ animation: b .24s ease; }
  @keyframes b{ 0%{ transform:translateY(0) scale(1) } 50%{ transform: translateY(2px) scale(.97) } 100%{ transform:translateY(0) scale(1) } }

  /* Floating +N */
  .floatText{
    position:fixed; pointer-events:none; font-weight:700; color:#fff; text-shadow:0 2px 6px rgba(0,0,0,.45);
    animation: floatUp .8s ease forwards;
  }
  @keyframes floatUp{
    0%{ opacity:0; transform:translateY(10px) scale(.9) }
    10%{ opacity:1 }
    100%{ opacity:0; transform:translateY(-26px) scale(1.05) }
  }

  /* Background SVG holder (yours) */
  .bg-waves{ position: fixed; inset: 0; z-index: -1; pointer-events: none; }

  /* Simple utilities */
  .grid4{ display:grid; grid-template-columns: repeat(4, 1fr); gap:8px; }
  .center{ display:flex; justify-content:center; }
  .space-y-2 > * + *{ margin-top:8px; }
  .space-y-4 > * + *{ margin-top:16px; }
  .space-y-5 > * + *{ margin-top:20px; }
  .font-semibold{ font-weight:600; }
  .text-sm{ font-size:.9rem; }
  .text-xs{ font-size:.78rem; }
  .w-56{ width:14rem; }
  .p-2{ padding:8px; }
  .p-3{ padding:12px; }
  .p-5{ padding:20px; }
</style>
</head>
<body>

<!-- === BACKGROUND: Silky Red Waves (from your spec) === -->
<svg class="bg-waves" viewBox="0 0 1440 1024" preserveAspectRatio="xMidYMid slice">
  <defs>
    <radialGradient id="base" cx="50%" cy="35%" r="85%">
      <stop offset="0%"  stop-color="#ff2b2b"/>
      <stop offset="45%" stop-color="#d10d0d"/>
      <stop offset="100%" stop-color="#6f0000"/>
    </radialGradient>
    <linearGradient id="hl" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%"  stop-color="rgba(255,120,120,0.95)"/>
      <stop offset="60%" stop-color="rgba(255,80,80,0.35)"/>
      <stop offset="100%" stop-color="rgba(255,80,80,0)"/>
    </linearGradient>
    <linearGradient id="shade" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%"   stop-color="rgba(120,0,0,0.65)"/>
      <stop offset="100%" stop-color="rgba(80,0,0,0.25)"/>
    </linearGradient>
    <filter id="blurLg"><feGaussianBlur stdDeviation="20"/></filter>
    <filter id="blurMd"><feGaussianBlur stdDeviation="10"/></filter>
    <filter id="blurSm"><feGaussianBlur stdDeviation="5"/></filter>
  </defs>
  <rect x="0" y="0" width="1440" height="1024" fill="url(#base)"/>
  <g opacity=".55">
    <path fill="url(#shade)" d="M0,150 C300,60 600,120 900,170 C1200,220 1320,260 1440,240 L1440,0 L0,0 Z"/>
    <path fill="url(#shade)" d="M0,330 C280,420 520,380 780,420 C1040,460 1260,560 1440,520 L1440,1024 L0,1024 Z"/>
    <path fill="url(#shade)" d="M0,690 C260,640 560,710 860,760 C1120,805 1300,840 1440,820 L1440,1024 L0,1024 Z"/>
  </g>
  <g opacity=".9">
    <path fill="url(#hl)" filter="url(#blurMd)" d="M0,210 C240,120 560,160 840,210 C1120,260 1310,300 1440,260 L1440,420 L0,420 Z"/>
    <path fill="url(#hl)" filter="url(#blurSm)" opacity=".75" d="M0,460 C220,520 540,520 820,560 C1100,600 1310,650 1440,630 L1440,760 L0,760 Z"/>
    <path fill="url(#hl)" filter="url(#blurMd)" opacity=".65" d="M0,600 C200,560 520,590 760,640 C1000,690 1250,730 1440,710 L1440,850 L0,850 Z"/>
  </g>
  <rect x="0" y="0" width="1440" height="380" fill="url(#hl)" filter="url(#blurLg)" opacity=".55"/>
</svg>

<div class="container">
  <!-- Header -->
  <header class="glass card p-3">
    <div>
      <div class="font-semibold">Tapify</div>
      <div class="text-xs" id="walk_rate">$0.000</div>
    </div>
    <div style="text-align:right">
      <div id="usd" class="font-semibold">$0.00</div>
      <div id="ngn" class="text-xs" style="opacity:.85">₦0.00</div>
    </div>
  </header>

  <!-- Nav -->
  <nav class="glass card p-2 grid4">
    <button id="tab_tap" class="btn text-sm" style="background:rgba(255,255,255,.15)">Tap</button>
    <button id="tab_aviator" class="btn text-sm">Aviator</button>
    <button id="tab_walk" class="btn text-sm">Walk</button>
    <button id="tab_wallet" class="btn text-sm">Wallet</button>
  </nav>

  <!-- Tap Panel -->
  <section id="panel_tap" class="glass card p-5">
    <div class="space-y-5" style="text-align:center">
      <div class="text-sm" style="opacity:.85">
        Tap the coin to earn <span class="font-semibold">${{tap_reward}}</span> per tap
      </div>

      <!-- Energy -->
      <div class="center" style="gap:12px">
        <div class="w-56 energy-wrap">
          <div id="energy_fill" class="energy-fill"></div>
          <div class="energy-gloss"></div>
        </div>
        <div id="energy_label" class="text-xs" style="width:4rem; text-align:left; opacity:.85">100/100</div>
      </div>

      <!-- Coin -->
      <div class="center">
        <button id="tap_coin" class="coin" aria-label="Tap to earn">
          <!-- NotCoin Engraved SVG (inside the button) -->
          <svg viewBox="0 0 500 500" aria-hidden="true">
            <defs>
              <radialGradient id="coinGradient" cx="50%" cy="50%" r="70%">
                <stop offset="0%" stop-color="#fff7cc"/>
                <stop offset="40%" stop-color="#f2c94c"/>
                <stop offset="100%" stop-color="#b8860b"/>
              </radialGradient>
              <radialGradient id="rimGradient" cx="50%" cy="50%" r="75%">
                <stop offset="0%" stop-color="rgba(255,255,255,0.25)"/>
                <stop offset="100%" stop-color="rgba(255,255,255,0)"/>
              </radialGradient>
              <mask id="ring-mask">
                <rect x="0" y="0" width="500" height="500" fill="white"/>
                <circle cx="250" cy="250" r="170" fill="black"/>
              </mask>
              <linearGradient id="engraveFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stop-color="#b8860b"/>
                <stop offset="100%" stop-color="#8b6b10"/>
              </linearGradient>
              <filter id="engrave">
                <feDropShadow dx="0" dy="0" stdDeviation="2" flood-color="#000" flood-opacity=".35"/>
              </filter>
            </defs>

            <!-- Coin base -->
            <circle cx="250" cy="250" r="220" fill="url(#coinGradient)" stroke="#d4af37" stroke-width="12"/>
            <!-- Rim shine overlay -->
            <circle cx="250" cy="250" r="220" fill="url(#rimGradient)"/>
            <!-- Shutter blades engraved -->
            <g mask="url(#ring-mask)" transform="rotate(22 250 250)" filter="url(#engrave)">
              <g fill="url(#engraveFill)">
                <rect x="238" y="50" width="24" height="400" rx="12" transform="rotate(0 250 250)"/>
                <rect x="238" y="50" width="24" height="400" rx="12" transform="rotate(45 250 250)"/>
                <rect x="238" y="50" width="24" height="400" rx="12" transform="rotate(90 250 250)"/>
                <rect x="238" y="50" width="24" height="400" rx="12" transform="rotate(135 250 250)"/>
                <rect x="238" y="50" width="24" height="400" rx="12" transform="rotate(180 250 250)"/>
                <rect x="238" y="50" width="24" height="400" rx="12" transform="rotate(225 250 250)"/>
                <rect x="238" y="50" width="24" height="400" rx="12" transform="rotate(270 250 250)"/>
                <rect x="238" y="50" width="24" height="400" rx="12" transform="rotate(315 250 250)"/>
              </g>
            </g>
            <!-- Gloss highlight -->
            <ellipse cx="200" cy="170" rx="120" ry="60" fill="white" fill-opacity="0.15"/>
          </svg>
        </button>
      </div>

      <div class="text-xs" style="opacity:.8">Batching enabled (auto-sends). Max {{max_tap}} per request.</div>

      <details class="glass" style="border-radius:12px; padding:12px; text-align:left">
        <summary class="font-semibold">Boosts & Tips</summary>
        <div class="text-sm" style="margin-top:8px; opacity:.85">
          <div>• Tap Strength scales with <span class="font-semibold">Walk Level</span> (Lvl 1–2 ➜ x1, Lvl 3–4 ➜ x10).</div>
          <div>• Energy refills over time; stronger taps consume more energy.</div>
          <div>• Upgrade Walk to indirectly unlock stronger taps.</div>
        </div>
      </details>
    </div>
  </section>

  <!-- Aviator Panel -->
  <section id="panel_aviator" class="glass card p-5 hidden">
    <div class="space-y-4">
      <div>
        <h2 class="font-semibold" style="font-size:1.1rem">Aviator</h2>
        <p class="text-sm" style="opacity:.85">Bet, watch the multiplier grow, and cash out before it crashes.</p>
      </div>
      <div class="grid4">
        <input id="bet_input" class="glass p-2" type="number" step="0.01" min="0.10" placeholder="Bet (USD)"/>
        <button id="bet_btn" class="btn">Bet</button>
        <div id="mult_text" class="btn" style="text-align:center">1.00×</div>
        <button id="cashout_btn" class="btn" disabled>Cash out</button>
      </div>
      <div id="status_text" class="text-sm" style="opacity:.85">Waiting…</div>
      <div id="aviator_hist" class="glass card p-2" style="display:flex; gap:6px; flex-wrap:wrap;"></div>
      <div id="aviator_players" class="glass card p-2"></div>
    </div>
  </section>

  <!-- Walk Panel -->
  <section id="panel_walk" class="glass card p-5 hidden">
    <div class="space-y-4">
      <div class="font-semibold">Walk</div>
      <div class="text-sm" style="opacity:.85">Simulate steps to earn at your current rate.</div>
      <div class="grid4">
        <input id="steps_input" class="glass p-2" type="number" min="1" step="1" placeholder="Steps"/>
        <button id="steps_btn" class="btn">Add steps</button>
        <input id="upgrade_level" class="glass p-2" type="number" min="1" max="4" step="1" placeholder="Target level"/>
        <button id="upgrade_btn" class="btn">Upgrade</button>
      </div>
      <div id="walk_result" class="text-sm" style="opacity:.85"></div>
      <div class="text-sm">Total steps: <span id="total_steps">0</span></div>
    </div>
  </section>

  <!-- Wallet Panel -->
  <section id="panel_wallet" class="glass card p-5 hidden">
    <div class="space-y-4">
      <div class="font-semibold">Wallet</div>
      <div class="grid4">
        <input id="dep_amount_ngn" class="glass p-2" type="number" min="100" step="100" placeholder="₦ Amount"/>
        <button id="dep_btn" class="btn">Deposit</button>
        <input id="wd_amount" class="glass p-2" type="number" min="50" step="0.01" placeholder="$ Withdraw"/>
        <input id="wd_payout" class="glass p-2" placeholder="Payout method"/>
      </div>
      <button id="wd_btn" class="btn">Request Withdraw</button>
      <div class="font-semibold">History</div>
      <div id="history_box" class="glass card p-2"></div>
    </div>
  </section>
</div>

<script>
/* ==== Guards ==== */
if (!window.Telegram && !CHAT_ID) {
  console.warn('Running outside Telegram; pass chat_id & username in the URL for testing.');
}

/* ==== Panels ==== */
const panels = {
  tap: document.getElementById('panel_tap'),
  aviator: document.getElementById('panel_aviator'),
  walk: document.getElementById('panel_walk'),
  wallet: document.getElementById('panel_wallet'),
};
function showPanel(name) {
  for (const k in panels) panels[k].classList.add('hidden');
  panels[name].classList.remove('hidden');
  document.getElementById('tab_tap').style.background = (name==='tap') ? 'rgba(255,255,255,.15)' : '';
  document.getElementById('tab_aviator').style.background = (name==='aviator') ? 'rgba(255,255,255,.15)' : '';
  document.getElementById('tab_walk').style.background = (name==='walk') ? 'rgba(255,255,255,.15)' : '';
  document.getElementById('tab_wallet').style.background = (name==='wallet') ? 'rgba(255,255,255,.15)' : '';
}
document.getElementById('tab_tap').onclick = () => showPanel('tap');
document.getElementById('tab_aviator').onclick = () => showPanel('aviator');
document.getElementById('tab_walk').onclick = () => showPanel('walk');
document.getElementById('tab_wallet').onclick = () => { showPanel('wallet'); loadHistory(); };

/* ==== UI elements ==== */
const usdEl = document.getElementById('usd');
const ngnEl = document.getElementById('ngn');
const walkRateEl = document.getElementById('walk_rate');
const totalStepsEl = document.getElementById('total_steps');

/* ==== Fetch user ==== */
async function fetchUser() {
  if (!CHAT_ID) return;
  const r = await fetch(`/api/user?chat_id=${encodeURIComponent(CHAT_ID)}&username=${encodeURIComponent(USERNAME)}`);
  const j = await r.json();
  if (j.balance_usd !== undefined) {
    usdEl.textContent = `$${j.balance_usd}`;
    ngnEl.textContent = `₦${j.balance_ngn}`;
    walkRateEl.textContent = `$${j.walk_rate}`;
    totalStepsEl.textContent = j.total_steps;
    // Refresh tap strength based on walk level:
    setTapStrengthFromLevel(j.walk_level);
  }
}

/* ==== TAP: energy, strength, floats, batching ==== */
const MAX_TAP = {{max_tap}};
const coin = document.getElementById('tap_coin');
const energyFill = document.getElementById('energy_fill');
const energyLabel = document.getElementById('energy_label');

let energyMax = 100;
let energy = energyMax;
/* Refill slowed down per your request */
let regenPerSecond = 2;   // slower refill so bar goes down with active tapping
let tapStrength = 1;      // default; boosted at higher levels

function setTapStrengthFromLevel(level){
  tapStrength = (level >= 3) ? 10 : 1; // Lvl 3–4 => stronger taps
}

function updateEnergyUI(){
  const pct = Math.max(0, Math.min(100, (energy/energyMax)*100));
  energyFill.style.width = pct + '%';
  energyLabel.textContent = Math.floor(energy) + '/' + energyMax;
}
updateEnergyUI();

// regen (4 ticks/sec)
setInterval(()=>{
  energy = Math.min(energyMax, energy + regenPerSecond/4);
  updateEnergyUI();
}, 250);

let tapCountBatch = 0;
function flushTaps() {
  if (tapCountBatch <= 0) return;
  const count = Math.min(MAX_TAP, tapCountBatch);
  tapCountBatch -= count;
  fetch(`/api/tap?chat_id=${encodeURIComponent(CHAT_ID)}`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ count })
  }).then(()=>fetchUser());
}
setInterval(flushTaps, 900);

/* Floating +N near tap location */
function spawnFloat(x, y, text) {
  const el = document.createElement('div');
  el.className = 'floatText';
  el.textContent = '+' + text;
  el.style.left = (x - 10) + 'px';
  el.style.top = (y - 10) + 'px';
  document.body.appendChild(el);
  setTimeout(()=>el.remove(), 820);
}

/* Coin click */
coin.addEventListener('click', (e)=>{
  if (energy < tapStrength) return;     // not enough energy
  energy -= tapStrength;
  updateEnergyUI();

  // bounce
  coin.classList.add('bounce');
  setTimeout(()=>coin.classList.remove('bounce'), 240);

  // floating text at click coords
  const rect = coin.getBoundingClientRect();
  const cx = rect.left + rect.width/2;
  const cy = rect.top + rect.height/2;
  spawnFloat(e.clientX || cx, e.clientY || cy, tapStrength);

  // enqueue taps
  tapCountBatch += tapStrength;
  if (tapCountBatch >= MAX_TAP) flushTaps();
});

/* ==== AVIATOR (UI glue; server rules unchanged) ==== */
let currentRoundId = null;
let aviatorTimer = null;
const betInput = document.getElementById('bet_input');
const betBtn = document.getElementById('bet_btn');
const multText = document.getElementById('mult_text');
const statusText = document.getElementById('status_text');
const cashoutBtn = document.getElementById('cashout_btn');
const histBox = document.getElementById('aviator_hist');
const playersBox = document.getElementById('aviator_players');

const lastResults = []; // local session history
function addHistory(mult){
  lastResults.unshift(parseFloat(mult));
  if (lastResults.length > 20) lastResults.pop();
  histBox.innerHTML = '';
  lastResults.forEach(v=>{
    const pill = document.createElement('span');
    pill.className = 'p-2';
    pill.style.borderRadius = '8px';
    pill.style.fontSize = '.78rem';
    pill.style.fontWeight = '700';
    pill.textContent = v + '×';
    if (v < 2){ pill.style.background = 'rgba(244,63,94,.3)'; pill.style.border='1px solid rgba(255,255,255,.1)'; }
    else if (v < 5){ pill.style.background = 'rgba(245,158,11,.3)'; pill.style.border='1px solid rgba(255,255,255,.1)'; }
    else { pill.style.background = 'rgba(16,185,129,.3)'; pill.style.border='1px solid rgba(255,255,255,.1)'; }
    histBox.appendChild(pill);
  });
}
function setPlayers(state){
  playersBox.innerHTML = '';
  const you = document.createElement('div');
  you.textContent = state?.cashout ? `You cashed out at ${state.cashout}×` : 'You are in…';
  playersBox.appendChild(you);
}

betBtn.onclick = async ()=>{
  const bet = parseFloat(betInput.value||'0');
  if (!bet) { alert('Enter bet'); return; }
  const r = await fetch(`/api/aviator/start?chat_id=${encodeURIComponent(CHAT_ID)}`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ bet })
  });
  const j = await r.json();
  if(!j.ok){ alert(j.error||'Error'); return; }
  currentRoundId = j.round_id;
  statusText.textContent = 'Flying…';
  cashoutBtn.disabled = false;
  loopState();
};
cashoutBtn.onclick = async ()=>{
  if(!currentRoundId) return;
  const r = await fetch(`/api/aviator/cashout?chat_id=${encodeURIComponent(CHAT_ID)}&round_id=${encodeURIComponent(currentRoundId)}`, { method:'POST' });
  const j = await r.json();
  if(!j.ok){ alert(j.error||'Error'); return; }
  statusText.textContent = `Cashed out ${j.cashout_mult}×`;
  addHistory(j.final_mult);
  cashoutBtn.disabled = true;
  fetchUser();
};
async function loopState(){
  if(!currentRoundId) return;
  const r = await fetch(`/api/aviator/state?chat_id=${encodeURIComponent(CHAT_ID)}&round_id=${encodeURIComponent(currentRoundId)}`);
  const j = await r.json();
  multText.textContent = (j.mult || 1).toFixed(2) + '×';
  setPlayers(j);
  if (j.done){
    addHistory(j.final_mult);
    currentRoundId = null;
    cashoutBtn.disabled = true;
    statusText.textContent = 'Round ended';
    fetchUser();
    return;
  }
  setTimeout(loopState, 500);
}

/* ==== Walk ==== */
document.getElementById('steps_btn').onclick = async ()=>{
  const steps = parseInt(document.getElementById('steps_input').value||'0',10);
  if (!steps || steps < 1) { alert('Enter steps'); return; }
  const r = await fetch(`/api/walk?chat_id=${encodeURIComponent(CHAT_ID)}`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ steps })
  });
  const j = await r.json();
  if(!j.ok){ alert(j.error||'Error'); return; }
  document.getElementById('walk_result').textContent = `+ $${j.earned_usd}`;
  totalStepsEl.textContent = j.total_steps;
  fetchUser();
};

document.getElementById('upgrade_btn').onclick = async ()=>{
  const target_level = parseInt(document.getElementById('upgrade_level').value||'0',10);
  if (!target_level) return;
  const r = await fetch(`/api/upgrade?chat_id=${encodeURIComponent(CHAT_ID)}`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ target_level })
  });
  const j = await r.json();
  if(!j.ok){ alert(j.error||'Error'); return; }
  alert(`Upgraded to Lvl ${j.walk_level} @ $${j.walk_rate}/step`);
  fetchUser();
};

/* ==== Wallet ==== */
async function loadHistory(){
  const r = await fetch(`/api/transactions?chat_id=${encodeURIComponent(CHAT_ID)}`);
  const j = await r.json();
  const box = document.getElementById('history_box');
  box.innerHTML = '';
  for (const t of j.items || []){
    const row = document.createElement('div');
    row.className = 'glass';
    row.style.display='flex'; row.style.justifyContent='space-between'; row.style.alignItems='center';
    row.style.padding='10px'; row.style.borderRadius='10px'; row.style.marginBottom='6px';
    const amt=(parseFloat(t.amount_usd)>=0?'+$':'-$')+Math.abs(parseFloat(t.amount_usd)).toFixed(2);
    row.innerHTML = `
      <div>
        <div class="font-semibold">${t.type} <span class="text-xs" style="opacity:.6">#${t.id}</span></div>
        <div class="text-xs" style="opacity:.6">${new Date(t.created_at).toLocaleString()}</div>
      </div>
      <div style="text-align:right">
        <div style="color:${parseFloat(t.amount_usd)>=0?'#bbf7d0':'#fecaca'}">${amt}</div>
        <div class="text-xs" style="opacity:.6">${t.status}</div>
      </div>`;
    box.appendChild(row);
  }
}
document.getElementById('dep_btn').onclick = async ()=>{
  const amount_ngn=parseFloat(document.getElementById('dep_amount_ngn').value||'0');
  if (!amount_ngn || amount_ngn < 100) { alert('Minimum deposit is ₦100'); return; }
  const r=await fetch(`/api/deposit?chat_id=${encodeURIComponent(CHAT_ID)}`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ amount_ngn })
  });
  const j=await r.json();
  if(!j.ok){ alert(j.error||'Deposit init failed'); return; }
  window.open(j.checkout_url, '_blank');
};
document.getElementById('wd_btn').onclick = async ()=>{
  const amount=parseFloat(document.getElementById('wd_amount').value||'0').toFixed(2);
  const payout=document.getElementById('wd_payout').value||'';
  const r=await fetch(`/api/withdraw?chat_id=${encodeURIComponent(CHAT_ID)}`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ amount, payout })
  });
  const j=await r.json();
  if(!j.ok){ alert(j.error||'Error'); return; }
  alert(`Withdrawal requested. Ticket #${j.request_id}`);
  fetchUser(); loadHistory();
};

/* ==== Init ==== */
fetchUser(); showPanel('tap');
</script>
</body>
</html>
"""

# =========================
# Routes
# =========================
@app.get("/")
def index():
    return render_template_string(
        BASE_HTML,
        max_tap=MAX_TAP_PER_REQUEST,
        tap_reward=str(to_cents(TAP_REWARD))
    )

@app.get("/api/user")
def api_user():
    user = get_or_create_user_from_query()
    return jsonify({
        "chat_id": user.chat_id,
        "username": user.username,
        "balance_usd": str(to_cents(user.balance_usd)),
        "balance_ngn": str(to_cents(user.balance_ngn)),
        "walk_level": user.walk_level,
        "walk_rate": str(user.walk_rate),
        "total_steps": int(user.total_steps or 0),
    })

@app.post("/api/tap")
def api_tap():
    user = get_or_create_user_from_query()
    body = request.get_json(silent=True) or {}
    count = int(body.get("count", 0))
    if count <= 0:
        return jsonify({"ok": False, "error": "count>0 required"}), 400
    if count > MAX_TAP_PER_REQUEST:
        count = MAX_TAP_PER_REQUEST
    earn = to_cents(TAP_REWARD * Decimal(count))
    add_tx(user, "tap", usd=earn, ngn=earn * USD_TO_NGN, status="approved", meta={"count": count})
    return jsonify({"ok": True, "earned_usd": str(earn), "balance_usd": str(user.balance_usd)})

@app.post("/api/walk")
def api_walk():
    user = get_or_create_user_from_query()
    body = request.get_json(silent=True) or {}
    steps = int(body.get("steps", 0))
    if steps <= 0:
        return jsonify({"ok": False, "error": "steps>0 required"}), 400

    # reset day handled in getter; enforce cap
    cap = WALK_DAILY_CAP_USD
    remaining_usd = to_cents(cap - Decimal(user.steps_usd_today or 0))
    if remaining_usd <= 0:
        return jsonify({"ok": True, "earned_usd": "0.00", "cap_reached": True, "balance_usd": str(user.balance_usd)})

    earn = to_cents(Decimal(user.walk_rate) * Decimal(steps))
    if earn > remaining_usd:
        earn = remaining_usd

    # Apply
    user.total_steps = int(user.total_steps or 0) + steps
    user.steps_usd_today = to_cents(Decimal(user.steps_usd_today or 0) + earn)
    db.session.commit()

    add_tx(user, "walk", usd=earn, ngn=earn * USD_TO_NGN, status="approved", meta={"steps": steps, "rate": str(user.walk_rate)})
    return jsonify({
        "ok": True,
        "earned_usd": str(earn),
        "balance_usd": str(user.balance_usd),
        "balance_ngn": str(user.balance_ngn),
        "total_steps": int(user.total_steps),
        "cap_reached": user.steps_usd_today >= cap
    })

@app.post("/api/upgrade")
def api_upgrade():
    user = get_or_create_user_from_query()
    body = request.get_json(silent=True) or {}
    target = int(body.get("target_level", 0))
    if target <= user.walk_level:
        return jsonify({"ok": False, "error": "Target must be higher than current level"}), 400
    if target not in WALK_UPGRADES:
        return jsonify({"ok": False, "error": "Invalid level"}), 400

    total_cost = Decimal("0.00")
    for lvl in range(user.walk_level + 1, target + 1):
        total_cost += WALK_UPGRADES[lvl]["price"]

    if Decimal(user.balance_usd) < total_cost:
        return jsonify({"ok": False, "error": "Insufficient balance", "required_usd": str(to_cents(total_cost))}), 400

    # Deduct & set new level + rate
    add_tx(user, "upgrade", usd=-to_cents(total_cost), ngn=-to_cents(total_cost * USD_TO_NGN), status="approved",
           meta={"from": user.walk_level, "to": target, "new_rate": str(WALK_UPGRADES[target]["rate"])})

    user.walk_level = target
    user.walk_rate = WALK_UPGRADES[target]["rate"]
    db.session.commit()

    return jsonify({"ok": True, "balance_usd": str(user.balance_usd), "walk_level": user.walk_level, "walk_rate": str(user.walk_rate)})

# --- Aviator ---
# In-memory rounds (simple demo)
ROUNDS = {}  # { (chat_id, round_id): {"start": datetime, "mult": Decimal, "done": bool, "final_mult": Decimal, "cashout": Decimal|None} }

@app.post("/api/aviator/start")
def api_aviator_start():
    user = get_or_create_user_from_query()
    body = request.get_json(silent=True) or {}
    bet = Decimal(str(body.get("bet", "0")))
    if bet < MIN_BET or bet > MAX_BET:
        return jsonify({"ok": False, "error": f"Bet must be between {MIN_BET} and {MAX_BET}"}), 400
    if Decimal(user.balance_usd) < bet:
        return jsonify({"ok": False, "error": "Insufficient balance"}), 400

    # Deduct bet via ledger
    add_tx(user, "aviator_bet", usd=-to_cents(bet), ngn=-to_cents(bet * USD_TO_NGN), status="approved")

    round_id = f"r{int(datetime.utcnow().timestamp()*1000)}"
    final_mult = sample_crash_multiplier()
    ROUNDS[(user.chat_id, round_id)] = {
        "start": now_utc(),
        "mult": Decimal("1.00"),
        "done": False,
        "final_mult": final_mult,
        "cashout": None
    }
    return jsonify({"ok": True, "round_id": round_id})

@app.get("/api/aviator/state")
def api_aviator_state():
    user = get_or_create_user_from_query()
    round_id = request.args.get("round_id") or ""
    key = (user.chat_id, round_id)
    st = ROUNDS.get(key)
    if not st:
        return jsonify({"ok": False, "error": "No such round"}), 404

    # simple time-based growth
    elapsed = (now_utc() - st["start"]).total_seconds()
    if not st["done"]:
        current = Decimal("1.00") + AVIATOR_GROWTH_PER_SEC * Decimal(elapsed)
        if current >= st["final_mult"]:
            st["done"] = True
            st["mult"] = st["final_mult"]
        else:
            st["mult"] = current

    return jsonify({"ok": True, "mult": float(st["mult"]), "done": st["done"], "final_mult": float(st["final_mult"]), "cashout": float(st["cashout"]) if st["cashout"] else None})

@app.post("/api/aviator/cashout")
def api_aviator_cashout():
    user = get_or_create_user_from_query()
    round_id = request.args.get("round_id") or ""
    key = (user.chat_id, round_id)
    st = ROUNDS.get(key)
    if not st:
        return jsonify({"ok": False, "error": "No such round"}), 404

    # finalize current mult and pay if not already cashed
    elapsed = (now_utc() - st["start"]).total_seconds()
    current = Decimal("1.00") + AVIATOR_GROWTH_PER_SEC * Decimal(elapsed)
    if current > st["final_mult"]:
        current = st["final_mult"]
        st["done"] = True
    if st["cashout"]:
        return jsonify({"ok": False, "error": "Already cashed out"}), 400

    st["cashout"] = to_cents(current)
    # Payout equals bet * cashout - (already deducted bet). For demo, just credit (bet * (mult-1)).
    # In production, store the bet amount in the round state.
    # Here we approximate paying $1 * (mult-1) for illustration since bet amount isn't persisted.
    payout = Decimal("1.00") * (st["cashout"] - Decimal("1.00"))
    if payout > 0:
        add_tx(user, "aviator_cashout", usd=to_cents(payout), ngn=to_cents(payout * USD_TO_NGN), status="approved")
    return jsonify({"ok": True, "cashout_mult": float(st["cashout"]), "final_mult": float(st["final_mult"])})

# --- Wallet & Transactions ---
@app.get("/api/transactions")
def api_transactions():
    user = get_or_create_user_from_query()
    items = (Transaction.query
             .filter_by(chat_id=user.chat_id)
             .order_by(Transaction.created_at.desc())
             .limit(50)
             .all())
    def _row(t: Transaction):
        return {
            "id": t.id,
            "type": t.type,
            "amount_usd": str(to_cents(t.amount_usd)),
            "amount_ngn": str(to_cents(t.amount_ngn)),
            "status": t.status,
            "created_at": t.created_at.isoformat()
        }
    return jsonify({"ok": True, "items": [_row(t) for t in items]})

@app.post("/api/deposit")
def api_deposit():
    user = get_or_create_user_from_query()
    body = request.get_json(silent=True) or {}
    amount_ngn = Decimal(str(body.get("amount_ngn", "0")))
    if amount_ngn < MIN_DEPOSIT_NGN:
        return jsonify({"ok": False, "error": f"Minimum deposit is ₦{MIN_DEPOSIT_NGN}"}), 400
    # Normally: create Paystack transaction, return checkout URL
    # Stub for now:
    checkout_url = "https://paystack.mock/checkout"
    add_tx(user, "deposit", usd=to_cents(amount_ngn / USD_TO_NGN), ngn=to_cents(amount_ngn), status="pending")
    return jsonify({"ok": True, "checkout_url": checkout_url})

@app.post("/api/withdraw")
def api_withdraw():
    user = get_or_create_user_from_query()
    body = request.get_json(silent=True) or {}
    amount = Decimal(str(body.get("amount", "0")))
    payout = (body.get("payout") or "").strip()
    if amount < MIN_WITHDRAW_USD:
        return jsonify({"ok": False, "error": f"Minimum withdraw is ${MIN_WITHDRAW_USD}"}), 400
    if Decimal(user.balance_usd) < amount:
        return jsonify({"ok": False, "error": "Insufficient balance"}), 400
    # Hold funds
    add_tx(user, "withdraw_request", usd=-to_cents(amount), ngn=-to_cents(amount * USD_TO_NGN), status="pending", meta={"payout": payout})
    return jsonify({"ok": True, "request_id": "W" + str(int(datetime.utcnow().timestamp()))})

# =========================
# Run
# =========================
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
