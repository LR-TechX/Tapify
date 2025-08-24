import os
import json
import random
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, getcontext

from flask import Flask, request, jsonify, render_template_string, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

# --------------------
# Config
# --------------------
getcontext().prec = 28

# Force Postgres only (no SQLite fallback)
DATABASE_URL = os.environ["DATABASE_URL"]
SECRET_KEY = os.environ["SECRET_KEY"]

USD_TO_NGN = Decimal("1000")      # Project mapping: $8 == ₦8000
TAP_REWARD = Decimal("0.001")     # per tap
MAX_TAP_PER_REQUEST = 50

# Walk upgrades (level -> {rate per step, price})
WALK_UPGRADES = {
    1: {"rate": Decimal("0.01"), "price": Decimal("0.00")},
    2: {"rate": Decimal("0.02"), "price": Decimal("5.00")},
    3: {"rate": Decimal("0.05"), "price": Decimal("15.00")},
    4: {"rate": Decimal("0.10"), "price": Decimal("40.00")},
}

# Aviator parameters
AVIATOR_GROWTH_PER_SEC = Decimal("0.25")
MIN_BET = Decimal("0.10")
MAX_BET = Decimal("1000.00")

# Wallet params
MIN_DEPOSIT = Decimal("1.00")
MIN_WITHDRAW = Decimal("5.00")


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


# --------------------
# App & DB
# --------------------
app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SQLALCHEMY_DATABASE_URI=DATABASE_URL,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
)

db = SQLAlchemy(app)


# --------------------
# Models (chat_id is the key)
# --------------------
class User(db.Model):
    __tablename__ = "users"
    # IMPORTANT: chat_id is the primary key (no integer id)
    chat_id = db.Column(db.BigInteger, primary_key=True)
    username = db.Column(db.String(128))

    balance_usd = db.Column(db.Numeric(18, 2), default=Decimal("0.00"))
    balance_ngn = db.Column(db.Numeric(18, 2), default=Decimal("0.00"))

    walk_level = db.Column(db.Integer, default=1)
    walk_rate = db.Column(db.Numeric(18, 4), default=Decimal("0.01"))
    total_steps = db.Column(db.BigInteger, default=0)

    created_at = db.Column(db.DateTime(timezone=True), default=lambda:
    datetime.now(timezone.utc))

    def ensure_defaults(self):
        changed = False
        if self.balance_usd is None:
            self.balance_usd = Decimal("0.00"); changed = True
        if self.balance_ngn is None:
            self.balance_ngn = Decimal("0.00"); changed = True
        if not self.walk_level:
            self.walk_level = 1; changed = True
        if not self.walk_rate:
            self.walk_rate = Decimal("0.01"); changed = True
        if self.total_steps is None:
            self.total_steps = 0; changed = True
        return changed


class Tx(db.Model):
    __tablename__ = "transactions"
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.BigInteger, db.ForeignKey("users.chat_id"), nullable=False)
    type = db.Column(db.String(64), nullable=False)  # tap, walk, aviator_bet, aviator_cashout, upgrade, deposit, withdraw, withdraw_revert, signup_bonus
    status = db.Column(db.String(32), default="approved")  # approved|pending|rejected

    amount_usd = db.Column(db.Numeric(18, 2), nullable=False)
    meta = db.Column(db.JSON)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda:
    datetime.now(timezone.utc))


class AviatorRound(db.Model):
    __tablename__ = "aviator_rounds"
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.BigInteger, db.ForeignKey("users.chat_id"), nullable=False)
    bet_usd = db.Column(db.Numeric(18, 2), nullable=False)
    start_time = db.Column(db.DateTime(timezone=True), nullable=False)
    crash_multiplier = db.Column(db.Numeric(18, 2), nullable=False)
    growth_per_sec = db.Column(db.Numeric(18, 2), nullable=False,
                               default=AVIATOR_GROWTH_PER_SEC)

    status = db.Column(db.String(32), default="active")  # active|cashed|crashed
    cashout_multiplier = db.Column(db.Numeric(18, 2))
    cashout_time = db.Column(db.DateTime(timezone=True))
    profit_usd = db.Column(db.Numeric(18, 2))


with app.app_context():
    db.create_all()

# --------------------
# Auto-migrate (adds missing columns that the game expects to the shared users table)
# --------------------
with app.app_context():
    alter_sql = """
    ALTER TABLE users
      ADD COLUMN IF NOT EXISTS balance_usd NUMERIC(18,2) DEFAULT 0,
      ADD COLUMN IF NOT EXISTS balance_ngn NUMERIC(18,2) DEFAULT 0,
      ADD COLUMN IF NOT EXISTS walk_level INT DEFAULT 1,
      ADD COLUMN IF NOT EXISTS walk_rate NUMERIC(18,4) DEFAULT 0.01,
      ADD COLUMN IF NOT EXISTS total_steps BIGINT DEFAULT 0,
      ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
    """
    try:
        db.session.execute(text(alter_sql))
        db.session.commit()
    except Exception as e:
        # Don't crash the app if migration fails; log to stdout/stderr
        print("Warning: Could not auto-migrate users table →", e)


# --------------------
# User helper
# --------------------
def get_or_create_user_from_query():
    chat_id = request.args.get("chat_id") or request.headers.get("X-Chat-Id") or (request.json or {}).get("chat_id")
    username = request.args.get("username") or (request.json or {}).get("username")
    if not chat_id:
        abort(400, "Missing chat_id. Launch from Telegram WebApp button or append ?chat_id=...")

    try:
        chat_id = int(chat_id)
    except Exception:
        abort(400, "chat_id must be numeric")

    user = User.query.get(chat_id)
    if not user:

        user = User(chat_id=chat_id, username=username)
        user.ensure_defaults()
        # Signup bonus
        user.balance_usd = Decimal("8.00")
        user.balance_ngn = to_cents(user.balance_usd * USD_TO_NGN)
        db.session.add(user)
        db.session.flush()
        db.session.add(Tx(chat_id=user.chat_id, type="signup_bonus", status="approved", amount_usd=Decimal("8.00"), meta={"ngn": str(user.balance_ngn)}))
        db.session.commit()
    else:
        if user.ensure_defaults():
            db.session.commit()
    return user


# --------------------
# API — Profile
# --------------------
@app.get("/api/user")
def api_user():
    user = get_or_create_user_from_query()
    return jsonify({
        "chat_id": user.chat_id,
        "username": user.username,
        "balance_usd": str(to_cents(user.balance_usd)),
        "balance_ngn": str(to_cents(user.balance_ngn)),
        "walk_level": user.walk_level,
        "walk_rate": str(to_cents(user.walk_rate)),
        "total_steps": int(user.total_steps or 0),
    })


# --------------------
# API — Tap Coin
# --------------------
@app.post("/api/tap")
def api_tap():
    user = get_or_create_user_from_query()
    body = request.get_json(silent=True) or {}
    count = int(body.get("count", 1))
    count = max(1, min(count, MAX_TAP_PER_REQUEST))
    earn = to_cents(TAP_REWARD * Decimal(count))
    user.balance_usd = to_cents(Decimal(user.balance_usd) + earn)

    user.balance_ngn = to_cents(user.balance_usd * USD_TO_NGN)
    db.session.add(Tx(chat_id=user.chat_id, type="tap", status="approved", amount_usd=earn, meta={"count": count}))
    db.session.commit()
    return jsonify({"ok": True, "earned_usd": str(earn), "balance_usd": str(user.balance_usd), "balance_ngn": str(user.balance_ngn)})


# --------------------
# API — Walk & Earn
# --------------------

@app.route("/", methods=["GET", "HEAD"])
@app.route("/app", methods=["GET", "HEAD"])
def app_entry():
    chat_id = request.args.get("chat_id")
    username = request.args.get("username", "guest")

    if not chat_id:
        return "Missing chat_id", 400

    user = User.query.get(chat_id)
    if not user:
        user = User(chat_id=chat_id, username=username)
        db.session.add(user)
        db.session.commit()

    # Return same page for GET and HEAD
    if request.method == "HEAD":
        return "", 200

    return index()

@app.post("/api/steps")
def api_steps():
    user = get_or_create_user_from_query()
    body = request.get_json(silent=True) or {}
    steps = int(body.get("steps", 0))
    if steps <= 0:
        return jsonify({"ok": False, "error": "steps must be positive"}), 400
    earn = to_cents(Decimal(user.walk_rate) * Decimal(steps))
    user.total_steps = int(user.total_steps or 0) + steps
    user.balance_usd = to_cents(Decimal(user.balance_usd) + earn)
    user.balance_ngn = to_cents(user.balance_usd * USD_TO_NGN)
    db.session.add(Tx(chat_id=user.chat_id, type="walk", status="approved", amount_usd=earn, meta={"steps": steps, "rate": str(user.walk_rate)}))
    db.session.commit()
    return jsonify({"ok": True, "earned_usd": str(earn), "balance_usd": str(user.balance_usd), "balance_ngn": str(user.balance_ngn), "total_steps": int(user.total_steps)})


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

    user.balance_usd = to_cents(Decimal(user.balance_usd) - total_cost)
    user.walk_level = target
    user.walk_rate = WALK_UPGRADES[target]["rate"]
    user.balance_ngn = to_cents(user.balance_usd * USD_TO_NGN)

    db.session.add(Tx(chat_id=user.chat_id, type="upgrade", status="approved", amount_usd=-to_cents(total_cost), meta={"new_level": target, "new_rate": str(user.walk_rate)}))
    db.session.commit()
    return jsonify({"ok": True, "balance_usd": str(user.balance_usd), "walk_level": user.walk_level, "walk_rate": str(user.walk_rate)})


# --------------------
# API — Aviator
# --------------------
@app.post("/api/aviator/start")
def api_aviator_start():
    user = get_or_create_user_from_query()
    body = request.get_json(silent=True) or {}
    bet = Decimal(str(body.get("bet", "0")))
    if bet < MIN_BET or bet > MAX_BET:
        return jsonify({"ok": False, "error": f"Bet must be between {MIN_BET} and {MAX_BET}"}), 400
    if Decimal(user.balance_usd) < bet:
        return jsonify({"ok": False, "error": "Insufficient balance"}), 400

    user.balance_usd = to_cents(Decimal(user.balance_usd) - bet)
    user.balance_ngn = to_cents(user.balance_usd * USD_TO_NGN)

    round_obj = AviatorRound(
        chat_id=user.chat_id,
        bet_usd=to_cents(bet),
        start_time=datetime.now(timezone.utc),
        crash_multiplier=sample_crash_multiplier(),
        growth_per_sec=AVIATOR_GROWTH_PER_SEC,
        status="active",
    )
    db.session.add(round_obj)
    db.session.add(Tx(chat_id=user.chat_id, type="aviator_bet", status="approved", amount_usd=-to_cents(bet), meta={}))
    db.session.commit()
    return jsonify({"ok": True, "round_id": round_obj.id, "bet": str(round_obj.bet_usd)})


def _aviator_state(round_obj: AviatorRound):
    now = datetime.now(timezone.utc)
    elapsed = Decimal(str((now - round_obj.start_time).total_seconds()))
    current_mult = Decimal("1.00") + (Decimal(round_obj.growth_per_sec) * elapsed)
    crashed = current_mult >= Decimal(round_obj.crash_multiplier)

    if crashed:
        current_mult = Decimal(round_obj.crash_multiplier)
    return current_mult.quantize(Decimal("0.01"), rounding=ROUND_DOWN), crashed


@app.get("/api/aviator/state")
def api_aviator_state():
    user = get_or_create_user_from_query()
    round_id = request.args.get("round_id")
    r = AviatorRound.query.filter_by(id=round_id, chat_id=user.chat_id).first()
    if not r:
        return jsonify({"ok": False, "error": "Round not found"}), 404
    if r.status != "active":
        return jsonify({"ok": True, "status": r.status, "current_multiplier": str(r.cashout_multiplier or r.crash_multiplier)})
    mult, crashed = _aviator_state(r)
    if crashed:
        r.status = "crashed"
        db.session.commit()
        return jsonify({"ok": True, "status": "crashed", "current_multiplier": str(mult)})
    return jsonify({"ok": True, "status": "active", "current_multiplier": str(mult)})


@app.post("/api/aviator/cashout")
def api_aviator_cashout():
    user = get_or_create_user_from_query()
    body = request.get_json(silent=True) or {}
    round_id = body.get("round_id")
    r = AviatorRound.query.filter_by(id=round_id, chat_id=user.chat_id).first()
    if not r:
        return jsonify({"ok": False, "error": "Round not found"}), 404
    if r.status != "active":
        return jsonify({"ok": False, "error": f"Round is {r.status}"}), 400

    mult, crashed = _aviator_state(r)
    if crashed:
        r.status = "crashed"
        db.session.commit()
        return jsonify({"ok": False, "error": "Crashed before cashout"}), 400

    payout = to_cents(Decimal(r.bet_usd) * mult)
    profit = to_cents(payout - Decimal(r.bet_usd))

    user.balance_usd = to_cents(Decimal(user.balance_usd) + payout)

    user.balance_ngn = to_cents(user.balance_usd * USD_TO_NGN)

    r.status = "cashed"
    r.cashout_multiplier = mult
    r.cashout_time = datetime.now(timezone.utc)
    r.profit_usd = profit

    db.session.add(Tx(chat_id=user.chat_id, type="aviator_cashout", status="approved", amount_usd=payout, meta={"mult": str(mult)}))
    db.session.commit()
    return jsonify({"ok": True, "payout_usd": str(payout), "multiplier": str(mult), "balance_usd": str(user.balance_usd), "balance_ngn": str(user.balance_ngn)})


# --------------------
# API — Wallet (Deposit / Withdraw / History)
# --------------------
@app.post("/api/deposit")
def api_deposit_create():
    user = get_or_create_user_from_query()
    body = request.get_json(silent=True) or {}
    amount = Decimal(str(body.get("amount", "0")))
    method = (body.get("method") or "manual").strip()
    reference = (body.get("reference") or "").strip()

    if amount < MIN_DEPOSIT:
        return jsonify({"ok": False, "error": f"Minimum deposit is ${MIN_DEPOSIT}"}), 400

    tx = Tx(chat_id=user.chat_id, type="deposit", status="pending", amount_usd=to_cents(amount), meta={"method": method, "reference": reference})
    db.session.add(tx)
    db.session.commit()
    return jsonify({"ok": True, "message": "Deposit created. Awaiting admin approval.", "tx_id": tx.id})


@app.post("/api/withdraw")
def api_withdraw_request():
    user = get_or_create_user_from_query()
    body = request.get_json(silent=True) or {}
    amount = Decimal(str(body.get("amount", "0")))
    payout = (body.get("payout") or "").strip()  # bank/wallet info

    if amount < MIN_WITHDRAW:

        return jsonify({"ok": False, "error": f"Minimum withdraw is ${MIN_WITHDRAW}"}), 400
    if Decimal(user.balance_usd) < amount:
        return jsonify({"ok": False, "error": "Insufficient balance"}), 400

    # Hold funds immediately
    user.balance_usd = to_cents(Decimal(user.balance_usd) - amount)
    user.balance_ngn = to_cents(user.balance_usd * USD_TO_NGN)

    tx = Tx(chat_id=user.chat_id, type="withdraw", status="pending", amount_usd=-to_cents(amount), meta={"payout": payout})
    db.session.add(tx)
    db.session.commit()
    return jsonify({"ok": True, "message": "Withdrawal requested. Awaiting admin approval.", "tx_id": tx.id, "balance_usd": str(user.balance_usd)})


@app.get("/api/transactions")
def api_transactions():
    user = get_or_create_user_from_query()
    q = Tx.query.filter_by(chat_id=user.chat_id).order_by(Tx.id.desc()).limit(50).all()
    return jsonify({
        "ok": True,
        "items": [
            {
                "id": t.id,
                "type": t.type,
                "status": t.status,
                "amount_usd": str(to_cents(t.amount_usd)),
                "meta": t.meta or {},
                "created_at": t.created_at.isoformat(),
            } for t in q
        ]
    })


# --------------------
# UI (Tailwind single-file)
# --------------------
BASE_HTML = """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />

  <title>Tapify — WebApp</title>
  <script src=\"https://cdn.tailwindcss.com\"></script>
  <link
href=\"https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap\"
rel=\"stylesheet\"/>
  <style>
    body { font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, Noto Sans, Helvetica Neue, Arial; }
    .glass { backdrop-filter: blur(8px); background: rgba(255,255,255,0.08); }
    .card { border-radius: 1rem; box-shadow: 0 10px 30px rgba(0,0,0,0.1); }
    .btn { border-radius: 9999px; padding: 0.75rem 1.25rem; font-weight: 700; }
    .nav-btn { padding: .5rem .9rem; border-radius: 9999px; }
    dialog::backdrop { background: rgba(0,0,0,0.5); }
  </style>
</head>
<body class=\"min-h-screen bg-gradient-to-b from-slate-900 to-slate-800 text-white\">
  <div class=\"max-w-xl mx-auto p-4 space-y-4\">

    <header class=\"flex items-center justify-between\">
      <div>
        <h1 class=\"text-2xl font-extrabold\">Tapify</h1>
        <p class=\"text-slate-300 text-sm\">NotCoin vibes • Hamster energy • Sporty UI</p>
      </div>
      <div class=\"text-right\">
        <div class=\"text-sm text-slate-400\">USD</div>
        <div id=\"usd\" class=\"text-xl font-bold\">$0.00</div>
        <div id=\"ngn\" class=\"text-xs text-slate-400\">₦0</div>
      </div>
    </header>

    <div class=\"glass card p-2 grid grid-cols-4 gap-2\">
      <button id=\"tab_tap\" class=\"nav-btn bg-white/10 hover:bg-white/20\">Tap Coin</button>
      <button id=\"tab_aviator\" class=\"nav-btn hover:bg-white/20\">Aviator</button>
      <button id=\"tab_walk\" class=\"nav-btn hover:bg-white/20\">Walk & Earn</button>
      <button id=\"tab_wallet\" class=\"nav-btn hover:bg-white/20\">Wallet</button>
    </div>

    <!-- Tap Tab -->
    <section id=\"panel_tap\" class=\"glass card p-5 space-y-4\">
      <div class=\"text-center space-y-2\">
        <div class=\"text-slate-300 text-sm\">Tap the coin to earn ${{tap_reward}} per tap</div>
        <button id=\"tap_btn\" class=\"btn bg-amber-400 text-slate-900 w-full text-xl\">💰 TAP</button>

        <div class=\"text-sm text-slate-400\">You can batch up to {{max_tap}} taps per request.</div>
      </div>
    </section>

    <!-- Aviator Tab -->
    <section id=\"panel_aviator\" class=\"glass card p-5 space-y-4 hidden\">
      <div>
        <h2 class=\"font-bold text-lg\">Aviator</h2>
        <p class=\"text-slate-300 text-sm\">Bet, watch the multiplier grow, and cash out before it crashes.</p>
      </div>
      <div class=\"grid grid-cols-3 gap-3\">
        <input id=\"bet_input\" type=\"number\" step=\"0.01\" min=\"0.10\" placeholder=\"Bet $\" class=\"col-span-2 px-3 py-2 rounded bg-white/10 outline-none\"/>
        <button id=\"bet_btn\" class=\"btn bg-indigo-400 text-slate-900\">Bet</button>
      </div>
      <div id=\"aviator_board\" class=\"glass p-6 rounded text-center space-y-2\">
        <div class=\"text-sm text-slate-400\">Current Multiplier</div>
        <div id=\"mult_text\" class=\"text-5xl font-extrabold\">1.00×</div>
        <div id=\"status_text\" class=\"text-slate-400\">Place a bet to start.</div>
        <div class=\"flex gap-2 justify-center\">
          <button id=\"cashout_btn\" class=\"btn bg-emerald-400 text-slate-900 disabled:opacity-40\" disabled>Cash Out</button>
        </div>
      </div>
      <div class=\"text-xs text-slate-400\">Growth ≈ +0.25x/sec. Crashes are random & heavy-tailed. Don’t be greedy 😉</div>
    </section>

    <!-- Walk Tab -->
    <section id=\"panel_walk\" class=\"glass card p-5 space-y-4 hidden\">
      <div class=\"flex items-center justify-between\">
        <div>
          <h2 class=\"font-bold text-lg\">Walk & Earn</h2>
          <p class=\"text-slate-300 text-sm\">Current rate: <span id=\"walk_rate\">$0.01</span> / step • Steps: <span id=\"total_steps\">0</span></p>
        </div>
        <div class=\"text-right\">
          <button id=\"upgrade_btn\" class=\"btn bg-fuchsia-400 text-slate-900\">Upgrade</button>
        </div>
      </div>

      <div class=\"space-y-3\">

        <div class=\"text-sm text-slate-300\">Use auto step counter (motion) or input manually if your device blocks sensors.</div>
        <div class=\"grid grid-cols-3 gap-3\">
          <button id=\"start_walk\" class=\"btn bg-teal-400 text-slate-900\">Start</button>
          <button id=\"stop_walk\" class=\"btn bg-rose-400 text-slate-900\">Stop</button>
          <button id=\"send_steps\" class=\"btn bg-amber-300 text-slate-900\">Send Steps</button>
        </div>
        <div class=\"grid grid-cols-3 gap-3\">
          <input id=\"manual_steps\" type=\"number\" class=\"col-span-2 px-3 py-2 rounded bg-white/10 outline-none\" placeholder=\"Manual steps\"/>
          <button id=\"add_manual\" class=\"btn bg-white text-slate-900\">Add</button>
        </div>
        <div class=\"text-center text-2xl\">Session steps: <span id=\"session_steps\">0</span></div>
      </div>

      <dialog id=\"upgrade_modal\" class=\"p-0 bg-transparent\">
        <div class=\"bg-slate-900 p-5 rounded-xl max-w-sm w-[90vw] space-y-3\">
          <h3 class=\"text-lg font-bold\">Upgrade Walk Rate</h3>
          <div class=\"text-sm text-slate-300\">Levels boost your $/step. Prices are cumulative when jumping multiple levels.</div>
          <div class=\"space-y-2 text-sm\">
            <div class=\"flex items-center justify-between\"><span>Lvl 1 • $0.01/step</span><span class=\"text-slate-400\">$0</span></div>
            <div class=\"flex items-center justify-between\"><span>Lvl 2 • $0.02/step</span><span class=\"text-slate-400\">$5</span></div>
            <div class=\"flex items-center justify-between\"><span>Lvl 3 • $0.05/step</span><span class=\"text-slate-400\">$15</span></div>
            <div class=\"flex items-center justify-between\"><span>Lvl 4 • $0.10/step</span><span class=\"text-slate-400\">$40</span></div>
          </div>
          <div class=\"grid grid-cols-2 gap-2\">
            <input id=\"target_level\" type=\"number\" min=\"2\" max=\"4\" class=\"px-3 py-2 rounded bg-white/10 outline-none\" placeholder=\"Target level (2-4)\"/>
            <button id=\"confirm_upgrade\" class=\"btn bg-fuchsia-400 text-slate-900\">Confirm</button>
          </div>
          <button id=\"close_upgrade\" class=\"w-full btn bg-white/10\">Close</button>
        </div>
      </dialog>
    </section>

    <!-- Wallet Tab -->

    <section id=\"panel_wallet\" class=\"glass card p-5 space-y-5 hidden\">
      <h2 class=\"font-bold text-lg\">Wallet</h2>

      <div class=\"space-y-2\">
        <h3 class=\"font-semibold\">Deposit</h3>
        <div class=\"grid grid-cols-3 gap-2\">
          <input id=\"dep_amount\" type=\"number\" step=\"0.01\" min=\"1\" placeholder=\"Amount ($)\" class=\"col-span-1 px-3 py-2 rounded bg-white/10 outline-none\"/>
          <input id=\"dep_ref\" placeholder=\"Payment Ref/Txn ID\" class=\"col-span-2 px-3 py-2 rounded bg-white/10 outline-none\"/>
        </div>
        <button id=\"dep_btn\" class=\"btn bg-emerald-400 text-slate-900\">Create Deposit</button>
        <p class=\"text-xs text-slate-400\">After you transfer, paste your reference/ID and submit. Admin will approve.</p>
      </div>

      <div class=\"space-y-2\">
        <h3 class=\"font-semibold\">Withdraw</h3>
        <div class=\"grid grid-cols-3 gap-2\">
          <input id=\"wd_amount\" type=\"number\" step=\"0.01\" min=\"5\" placeholder=\"Amount ($)\" class=\"col-span-1 px-3 py-2 rounded bg-white/10 outline-none\"/>
          <input id=\"wd_payout\" placeholder=\"Bank/Wallet details\" class=\"col-span-2 px-3 py-2 rounded bg-white/10 outline-none\"/>
        </div>
        <button id=\"wd_btn\" class=\"btn bg-rose-400 text-slate-900\">Request Withdraw</button>
        <p class=\"text-xs text-slate-400\">Funds are held immediately and released after admin approval.</p>
      </div>

      <div>
        <h3 class=\"font-semibold mb-2\">Recent Activity</h3>
        <div id=\"history\" class=\"space-y-2 text-sm\"></div>
      </div>
    </section>

    <footer class=\"text-center text-xs text-slate-500\">Tapify WebApp • v1.2</footer>
  </div>

<script>
const qs = new URLSearchParams(location.search);
const CHAT_ID = qs.get('chat_id');
const USERNAME = qs.get('username') || '';
if (!CHAT_ID) alert('Missing chat_id. Please open from the Telegram button.');


const panels = {
  tap: document.getElementById('panel_tap'),
  aviator: document.getElementById('panel_aviator'),
  walk: document.getElementById('panel_walk'),
  wallet: document.getElementById('panel_wallet'),
};
function showPanel(name) {
  for (const k in panels) panels[k].classList.add('hidden');
  panels[name].classList.remove('hidden');
  document.getElementById('tab_tap').classList.toggle('bg-white/10', name==='tap');
  document.getElementById('tab_aviator').classList.toggle('bg-white/10', name==='aviator');
  document.getElementById('tab_walk').classList.toggle('bg-white/10', name==='walk');
  document.getElementById('tab_wallet').classList.toggle('bg-white/10', name==='wallet');
}

document.getElementById('tab_tap').onclick = () => showPanel('tap');
document.getElementById('tab_aviator').onclick = () => showPanel('aviator');
document.getElementById('tab_walk').onclick = () => showPanel('walk');
document.getElementById('tab_wallet').onclick = () => { showPanel('wallet'); loadHistory(); };

const usdEl = document.getElementById('usd');
const ngnEl = document.getElementById('ngn');
const walkRateEl = document.getElementById('walk_rate');
const totalStepsEl = document.getElementById('total_steps');

async function fetchUser() {
  const r = await fetch(`/api/user?chat_id=${encodeURIComponent(CHAT_ID)}&username=${encodeURIComponent(USERNAME)}`);
  const j = await r.json();
  if (j.balance_usd) {
    usdEl.textContent = `$${j.balance_usd}`;
    ngnEl.textContent = `₦${j.balance_ngn}`;
    walkRateEl.textContent = `$${j.walk_rate}`;
    totalStepsEl.textContent = j.total_steps;
  }
}

// Tap
let tapCountBatch = 0;
const tapBtn = document.getElementById('tap_btn');
const MAX_TAP = {{max_tap}};
function flushTaps() {

  if (tapCountBatch <= 0) return;
  const count = tapCountBatch; tapCountBatch = 0;
  fetch(`/api/tap?chat_id=${encodeURIComponent(CHAT_ID)}`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ count }) })
    .then(()=>fetchUser());
}
 tapBtn.addEventListener('click', ()=>{ tapCountBatch++; if (tapCountBatch>=MAX_TAP) flushTaps(); tapBtn.classList.add('scale-95');
setTimeout(()=>tapBtn.classList.remove('scale-95'),80);});
 setInterval(flushTaps, 1200);

// Aviator
let currentRoundId = null; let aviatorTimer = null;
const betInput = document.getElementById('bet_input');
const betBtn = document.getElementById('bet_btn');
const multText = document.getElementById('mult_text');
const statusText = document.getElementById('status_text');
const cashoutBtn = document.getElementById('cashout_btn');

betBtn.onclick = async () => {
  const bet = parseFloat(betInput.value||'0').toFixed(2);
  const r = await fetch(`/api/aviator/start?chat_id=${encodeURIComponent(CHAT_ID)}`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ bet }) });
  const j = await r.json(); if (!j.ok) { alert(j.error||'Error'); return; }
  currentRoundId = j.round_id; statusText.textContent='Round started — watch the multiplier!'; cashoutBtn.disabled=false;
  if (aviatorTimer) clearInterval(aviatorTimer);
  aviatorTimer = setInterval(async ()=>{
    const s = await fetch(`/api/aviator/state?chat_id=${encodeURIComponent(CHAT_ID)}&round_id=${currentRoundId}`);
    const sj = await s.json(); if (!sj.ok) return;
    multText.textContent = `${sj.current_multiplier}×`;
    if (sj.status==='crashed'){ statusText.textContent='💥 Crashed!'; cashoutBtn.disabled=true; clearInterval(aviatorTimer); aviatorTimer=null; fetchUser(); }
  }, 200);
  fetchUser();
};

cashoutBtn.onclick = async () => {
  if (!currentRoundId) return;
  const r = await fetch(`/api/aviator/cashout?chat_id=${encodeURIComponent(CHAT_ID)}`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ round_id: currentRoundId }) });

  const j = await r.json(); if (!j.ok) { alert(j.error||'Cashout failed'); return; }
  statusText.textContent = `✅ Cashed at ${j.multiplier}× — +$${j.payout_usd}`; cashoutBtn.disabled=true;
  if (aviatorTimer) { clearInterval(aviatorTimer); aviatorTimer=null; }
  fetchUser();
};

// Walk
let sessionSteps = 0; const sessionEl = document.getElementById('session_steps');
const startWalk = document.getElementById('start_walk'); const stopWalk = document.getElementById('stop_walk');
const sendSteps = document.getElementById('send_steps'); const manualSteps = document.getElementById('manual_steps'); const addManual = document.getElementById('add_manual');
let motionListener = null; let lastMagnitude = null; let stepThreshold = 1.2;
startWalk.onclick = async ()=>{ if (typeof DeviceMotionEvent!=='undefined' && typeof DeviceMotionEvent.requestPermission==='function') { try{ await DeviceMotionEvent.requestPermission(); }catch(e){} }
  if (motionListener) return; motionListener=(e)=>{ const ax=e.accelerationIncludingGravity.x||0, ay=e.accelerationIncludingGravity.y||0, az=e.accelerationIncludingGravity.z||0; const mag=Math.sqrt(ax*ax+ay*ay+az*az); if (lastMagnitude===null) lastMagnitude=mag; const d=Math.abs(mag-lastMagnitude); if (d>stepThreshold){ sessionSteps+=1; sessionEl.textContent=sessionSteps; } lastMagnitude=mag; };
window.addEventListener('devicemotion', motionListener); };
stopWalk.onclick = ()=>{ if (motionListener){ window.removeEventListener('devicemotion', motionListener); motionListener=null; lastMagnitude=null; } };
addManual.onclick = ()=>{ const val=parseInt(manualSteps.value||'0'); if (val>0){ sessionSteps+=val; sessionEl.textContent=sessionSteps; manualSteps.value=''; } };
sendSteps.onclick = async ()=>{ if (sessionSteps<=0){ alert('No steps to send'); return; } const r=await fetch(`/api/steps?chat_id=${encodeURIComponent(CHAT_ID)}`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ steps: sessionSteps }) }); const j=await r.json(); if(!j.ok){ alert(j.error||'Error'); return; } sessionSteps=0; sessionEl.textContent='0'; fetchUser(); };

// Upgrades modal
const upgradeBtn=document.getElementById('upgrade_btn');
upgradeBtn?.addEventListener('click',
()=>document.getElementById('upgrade_modal').showModal());
document.getElementById('close_upgrade').onclick =
()=>document.getElementById('upgrade_modal').close();
document.getElementById('confirm_upgrade').onclick = async ()=>{ const target=parseInt(document.getElementById('target_level').value||'0'); if(!target||target<2){ alert('Enter target level 2-4'); return; } const r=await fetch(`/api/upgrade?chat_id=${encodeURIComponent(CHAT_ID)}`, { method:'POST',
headers:{'Content-Type':'application/json'}, body: JSON.stringify({ target_level: target }) }); const j=await r.json(); if(!j.ok){ alert(j.error||'Upgrade failed'); return; } document.getElementById('upgrade_modal').close(); fetchUser(); };

// Wallet
async function loadHistory(){ const r=await fetch(`/api/transactions?chat_id=${encodeURIComponent(CHAT_ID)}`); const j=await r.json(); const box=document.getElementById('history'); box.innerHTML=''; if(!j.ok) return; for (const t of j.items){ const row=document.createElement('div'); row.className='flex items-center justify-between bg-white/5 rounded px-3 py-2'; const amt=(parseFloat(t.amount_usd)>=0?'+$':'-$')+Math.abs(parseFloat(t.amount_usd)).toFixed(2); row.innerHTML=`<div><div class=\"font-semibold\">${t.type} <span class=\"text-xs text-slate-400\">#${t.id}</span></div><div class=\"text-xs text-slate-400\">${new Date(t.created_at).toLocaleString()}</div></div><div class=\"text-right\"><div class=\"${parseFloat(t.amount_usd)>=0?'text-emerald-300':'text-rose-300'}\">${amt}</div><div class=\"text-xs text-slate-400\">${t.status}</div></div>`; box.appendChild(row); } }

document.getElementById('dep_btn').onclick = async ()=>{ const amount=parseFloat(document.getElementById('dep_amount').value||'0').toFixed(2); const reference=document.getElementById('dep_ref').value||''; const r=await fetch(`/api/deposit?chat_id=${encodeURIComponent(CHAT_ID)}`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ amount, method:'manual', reference }) }); const j=await r.json(); if(!j.ok){ alert(j.error||'Error'); return; } alert(`Deposit submitted. Ticket #${j.tx_id}`); loadHistory(); };

document.getElementById('wd_btn').onclick = async ()=>{ const amount=parseFloat(document.getElementById('wd_amount').value||'0').toFixed(2); const payout=document.getElementById('wd_payout').value||''; const r=await fetch(`/api/withdraw?chat_id=${encodeURIComponent(CHAT_ID)}`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ amount, payout }) }); const j=await r.json(); if(!j.ok){ alert(j.error||'Error'); return; } alert(`Withdrawal requested. Ticket #${j.tx_id}`); fetchUser(); loadHistory(); };


// Init
fetchUser(); showPanel('tap');
</script>

</body>
</html>
"""

@app.get("/")
def index():

    _ = get_or_create_user_from_query()
    return render_template_string(
        BASE_HTML,
        tap_reward=f"{TAP_REWARD}",
        max_tap=MAX_TAP_PER_REQUEST,
        user=user,
    )


@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
