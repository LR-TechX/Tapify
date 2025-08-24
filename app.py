import os
import random
from decimal import Decimal
from datetime import datetime, timezone

from flask import Flask, request, jsonify, abort, render_template_string
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

# ====================================================
# Flask & Database setup
# ====================================================
app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/mydb")
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ====================================================
# Database Models
# ====================================================

class User(db.Model):
    __tablename__ = "users"
    chat_id = db.Column(db.BigInteger, primary_key=True)
    username = db.Column(db.String(128))

    balance_usd = db.Column(db.Numeric(18, 2), default=Decimal("0.00"))
    balance_ngn = db.Column(db.Numeric(18, 2), default=Decimal("0.00"))
    walk_level = db.Column(db.Integer, default=1)
    walk_rate = db.Column(db.Numeric(18, 4), default=Decimal("0.01"))
    total_steps = db.Column(db.BigInteger, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AviatorRound(db.Model):
    __tablename__ = "aviator_rounds"
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.BigInteger, db.ForeignKey("users.chat_id"))
    bet_usd = db.Column(db.Numeric(18, 2))
    start_time = db.Column(db.DateTime, default=datetime.utcnow)
    crash_multiplier = db.Column(db.Numeric(18, 2))
    growth_per_sec = db.Column(db.Numeric(18, 4))
    status = db.Column(db.String(20))
    cashout_multiplier = db.Column(db.Numeric(18, 2))
    cashout_time = db.Column(db.DateTime)
    profit_usd = db.Column(db.Numeric(18, 2))

# ====================================================
# Auto-Migration (Users + AviatorRounds)
# ====================================================
with app.app_context():
    # Create users table if not exists
    db.session.execute(text("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id BIGINT PRIMARY KEY,
        username TEXT
    )
    """))
    db.session.commit()

    # Alter users table to add game columns
    user_alters = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS balance_usd NUMERIC(18,2) DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS balance_ngn NUMERIC(18,2) DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS walk_level INT DEFAULT 1",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS walk_rate NUMERIC(18,4) DEFAULT 0.01",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_steps BIGINT DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()"
    ]
    for stmt in user_alters:
        try:
            db.session.execute(text(stmt))
            db.session.commit()
        except Exception as e:
            print("Migration warning (users):", e)

    # Create aviator_rounds table if not exists
    db.session.execute(text("""
    CREATE TABLE IF NOT EXISTS aviator_rounds (
        id SERIAL PRIMARY KEY,
        chat_id BIGINT REFERENCES users(chat_id),
        start_time TIMESTAMPTZ DEFAULT NOW()
    )
    """))
    db.session.commit()

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
        try:
            db.session.execute(text(stmt))
            db.session.commit()
        except Exception as e:
            print("Migration warning (aviator_rounds):", e)

# ====================================================
# Helper: Get or Create User
# ====================================================
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

    if not username:
        username = f"user_{chat_id}"

    user = User.query.filter_by(chat_id=chat_id).first()
    if not user:
        user = User(chat_id=chat_id, username=username)
        db.session.add(user)
        db.session.commit()
    return user

# ====================================================
# Routes
# ====================================================

# Health check + WebApp UI
@app.get("/")
def index():
    if not request.args and not request.is_json:
        return "OK", 200  # Render health check

    user = get_or_create_user_from_query()
    BASE_HTML = """
    <html>
      <head><title>Game WebApp</title></head>
      <body>
        <h1>Welcome, {{username}}</h1>
        <p>Tap, Walk, and Fly Aviator!</p>
      </body>
    </html>
    """
    return render_template_string(
        BASE_HTML,
        username=user.username or user.chat_id
    )

# --- Tap Game ---
TAP_REWARD = Decimal("0.05")

@app.post("/api/tap")
def api_tap():
    user = get_or_create_user_from_query()
    user.balance_usd += TAP_REWARD
    db.session.commit()
    return jsonify({
        "message": "Tapped!",
        "reward_usd": str(TAP_REWARD),
        "balance_usd": str(user.balance_usd)
    })

# --- Steps / Walk Game ---
@app.post("/api/steps")
def api_steps():
    user = get_or_create_user_from_query()
    data = request.get_json(silent=True) or {}
    steps = int(data.get("steps", 0))

    if steps <= 0:
        abort(400, "Steps must be > 0")

    user.total_steps += steps
    reward = Decimal(str(steps)) * user.walk_rate
    user.balance_usd += reward
    db.session.commit()

    return jsonify({
        "message": "Steps logged",
        "steps": steps,
        "reward_usd": str(reward.quantize(Decimal("0.01"))),
        "balance_usd": str(user.balance_usd)
    })

# --- Aviator Game ---
@app.post("/api/aviator/start")
def api_aviator_start():
    user = get_or_create_user_from_query()
    data = request.get_json(silent=True) or {}
    bet_usd = Decimal(str(data.get("bet_usd", "0")))

    if bet_usd <= 0:
        abort(400, "Bet must be > 0")
    if user.balance_usd < bet_usd:
        abort(400, "Insufficient balance")

    round = AviatorRound(
        chat_id=user.chat_id,
        bet_usd=bet_usd,
        start_time=datetime.now(timezone.utc),
        crash_multiplier=Decimal(str(random.uniform(1.5, 5.0))).quantize(Decimal("0.01")),
        growth_per_sec=Decimal("0.25"),
        status="active"
    )

    db.session.add(round)
    user.balance_usd -= bet_usd  # Deduct bet
    db.session.commit()

    return jsonify({
        "message": "Round started",
        "round_id": round.id,
        "balance_usd": str(user.balance_usd)
    })

@app.post("/api/aviator/cashout")
def api_aviator_cashout():
    user = get_or_create_user_from_query()
    data = request.get_json(silent=True) or {}
    round_id = data.get("round_id")

    round = AviatorRound.query.filter_by(id=round_id, chat_id=user.chat_id).first()
    if not round or round.status != "active":
        abort(400, "Round not active or invalid")

    round.cashout_multiplier = Decimal(str(data.get("multiplier", "1.0")))
    round.cashout_time = datetime.now(timezone.utc)
    round.status = "cashed_out"
    round.profit_usd = (round.bet_usd * round.cashout_multiplier).quantize(Decimal("0.01"))

    user.balance_usd += round.profit_usd  # Add winnings
    db.session.commit()

    return jsonify({
        "message": "Cashed out",
        "profit_usd": str(round.profit_usd),
        "balance_usd": str(user.balance_usd)
    })

# --- Stats ---
@app.get("/stats")
def stats():
    user = get_or_create_user_from_query()
    return jsonify({
        "balance_usd": str(user.balance_usd),
        "balance_ngn": str(user.balance_ngn),
        "walk_level": user.walk_level,
        "walk_rate": str(user.walk_rate),
        "total_steps": user.total_steps
    })

# ====================================================
# Run
# ====================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)import os
import random
from decimal import Decimal
from datetime import datetime, timezone

from flask import Flask, request, jsonify, abort, render_template_string
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

# ====================================================
# Flask & Database setup
# ====================================================
app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/mydb")
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ====================================================
# Database Models
# ====================================================

class User(db.Model):
    __tablename__ = "users"
    chat_id = db.Column(db.BigInteger, primary_key=True)
    username = db.Column(db.String(128))

    balance_usd = db.Column(db.Numeric(18, 2), default=Decimal("0.00"))
    balance_ngn = db.Column(db.Numeric(18, 2), default=Decimal("0.00"))
    walk_level = db.Column(db.Integer, default=1)
    walk_rate = db.Column(db.Numeric(18, 4), default=Decimal("0.01"))
    total_steps = db.Column(db.BigInteger, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AviatorRound(db.Model):
    __tablename__ = "aviator_rounds"
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.BigInteger, db.ForeignKey("users.chat_id"))
    bet_usd = db.Column(db.Numeric(18, 2))
    start_time = db.Column(db.DateTime, default=datetime.utcnow)
    crash_multiplier = db.Column(db.Numeric(18, 2))
    growth_per_sec = db.Column(db.Numeric(18, 4))
    status = db.Column(db.String(20))
    cashout_multiplier = db.Column(db.Numeric(18, 2))
    cashout_time = db.Column(db.DateTime)
    profit_usd = db.Column(db.Numeric(18, 2))

# ====================================================
# Auto-Migration (Users + AviatorRounds)
# ====================================================
with app.app_context():
    # Create users table if not exists
    db.session.execute(text("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id BIGINT PRIMARY KEY,
        username TEXT
    )
    """))
    db.session.commit()

    # Alter users table to add game columns
    user_alters = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS balance_usd NUMERIC(18,2) DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS balance_ngn NUMERIC(18,2) DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS walk_level INT DEFAULT 1",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS walk_rate NUMERIC(18,4) DEFAULT 0.01",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_steps BIGINT DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()"
    ]
    for stmt in user_alters:
        try:
            db.session.execute(text(stmt))
            db.session.commit()
        except Exception as e:
            print("Migration warning (users):", e)

    # Create aviator_rounds table if not exists
    db.session.execute(text("""
    CREATE TABLE IF NOT EXISTS aviator_rounds (
        id SERIAL PRIMARY KEY,
        chat_id BIGINT REFERENCES users(chat_id),
        start_time TIMESTAMPTZ DEFAULT NOW()
    )
    """))
    db.session.commit()

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
        try:
            db.session.execute(text(stmt))
            db.session.commit()
        except Exception as e:
            print("Migration warning (aviator_rounds):", e)

# ====================================================
# Helper: Get or Create User
# ====================================================
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

    if not username:
        username = f"user_{chat_id}"

    user = User.query.filter_by(chat_id=chat_id).first()
    if not user:
        user = User(chat_id=chat_id, username=username)
        db.session.add(user)
        db.session.commit()
    return user

# ====================================================
# Routes
# ====================================================

# Health check + WebApp UI
@app.get("/")
def index():
    if not request.args and not request.is_json:
        return "OK", 200  # Render health check

    user = get_or_create_user_from_query()
    BASE_HTML = """
    <html>
      <head><title>Game WebApp</title></head>
      <body>
        <h1>Welcome, {{username}}</h1>
        <p>Tap, Walk, and Fly Aviator!</p>
      </body>
    </html>
    """
    return render_template_string(
        BASE_HTML,
        username=user.username or user.chat_id
    )

# --- Tap Game ---
TAP_REWARD = Decimal("0.05")

@app.post("/api/tap")
def api_tap():
    user = get_or_create_user_from_query()
    user.balance_usd += TAP_REWARD
    db.session.commit()
    return jsonify({
        "message": "Tapped!",
        "reward_usd": str(TAP_REWARD),
        "balance_usd": str(user.balance_usd)
    })

# --- Steps / Walk Game ---
@app.post("/api/steps")
def api_steps():
    user = get_or_create_user_from_query()
    data = request.get_json(silent=True) or {}
    steps = int(data.get("steps", 0))

    if steps <= 0:
        abort(400, "Steps must be > 0")

    user.total_steps += steps
    reward = Decimal(str(steps)) * user.walk_rate
    user.balance_usd += reward
    db.session.commit()

    return jsonify({
        "message": "Steps logged",
        "steps": steps,
        "reward_usd": str(reward.quantize(Decimal("0.01"))),
        "balance_usd": str(user.balance_usd)
    })

# --- Aviator Game ---
@app.post("/api/aviator/start")
def api_aviator_start():
    user = get_or_create_user_from_query()
    data = request.get_json(silent=True) or {}
    bet_usd = Decimal(str(data.get("bet_usd", "0")))

    if bet_usd <= 0:
        abort(400, "Bet must be > 0")
    if user.balance_usd < bet_usd:
        abort(400, "Insufficient balance")

    round = AviatorRound(
        chat_id=user.chat_id,
        bet_usd=bet_usd,
        start_time=datetime.now(timezone.utc),
        crash_multiplier=Decimal(str(random.uniform(1.5, 5.0))).quantize(Decimal("0.01")),
        growth_per_sec=Decimal("0.25"),
        status="active"
    )

    db.session.add(round)
    user.balance_usd -= bet_usd  # Deduct bet
    db.session.commit()

    return jsonify({
        "message": "Round started",
        "round_id": round.id,
        "balance_usd": str(user.balance_usd)
    })

@app.post("/api/aviator/cashout")
def api_aviator_cashout():
    user = get_or_create_user_from_query()
    data = request.get_json(silent=True) or {}
    round_id = data.get("round_id")

    round = AviatorRound.query.filter_by(id=round_id, chat_id=user.chat_id).first()
    if not round or round.status != "active":
        abort(400, "Round not active or invalid")

    round.cashout_multiplier = Decimal(str(data.get("multiplier", "1.0")))
    round.cashout_time = datetime.now(timezone.utc)
    round.status = "cashed_out"
    round.profit_usd = (round.bet_usd * round.cashout_multiplier).quantize(Decimal("0.01"))

    user.balance_usd += round.profit_usd  # Add winnings
    db.session.commit()

    return jsonify({
        "message": "Cashed out",
        "profit_usd": str(round.profit_usd),
        "balance_usd": str(user.balance_usd)
    })

# --- Stats ---
@app.get("/stats")
def stats():
    user = get_or_create_user_from_query()
    return jsonify({
        "balance_usd": str(user.balance_usd),
        "balance_ngn": str(user.balance_ngn),
        "walk_level": user.walk_level,
        "walk_rate": str(user.walk_rate),
        "total_steps": user.total_steps
    })

# ====================================================
# Run
# ====================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
