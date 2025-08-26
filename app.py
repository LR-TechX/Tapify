#Web edit version....

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

# Aviator
AVIATOR_GROWTH_PER_SEC = Decimal("0.25")
MIN_BET = Decimal("1000.00")       # USD
MAX_BET = Decimal("1000000.00")    # USD

# Wallet
MIN_DEPOSIT_NGN = Decimal("100.00")   # ₦ minimum deposit
MIN_WITHDRAW_USD = Decimal("50.00")   # $ minimum withdrawal

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
    walk_rate = db.Column(db.Numeric(18, 4), default=Decimal("0.001"))  # $/step
    total_steps = db.Column(db.BigInteger, default=0)

    # Daily cap tracking for Walk
    steps_credited_on = db.Column(db.Date)
    steps_usd_today   = db.Column(db.Numeric(18, 2), default=Decimal("0.00"))

    created_at = db.Column(db.DateTime(timezone=True), default=now_utc)

    def ensure_defaults(self):
        changed = False
        if self.balance_usd is None:
            self.balance_usd = Decimal("0.00"); changed = True
        if self.balance_ngn is None:
            self.balance_ngn = Decimal("0.00"); changed = True
        if not self.walk_level:
            self.walk_level = 1; changed = True
        if not self.walk_rate:
            self.walk_rate = Decimal("0.001"); changed = True
        if self.total_steps is None:
            self.total_steps = 0; changed = True
        if self.steps_credited_on is None:
            self.steps_credited_on = date.today(); changed = True
        if self.steps_usd_today is None:
            self.steps_usd_today = Decimal("0.00"); changed = True
        return changed


class Transaction(db.Model):
    __tablename__ = "transactions"
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.BigInteger, db.ForeignKey("users.chat_id"), nullable=False)
    type = db.Column(db.String(64), nullable=False)  # tap, walk, aviator_bet, aviator_cashout, upgrade, deposit, withdraw, withdraw_revert
    status = db.Column(db.String(32), default="approved")  # pending|approved|completed|rejected
    amount_usd = db.Column(db.Numeric(18, 2), nullable=False, default=Decimal("0.00"))
    amount_ngn = db.Column(db.Numeric(18, 2))
    external_ref = db.Column(db.String(128), unique=True)  # Paystack reference/id
    meta = db.Column(db.JSON)
    created_at = db.Column(db.DateTime(timezone=True), default=now_utc)


class WithdrawalRequest(db.Model):
    __tablename__ = "withdrawal_requests"
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.BigInteger, db.ForeignKey("users.chat_id"), nullable=False)
    amount_usd = db.Column(db.Numeric(18, 2), nullable=False)
    amount_ngn = db.Column(db.Numeric(18, 2), nullable=False)
    status = db.Column(db.String(20), default="pending")  # pending|approved|rejected
    created_at = db.Column(db.DateTime(timezone=True), default=now_utc)


class AviatorRound(db.Model):
    __tablename__ = "aviator_rounds"
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.BigInteger, db.ForeignKey("users.chat_id"), nullable=False)
    bet_usd = db.Column(db.Numeric(18, 2), nullable=False)
    start_time = db.Column(db.DateTime(timezone=True), nullable=False)
    crash_multiplier = db.Column(db.Numeric(18, 2), nullable=False)
    growth_per_sec = db.Column(db.Numeric(18, 2), nullable=False, default=AVIATOR_GROWTH_PER_SEC)

    status = db.Column(db.String(32), default="active")  # active|cashed|crashed
    cashout_multiplier = db.Column(db.Numeric(18, 2))
    cashout_time = db.Column(db.DateTime(timezone=True))
    profit_usd = db.Column(db.Numeric(18, 2))


with app.app_context():
    db.create_all()

# =========================
# Auto-migrations (idempotent)
# =========================
def run_migrations():
    def safe_exec(statement, label=""):
        try:
            db.session.execute(text(statement))
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"Migration warning ({label}):", e)

    with app.app_context():
        # --- USERS TABLE ---
        safe_exec("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id BIGINT PRIMARY KEY,
            username TEXT
        )
        """, "users base")

        user_alters = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS balance_usd NUMERIC(18,2) DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS balance_ngn NUMERIC(18,2) DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS walk_level INT DEFAULT 1",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS walk_rate NUMERIC(18,4) DEFAULT 0.01",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_steps BIGINT DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()"
        ]
        for stmt in user_alters:
            safe_exec(stmt, "users alter")

        # --- AVIATOR_ROUNDS TABLE ---
        safe_exec("""
        CREATE TABLE IF NOT EXISTS aviator_rounds (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT REFERENCES users(chat_id),
            start_time TIMESTAMPTZ DEFAULT NOW()
        )
        """, "aviator_rounds base")

        aviator_alters = [
            "ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS bet_usd NUMERIC(18,2)",
            "ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS crash_multiplier NUMERIC(18,2)",
            "ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS growth_per_sec NUMERIC(18,4)",
            "ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS status TEXT",
            "ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS cashout_multiplier NUMERIC(18,2)",
            "ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS cashout_time TIMESTAMPTZ",
            "ALTER TABLE aviator_rounds ADD COLUMN IF NOT EXISTS profit_usd NUMERIC(18,2)"
        ]
        for stmt in aviator_alters:
            safe_exec(stmt, "aviator_rounds alter")

        # --- TRANSACTIONS TABLE ---
        safe_exec("""
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT REFERENCES users(chat_id),
            amount_usd NUMERIC(18,2),
            amount_ngn NUMERIC(18,2),
            type TEXT,
            status TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """, "transactions base")

        # Ensure external_ref exists
        safe_exec("""
        ALTER TABLE transactions ADD COLUMN IF NOT EXISTS external_ref TEXT
        """, "transactions external_ref")

        # Then create index
        safe_exec("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_external_ref
        ON transactions (external_ref) WHERE external_ref IS NOT NULL
        """, "transactions ext_ref idx")

        # --- WITHDRAWAL_REQUESTS TABLE ---
        safe_exec("""
        CREATE TABLE IF NOT EXISTS withdrawal_requests (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT REFERENCES users(chat_id),
            amount_usd NUMERIC(18,2) NOT NULL,
            amount_ngn NUMERIC(18,2) NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """, "withdrawal_requests base")

run_migrations()

# =========================
# Balance / Ledger helpers
# =========================
def recalc_balance_from_ledger(user: User) -> Decimal:
    """Sum all tx with status in ('pending','approved','completed')."""
    rows = db.session.execute(text("""
        SELECT COALESCE(SUM(amount_usd), 0) AS total
        FROM transactions
        WHERE chat_id = :chat_id
          AND status IN ('pending','approved','completed')
    """), {"chat_id": user.chat_id}).fetchone()
    total = Decimal(str(rows.total or "0"))
    user.balance_usd = to_cents(total)
    user.balance_ngn = to_cents(user.balance_usd * USD_TO_NGN)
    db.session.commit()
    return user.balance_usd

def sync_user_balance(user: User, force_recalc: bool = False):
    """Optionally recompute from ledger; otherwise just clamp >= 0."""
    if force_recalc:
        recalc_balance_from_ledger(user)
    else:
        if user.balance_usd is None or user.balance_usd < 0:
            user.balance_usd = Decimal("0.00")
        if user.balance_ngn is None or user.balance_ngn < 0:
            user.balance_ngn = Decimal("0.00")
        db.session.commit()

def add_tx(user: User, t_type: str, usd: Decimal, ngn: Decimal | None = None,
           status: str = "approved", meta: dict | None = None,
           external_ref: str | None = None, affect_balance: bool = True) -> 'Transaction':
    usd = to_cents(usd)
    ngn = to_cents(ngn) if ngn is not None else None
    tx = Transaction(
        chat_id=user.chat_id,
        type=t_type,
        status=status,
        amount_usd=usd,
        amount_ngn=ngn,
        external_ref=external_ref,
        meta=meta or {}
    )
    db.session.add(tx)
    if affect_balance:
        user.balance_usd = to_cents(Decimal(user.balance_usd) + usd)
        user.balance_ngn = to_cents(user.balance_usd * USD_TO_NGN)
    db.session.commit()
    return tx

# =========================
# User helper
# =========================
def get_or_create_user_from_query():
    chat_id = None
    username = None

    if request.is_json:
        body = request.get_json(silent=True) or {}
        chat_id = body.get("chat_id")
        username = body.get("username")

    if not chat_id:
        chat_id = request.args.get("chat_id") or request.headers.get("X-Chat-Id")
    if not username:
        username = request.args.get("username")

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
        db.session.add(user)
        db.session.commit()
        sync_user_balance(user)  # clamp to defaults
    else:
        if user.ensure_defaults():
            db.session.commit()
        # Optionally force-recalc: sync_user_balance(user, force_recalc=True)

    return user

# =========================
# Walk cap helpers
# =========================
BASE_DAILY_WALK_CAP_USD = Decimal("1.00")   # at level-1 (0.001 $/step)

def current_walk_cap_usd(user: User) -> Decimal:
    # Scale cap proportionally to rate (level 2 gets 2x, etc.)
    rate = Decimal(user.walk_rate or "0.001")
    scale = rate / Decimal("0.001")
    cap = BASE_DAILY_WALK_CAP_USD * scale
    return to_cents(cap)

def ensure_today_walk_counter(user: User):
    today = date.today()
    if user.steps_credited_on != today:
        user.steps_credited_on = today
        user.steps_usd_today = Decimal("0.00")
        db.session.commit()

# =========================
# Routes
# =========================
@app.route("/", methods=["GET", "HEAD"])
def index():
    # Health check & no chat_id landing for hosting
    if request.method == "HEAD" or not request.args.get("chat_id"):
        return "OK", 200

    user = get_or_create_user_from_query()
    return render_template_string(
        BASE_HTML,
        tap_reward=f"{TAP_REWARD}",
        max_tap=MAX_TAP_PER_REQUEST,
        username=user.username or user.chat_id,
    )

@app.get("/health")
def health():
    return {"ok": True, "time": now_utc().isoformat()}

# --- Profile ---
@app.get("/api/user")
def api_user():
    user = get_or_create_user_from_query()
    ensure_today_walk_counter(user)
    cap = current_walk_cap_usd(user)
    remaining = to_cents(cap - Decimal(user.steps_usd_today or 0))
    if remaining < 0:
        remaining = Decimal("0.00")
    return jsonify({
        "chat_id": user.chat_id,
        "username": user.username,
        "balance_usd": str(to_cents(user.balance_usd)),
        "balance_ngn": str(to_cents(user.balance_ngn)),
        "walk_level": user.walk_level,
        "walk_rate": str(to_cents(user.walk_rate)),
        "total_steps": int(user.total_steps or 0),
        "walk_cap_usd_today": str(cap),
        "walk_remaining_usd_today": str(remaining),
    })

# --- Tap ---
@app.post("/api/tap")
def api_tap():
    user = get_or_create_user_from_query()
    body = request.get_json(silent=True) or {}
    count = int(body.get("count", 1))
    count = max(1, min(count, MAX_TAP_PER_REQUEST))

    earn = to_cents(TAP_REWARD * Decimal(count))
    add_tx(user, "tap", usd=earn, ngn=earn * USD_TO_NGN, status="approved", meta={"count": count})
    return jsonify({"ok": True, "earned_usd": str(earn), "balance_usd": str(user.balance_usd), "balance_ngn": str(user.balance_ngn)})

# --- Walk & Earn ---
@app.post("/api/steps")
def api_steps():
    user = get_or_create_user_from_query()
    body = request.get_json(silent=True) or {}
    steps = int(body.get("steps", 0))
    if steps <= 0:
        return jsonify({"ok": False, "error": "steps must be positive"}), 400

    ensure_today_walk_counter(user)
    cap = current_walk_cap_usd(user)
    already = Decimal(user.steps_usd_today or 0)
    remaining_usd = to_cents(cap - already)
    if remaining_usd <= 0:
        return jsonify({"ok": True, "earned_usd": "0.00", "cap_reached": True, "balance_usd": str(user.balance_usd)})

    earn = to_cents(Decimal(user.walk_rate) * Decimal(steps))
    if earn > remaining_usd:
        earn = remaining_usd

    # Apply earnings
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
    add_tx(user, "aviator_bet", usd=-to_cents(bet), ngn=-to_cents(bet * USD_TO_NGN), status="approved", meta={})

    round_obj = AviatorRound(
        chat_id=user.chat_id,
        bet_usd=to_cents(bet),
        start_time=now_utc(),
        crash_multiplier=sample_crash_multiplier(),
        growth_per_sec=AVIATOR_GROWTH_PER_SEC,
        status="active",
    )
    db.session.add(round_obj)
    db.session.commit()

    return jsonify({"ok": True, "round_id": round_obj.id, "bet": str(round_obj.bet_usd)})

def _aviator_state(round_obj: AviatorRound):
    elapsed = Decimal(str((now_utc() - round_obj.start_time).total_seconds()))
    current_mult = Decimal("1.00") + (Decimal(round_obj.growth_per_sec) * elapsed)
    crashed = current_mult >= Decimal(round_obj.crash_multiplier)
    if crashed:
        current_mult = Decimal(round_obj.crash_multiplier)
    return current_mult.quantize(Decimal("0.01"), rounding=ROUND_DOWN), crashed

@app.get("/api/aviator/state")
def api_aviator_state():
    user = get_or_create_user_from_query()
    round_id = request.args.get("round_id")
    try:
        round_id = int(round_id)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid round id"}), 400

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
    try:
        round_id = int(round_id)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid round id"}), 400

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

    # Credit payout via ledger
    add_tx(user, "aviator_cashout", usd=payout, ngn=payout * USD_TO_NGN, status="approved", meta={"mult": str(mult)})

    r.status = "cashed"
    r.cashout_multiplier = mult
    r.cashout_time = now_utc()
    r.profit_usd = profit
    db.session.commit()

    return jsonify({"ok": True, "payout_usd": str(payout), "multiplier": str(mult), "balance_usd": str(user.balance_usd), "balance_ngn": str(user.balance_ngn)})

# --- Deposits (Paystack) ---
@app.post("/api/deposit")
def api_deposit_create():
    if not PAYSTACK_SECRET_KEY:
        abort(400, "Paystack not configured (missing PAYSTACK_SECRET_KEY)")

    user = get_or_create_user_from_query()
    body = request.get_json(silent=True) or {}
    # Client sends NGN amount (string/number)
    amount_ngn = Decimal(str(body.get("amount_ngn", "0")))
    if amount_ngn < MIN_DEPOSIT_NGN:
        return jsonify({"ok": False, "error": f"Minimum deposit is ₦{MIN_DEPOSIT_NGN}"}), 400

    payload = {
        "email": f"{user.username or user.chat_id}@tapify.local",
        "amount": int(to_cents(amount_ngn) * 100),  # kobo
        "metadata": {"chat_id": user.chat_id}
    }
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    r = requests.post("https://api.paystack.co/transaction/initialize", json=payload, headers=headers, timeout=30)
    res = r.json()

    if not res.get("status"):
        abort(500, res.get("message", "Paystack init failed"))

    # Client should open this URL to pay
    auth_url = res["data"]["authorization_url"]
    reference = res["data"]["reference"]

    # Optional: record a pending tx (NOT affecting balance) – useful for audit
    add_tx(user, "deposit", usd=Decimal("0.00"), ngn=amount_ngn, status="pending",
           meta={"init_ref": reference}, external_ref=None, affect_balance=False)

    return jsonify({"ok": True, "checkout_url": auth_url, "reference": reference})

@app.post("/api/webhook/paystack")
def paystack_webhook():
    # Verify signature
    raw = request.get_data()
    signature = request.headers.get("x-paystack-signature")
    if not paystack_signature_valid(raw, signature):
        abort(403, "Invalid signature")

    payload = request.get_json(silent=True) or {}
    event = payload.get("event")
    data = payload.get("data", {})

    if event == "charge.success":
        # Unique reference to prevent double-credit
        reference = data.get("reference")
        # If we've already processed this reference, ignore
        existing = Transaction.query.filter_by(external_ref=reference).first()
        if existing:
            return "OK", 200

        meta = data.get("metadata") or {}
        chat_id = meta.get("chat_id")
        user = User.query.get(chat_id)
        if not user:
            # If user not found, do nothing (or log)
            return "OK", 200

        amount_kobo = Decimal(str(data.get("amount", "0")))
        amount_ngn = to_cents(amount_kobo / Decimal("100"))
        usd = to_cents(amount_ngn / USD_TO_NGN)

        # Finalize deposit: credit balance & write ledger with external_ref
        add_tx(user, "deposit", usd=usd, ngn=amount_ngn, status="completed",
               meta={"paystack_id": data.get("id")}, external_ref=reference, affect_balance=True)

    return "OK", 200

# --- Withdrawals ---
@app.post("/api/withdraw")
def api_withdraw_request():
    user = get_or_create_user_from_query()
    body = request.get_json(silent=True) or {}
    amount_usd = Decimal(str(body.get("amount", "0")))
    payout = (body.get("payout") or "").strip()  # bank/wallet info

    if amount_usd < MIN_WITHDRAW_USD:
        return jsonify({"ok": False, "error": f"Minimum withdraw is ${MIN_WITHDRAW_USD}"}), 400
    if Decimal(user.balance_usd) < amount_usd:
        return jsonify({"ok": False, "error": "Insufficient balance"}), 400

    # Hold funds immediately
    hold = to_cents(amount_usd)
    add_tx(user, "withdraw", usd=-hold, ngn=-to_cents(hold * USD_TO_NGN),
           status="pending", meta={"payout": payout})

    req = WithdrawalRequest(
        chat_id=user.chat_id,
        amount_usd=hold,
        amount_ngn=to_cents(hold * USD_TO_NGN),
        status="pending"
    )
    db.session.add(req)
    db.session.commit()

    return jsonify({"ok": True, "message": "Withdrawal requested. Awaiting admin approval.", "request_id": req.id, "balance_usd": str(user.balance_usd)})

@app.post("/api/admin/withdraw/approve")
def admin_withdraw_approve():
    require_admin()
    body = request.get_json(silent=True) or {}
    req_id = body.get("request_id")
    req = WithdrawalRequest.query.get(req_id)
    if not req or req.status != "pending":
        abort(400, "Invalid request")

    user = User.query.get(req.chat_id)
    if not user:
        abort(400, "User not found")

    req.status = "approved"
    # Mark the matching pending tx as approved (no extra balance change)
    db.session.execute(text("""
        UPDATE transactions
           SET status = 'approved'
         WHERE chat_id = :chat_id
           AND type = 'withdraw'
           AND status = 'pending'
           AND amount_usd = :neg_amount
         ORDER BY id DESC
         LIMIT 1
    """), {"chat_id": user.chat_id, "neg_amount": -to_cents(req.amount_usd)})
    db.session.commit()

    return jsonify({"ok": True, "message": "Withdrawal approved", "request_id": req.id})

@app.post("/api/admin/withdraw/reject")
def admin_withdraw_reject():
    require_admin()
    body = request.get_json(silent=True) or {}
    req_id = body.get("request_id")
    reason = (body.get("reason") or "").strip()
    req = WithdrawalRequest.query.get(req_id)
    if not req or req.status != "pending":
        abort(400, "Invalid request")

    user = User.query.get(req.chat_id)
    if not user:
        abort(400, "User not found")

    # Refund the held amount
    add_tx(user, "withdraw_revert", usd=to_cents(req.amount_usd),
           ngn=to_cents(req.amount_usd * USD_TO_NGN), status="approved",
           meta={"request_id": req_id, "reason": reason})

    # Mark the original pending withdraw tx rejected
    db.session.execute(text("""
        UPDATE transactions
           SET status = 'rejected'
         WHERE chat_id = :chat_id
           AND type = 'withdraw'
           AND status = 'pending'
           AND amount_usd = :neg_amount
         ORDER BY id DESC
         LIMIT 1
    """), {"chat_id": user.chat_id, "neg_amount": -to_cents(req.amount_usd)})

    req.status = "rejected"
    db.session.commit()

    return jsonify({"ok": True, "message": "Withdrawal rejected & funds returned", "request_id": req.id})

# --- History ---
@app.get("/api/transactions")
def api_transactions():
    user = get_or_create_user_from_query()
    q = Transaction.query.filter_by(chat_id=user.chat_id).order_by(Transaction.id.desc()).limit(50).all()
    return jsonify({
        "ok": True,
        "items": [
            {
                "id": t.id,
                "type": t.type,
                "status": t.status,
                "amount_usd": str(to_cents(t.amount_usd)),
                "meta": t.meta or {},
                "ext": t.external_ref,
                "created_at": t.created_at.isoformat(),
            } for t in q
        ]
    }

# =========================
# UI (Tailwind single-file) — with requested fixes
# =========================
BASE_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <!-- Strict mobile viewport (fix) -->
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no" />
  <title>Tapify — WebApp</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet"/>
  <script>
    tailwind.config = {
      theme: { extend: { boxShadow: { 'soft': '0 10px 30px rgba(0,0,0,0.20)' } } }
    }
  </script>
  <style>
    body { font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, Noto Sans, Helvetica Neue, Arial; }
    /* Background (will be replaced dynamically with your GitHub image) */
    body {
      height: 100vh;
      margin: 0;
      overflow-x: hidden;
      position: relative;
      background: #111;
      background-size: cover;
      background-position: center;
      background-repeat: no-repeat;
    }
    /* A subtle animated gloss over background for depth */
    body::after {
      content: '';
      position: absolute;
      top: 0; left: 0;
      width: 200%; height: 200%;
      background: linear-gradient(120deg, rgba(255,50,50,0.15), rgba(255,0,0,0.1), rgba(128,0,0,0.2));
      background-size: 400% 400%;
      pointer-events: none;
      z-index: 0;
      animation: flow 10s ease-in-out infinite;
    }
    @keyframes flow { 0%{background-position:0% 0%} 50%{background-position:100% 50%} 100%{background-position:0% 0%} }

    /* Utility glass */
    .glass { backdrop-filter: blur(10px); background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.08); }

    /* Gold glow for balances */
    .gold-glow { text-shadow: 0 0 20px rgba(255,215,0,0.45), 0 0 40px rgba(255,215,0,0.25); }

    /* Coin image styles (replacing CSS coin) */
    #coin_img {
      width: 180px; height: 180px;
      border-radius: 9999px;
      box-shadow: inset 0 8px 18px rgba(255,255,255,0.25), inset 0 -10px 16px rgba(0,0,0,0.25), 0 18px 40px rgba(0,0,0,0.35);
      transition: transform .08s ease;
      animation: coinPulse 3.2s ease-in-out infinite;
      object-fit: contain;
      background: rgba(0,0,0,0.08);
    }
    .bounce { animation: coinBounce .25s ease; }
    @keyframes coinPulse { 0%,100%{transform:scale(1)} 50%{transform:scale(1.04)} }
    @keyframes coinBounce {
      0% { transform: scale(1) translateY(0); }
      50% { transform: scale(0.95) translateY(2px); }
      100% { transform: scale(1) translateY(0); }
    }

    /* Floating +N text */
    .floatText {
      position: absolute;
      left: 0; top: 0;
      transform: translate(-50%,-50%);
      font-size: 16px;
      color: #fff; font-weight: 800;
      pointer-events: none;
      text-shadow: 0 2px 8px rgba(0,0,0,0.45);
      animation: floatUp 800ms ease forwards;
      z-index: 5;
    }
    @keyframes floatUp { 0%{transform:translateY(0);opacity:1} 100%{transform:translateY(-40px);opacity:0} }

    /* Energy bar */
    .energy-wrap { position: relative; height: 14px; border-radius: 9999px; background: rgba(0,0,0,0.35); overflow: hidden; }
    .energy-fill { height: 100%; width: 0%; background: linear-gradient(90deg, #34d399, #f59e0b); box-shadow: inset 0 0 8px rgba(255,255,255,0.35); transition: width .25s ease; }
    .energy-gloss { position: absolute; inset: 0; background: linear-gradient(180deg, rgba(255,255,255,0.35), rgba(255,255,255,0)); pointer-events: none; }

    /* Aviator plane + board (kept styling) */
    .plane { width: 36px; height: 36px; border-radius: 6px; background: #ef4444; transform: rotate(35deg); box-shadow: 0 8px 20px rgba(239,68,68,0.45); position: absolute; top: 60%; left: 10%; }
    .plane.fly { animation: flyDiag 2s linear infinite; }
    @keyframes flyDiag { 0%{ transform:translate(0,0) rotate(35deg); opacity:.9 } 100%{ transform:translate(240px,-140px) rotate(35deg); opacity:1 } }
    .plane.crash { animation: crashFx 600ms ease forwards; }
    @keyframes crashFx { 0% { transform: rotate(35deg) scale(1); opacity:1 } 60% { transform: rotate(75deg) scale(0.9); opacity:.6; filter: blur(1px) } 100% { transform: rotate(120deg) scale(0.6); opacity:0; filter: blur(2px) } }

    /* Buttons + cards */
    .btn { border-radius: 9999px; padding: 0.75rem 1.2rem; font-weight: 800; }
    .card { border-radius: 1rem; box-shadow: 0 10px 30px rgba(0,0,0,0.25); }

    /* Hide tap highlight on mobile */
    * { -webkit-tap-highlight-color: transparent; }
  </style>
</head>
<body class="min-h-screen text-white">
  <div class="relative z-10 max-w-xl mx-auto p-4 space-y-4">
    <!-- Header / balance -->
    <header class="glass card p-4 flex items-center justify-between">
      <div class="flex items-center gap-3">
        <div class="text-2xl">🕹</div>
        <div>
          <h1 class="text-2xl font-extrabold">Tapify</h1>
          <p class="text-white/70 text-xs">Telegram Mini App • Mobile-first</p>
          <p id="tg_user" class="text-white/50 text-xs"></p>
        </div>
      </div>
      <div class="text-right">
        <div class="text-[10px] text-white/70">Total Balance</div>
        <div id="usd" class="text-2xl md:text-3xl font-black gold-glow">$0.00</div>
        <div id="ngn" class="text-xs text-white/75">₦0</div>
      </div>
    </header>

    <!-- Nav -->
    <nav class="glass card p-2 grid grid-cols-4 gap-2">
      <button id="tab_tap" class="btn text-sm bg-white/15 hover:bg-white/25">Tap</button>
      <button id="tab_aviator" class="btn text-sm hover:bg-white/15">Aviator</button>
      <button id="tab_walk" class="btn text-sm hover:bg-white/15">Walk</button>
      <button id="tab_wallet" class="btn text-sm hover:bg-white/15">Wallet</button>
    </nav>

    <!-- Panels -->
    <section id="panel_tap" class="glass card p-4 space-y-4">
      <div class="flex items-center justify-between">
        <div class="text-sm text-white/70">Energy</div>
        <div class="energy-wrap w-40">
          <div id="energyFill" class="energy-fill"></div>
          <div class="energy-gloss"></div>
        </div>
        <div id="energyLabel" class="text-xs text-white/70">0/0</div>
      </div>

      <div class="grid place-items-center py-2">
        <!-- Coin image (fix: using your GitHub image) -->
        <img id="coin_img" alt="Tapcoin" />
      </div>

      <div class="text-center text-white/70 text-xs">Tap the coin to earn</div>
    </section>

    <section id="panel_aviator" class="hidden glass card p-4 space-y-3 relative overflow-hidden">
      <div class="text-sm text-white/80">Aviator</div>
      <div class="relative h-40 glass card p-3 overflow-hidden">
        <div id="plane" class="plane"></div>
        <div class="absolute top-2 right-3 text-3xl font-black mult-glow" id="mult_text">1.00×</div>
        <div class="absolute bottom-2 left-3 text-xs text-white/70" id="status_text">Idle</div>
      </div>
      <div class="flex gap-2">
        <input id="bet_input" type="number" min="0.10" step="0.01" placeholder="Bet ($)" class="w-full rounded-lg bg-white/10 p-2 outline-none"/>
        <button id="bet_btn" class="btn bg-emerald-500/80 hover:bg-emerald-500">Bet</button>
        <button id="cashout_btn" class="btn bg-yellow-500/80 hover:bg-yellow-500" disabled>Cashout</button>
      </div>
      <div class="grid grid-cols-2 gap-3">
        <div>
          <div class="text-xs text-white/70 mb-1">Last Results</div>
          <div id="aviator_hist" class="text-sm flex gap-1 flex-wrap"></div>
        </div>
        <div>
          <div class="text-xs text-white/70 mb-1">Players</div>
          <div id="aviator_players" class="text-xs text-white/80"></div>
        </div>
      </div>
    </section>

    <section id="panel_walk" class="hidden glass card p-4 space-y-3">
      <div class="text-sm text-white/80">Walk & Earn</div>
      <div class="text-xs text-white/70">Level: <span id="walk_level">1</span> • Rate: <span id="walk_rate">0.001</span> $/step</div>
      <div class="flex gap-2">
        <select id="upgrade_target" class="w-full rounded-lg bg-white/10 p-2 outline-none">
          <option value="2">Upgrade to Lv2 ($2.00)</option>
          <option value="3">Upgrade to Lv3 ($5.00)</option>
          <option value="4">Upgrade to Lv4 ($12.00)</option>
        </select>
        <button id="upgrade_btn" class="btn bg-indigo-500/80 hover:bg-indigo-500">Upgrade</button>
      </div>
    </section>

    <section id="panel_wallet" class="hidden glass card p-4 space-y-3">
      <div class="grid md:grid-cols-2 gap-3">
        <div class="glass card p-3">
          <div class="text-sm text-white/80 mb-2">Deposit</div>
          <input id="dep_amount_ngn" type="number" min="100" step="1" placeholder="Amount (₦)" class="w-full rounded-lg bg-white/10 p-2 outline-none mb-2"/>
          <button id="dep_btn" class="btn bg-sky-500/80 hover:bg-sky-500 w-full">Paystack Checkout</button>
        </div>
        <div class="glass card p-3">
          <div class="text-sm text-white/80 mb-2">Withdraw</div>
          <input id="wd_amount" type="number" min="0.10" step="0.01" placeholder="Amount ($)" class="w-full rounded-lg bg-white/10 p-2 outline-none mb-2"/>
          <input id="wd_payout" type="text" placeholder="Payout handle/address" class="w-full rounded-lg bg-white/10 p-2 outline-none mb-2"/>
          <button id="wd_btn" class="btn bg-rose-500/80 hover:bg-rose-500 w-full">Request Withdraw</button>
        </div>
      </div>

      <div class="glass card p-3">
        <div class="text-sm text-white/80 mb-2">Recent Transactions</div>
        <div id="tx_box" class="space-y-2"></div>
      </div>
    </section>
  </div>

  <script>
    // === Apply your GitHub background image (fix) ===
    document.body.style.backgroundImage =
      "url('https://raw.githubusercontent.com/lr-techx/tapifymain/red-waves.png')";

    const CHAT_ID = "{{ chat_id }}";
    const NAME = "{{ username }}";
    const tgUserEl = document.getElementById('tg_user');
    tgUserEl.textContent = NAME ? ("@" + NAME) : ("ID: " + CHAT_ID);

    // Tabs
    const panels = {
      tap: document.getElementById('panel_tap'),
      aviator: document.getElementById('panel_aviator'),
      walk: document.getElementById('panel_walk'),
      wallet: document.getElementById('panel_wallet'),
    };
    function showPanel(key){
      for (const k in panels){ panels[k].classList.add('hidden'); }
      panels[key].classList.remove('hidden');
    }
    document.getElementById('tab_tap').onclick = ()=>showPanel('tap');
    document.getElementById('tab_aviator').onclick = ()=>showPanel('aviator');
    document.getElementById('tab_walk').onclick = ()=>showPanel('walk');
    document.getElementById('tab_wallet').onclick = ()=>showPanel('wallet');

    // Balances
    async function fetchUser(){
      const r = await fetch(`/api/user?chat_id=${encodeURIComponent(CHAT_ID)}`);
      const j = await r.json();
      if(!j.ok) return;
      document.getElementById('usd').textContent = '$' + Number(j.balance_usd).toFixed(2);
      document.getElementById('ngn').textContent = '₦' + Math.floor(Number(j.balance_ngn)).toLocaleString();
      document.getElementById('walk_level').textContent = j.walk_level;
      document.getElementById('walk_rate').textContent = j.walk_rate;
      setTapStrengthFromLevel(j.walk_level);
    }

    // ==== TAP ====
    const coin = document.getElementById('coin_img');
    // Use your provided GitHub coin image (fix)
    coin.src = "https://raw.githubusercontent.com/lr-techx/tapify/main/tapcoin.png";

    const energyFill = document.getElementById('energyFill');
    const energyLabel = document.getElementById('energyLabel');

    const MAX_TAP = 200;          // client batch cap
    let energyMax = 100;
    let energy = energyMax;
    let regenPerSecond = 3;       // FIX: reduced from 8 -> 3
    let tapStrength = 1;          // default; boosted at higher levels

    function setTapStrengthFromLevel(level){
      tapStrength = (level >= 3) ? 10 : 1;
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

    function spawnFloat(x, y, text) {
      const el = document.createElement('div');
      el.className = 'floatText';
      el.textContent = '+' + text;
      el.style.left = x + 'px';
      el.style.top = y + 'px';
      document.body.appendChild(el);
      setTimeout(()=>el.remove(), 820);
    }

    coin.addEventListener('click', (e)=>{
      if (energy < tapStrength) return;
      energy -= tapStrength;
      updateEnergyUI();

      coin.classList.add('bounce');
      setTimeout(()=>coin.classList.remove('bounce'), 240);

      const rect = coin.getBoundingClientRect();
      const cx = (e.clientX || (rect.left + rect.width/2));
      const cy = (e.clientY || (rect.top + rect.height/2));
      spawnFloat(cx, cy, tapStrength);

      tapCountBatch += tapStrength;
      if (tapCountBatch >= MAX_TAP) flushTaps();
    });

    // ==== AVIATOR ====
    let currentRoundId = null;
    let aviatorTimer = null;
    const betInput = document.getElementById('bet_input');
    const betBtn = document.getElementById('bet_btn');
    const multText = document.getElementById('mult_text');
    const statusText = document.getElementById('status_text');
    const cashoutBtn = document.getElementById('cashout_btn');
    const plane = document.getElementById('plane');
    const histBox = document.getElementById('aviator_hist');
    const playersBox = document.getElementById('aviator_players');

    const lastResults = [];
    function addHistory(mult){
      lastResults.unshift(parseFloat(mult));
      if (lastResults.length > 20) lastResults.pop();
      histBox.innerHTML = '';
      for (const m of lastResults){
        const tag = document.createElement('span');
        tag.className = 'px-2 py-1 rounded-lg bg-white/10';
        tag.textContent = m.toFixed(2) + '×';
        histBox.appendChild(tag);
      }
    }

    function startAviatorUI(){
      statusText.textContent = 'Flying…';
      plane.classList.remove('crash');
      plane.classList.add('fly');
      cashoutBtn.disabled = false;
    }

    function stopAviatorUI(crashed){
      plane.classList.remove('fly');
      if (crashed){
        plane.classList.add('crash');
        statusText.textContent = 'Crashed';
      } else {
        statusText.textContent = 'Cashed out';
      }
      cashoutBtn.disabled = true;
    }

    async function pollRound(){
      if (!currentRoundId) return;
      const r = await fetch(`/api/aviator/state?chat_id=${encodeURIComponent(CHAT_ID)}&round_id=${currentRoundId}`);
      const j = await r.json();
      if (!j.ok) return;
      multText.textContent = (parseFloat(j.current_multiplier||"1.00")).toFixed(2) + '×';
      if (j.status === 'crashed'){
        addHistory(parseFloat(j.current_multiplier));
        stopAviatorUI(true);
        clearInterval(aviatorTimer); aviatorTimer = null; currentRoundId = null;
      }
    }

    betBtn.onclick = async ()=>{
      const bet = parseFloat(betInput.value || '0');
      if (!bet || bet < 0.10) { alert('Min bet is $0.10'); return; }
      const r = await fetch(`/api/aviator/start?chat_id=${encodeURIComponent(CHAT_ID)}`, {
        method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ bet })
      });
      const j = await r.json();
      if (!j.ok){ alert(j.error||'Error'); return; }
      currentRoundId = j.round_id;
      startAviatorUI();
      if (aviatorTimer) clearInterval(aviatorTimer);
      aviatorTimer = setInterval(pollRound, 400);
    };

    cashoutBtn.onclick = async ()=>{
      if (!currentRoundId) return;
      const r = await fetch(`/api/aviator/cashout?chat_id=${encodeURIComponent(CHAT_ID)}`, {
        method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ round_id: currentRoundId })
      });
      const j = await r.json();
      if (!j.ok){ alert(j.error||'Error'); return; }
      addHistory(parseFloat(j.multiplier||"1.00"));
      stopAviatorUI(false);
      clearInterval(aviatorTimer); aviatorTimer = null; currentRoundId = null;
      fetchUser();
    };

    // ==== WALK ====
    document.getElementById('upgrade_btn').onclick = async ()=>{
      const target = parseInt(document.getElementById('upgrade_target').value, 10);
      const r = await fetch(`/api/walk/upgrade?chat_id=${encodeURIComponent(CHAT_ID)}`, {
        method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ target_level: target })
      });
      const j = await r.json();
      if(!j.ok){ alert(j.error||'Error'); return; }
      document.getElementById('walk_level').textContent = j.walk_level;
      document.getElementById('walk_rate').textContent = j.walk_rate;
      fetchUser();
    };

    // ==== WALLET ====
    async function loadHistory(){
      const r = await fetch(`/api/transactions?chat_id=${encodeURIComponent(CHAT_ID)}`);
      const j = await r.json();
      if(!j.ok) return;
      const box = document.getElementById('tx_box');
      box.innerHTML = '';
      for (const t of j.items){
        const amt = Number(t.amount_usd).toFixed(2);
        const row = document.createElement('div');
        row.className = 'p-2 rounded-lg bg-white/5 flex items-center justify-between';
        row.innerHTML = `
          <div class="text-xs">
            <div class="font-semibold">${t.type}</div>
            <div class="text-white/60">${new Date(t.created_at).toLocaleString()}</div>
          </div>
          <div class="text-right">
            <div class="font-bold ${parseFloat(amt)>=0?'text-emerald-200':'text-rose-200'}">${amt}</div>
            <div class="text-xs text-white/60">${t.status}</div>
          </div>`;
        box.appendChild(row);
      }
    }

    document.getElementById('dep_btn').onclick = async ()=>{
      const amount_ngn = parseFloat(document.getElementById('dep_amount_ngn').value||'0');
      if (!amount_ngn || amount_ngn < 100) { alert('Minimum deposit is ₦100'); return; }
      const r = await fetch(`/api/deposit?chat_id=${encodeURIComponent(CHAT_ID)}`, {
        method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ amount_ngn })
      });
      const j = await r.json();
      if(!j.ok){ alert(j.error||'Deposit init failed'); return; }
      window.open(j.checkout_url, '_blank');
    };

    document.getElementById('wd_btn').onclick = async ()=>{
      const amount = parseFloat(document.getElementById('wd_amount').value||'0').toFixed(2);
      const payout = document.getElementById('wd_payout').value||'';
      const r = await fetch(`/api/withdraw?chat_id=${encodeURIComponent(CHAT_ID)}`, {
        method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ amount, payout })
      });
      const j = await r.json();
      if(!j.ok){ alert(j.error||'Error'); return; }
      alert(\`Withdrawal requested. Ticket #\${j.request_id}\`);
      fetchUser(); loadHistory();
    };

    // Init
    fetchUser(); loadHistory(); showPanel('tap');
  </script>
</body>
</html>
"""

# =========================
# Run
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
