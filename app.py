#!/usr/bin/env python3
# app.py — Tapify Game App for Telegram
# Requirements:
#   pip install flask psycopg[binary] python-dotenv uvicorn
#
# Environment (.env):
#   DATABASE_URL=postgres://user:pass@host:port/dbname
#
# Start:
#   python app.py

import logging
import psycopg
import os
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv
import threading
import time

# Flask setup
app = Flask(__name__, template_folder='templates')

# Load environment variables
load_dotenv()

# Database setup
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    logging.error("DATABASE_URL is required in environment (.env)")
    raise ValueError("DATABASE_URL is required")
if "sslmode=" not in DATABASE_URL:
    DATABASE_URL += "?sslmode=require" if "?" not in DATABASE_URL else "&sslmode=require"

try:
    conn = psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row)
    conn.autocommit = True
    cursor = conn.cursor()
except psycopg.Error as e:
    logging.error(f"Database connection error: {e}")
    raise

# Logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Check if user is registered
def is_registered(chat_id):
    try:
        cursor.execute("SELECT payment_status FROM users WHERE chat_id=%s", (chat_id,))
        row = cursor.fetchone()
        logger.info(f"is_registered check for chat_id {chat_id}: {row}")
        return row and row["payment_status"] == 'registered'
    except psycopg.Error as e:
        logger.error(f"Database error in is_registered {chat_id}: {e}")
        return False

# Get user balance
def get_balance(chat_id):
    try:
        cursor.execute("SELECT balance FROM users WHERE chat_id=%s", (chat_id,))
        row = cursor.fetchone()
        return row["balance"] if row else 0
    except psycopg.Error as e:
        logger.error(f"Database error in get_balance {chat_id}: {e}")
        return 0

# Update user balance
def update_balance(chat_id, amount):
    try:
        cursor.execute("UPDATE users SET balance = balance + %s WHERE chat_id=%s", (amount, chat_id))
        conn.commit()
    except psycopg.Error as e:
        logger.error(f"Database error in update_balance {chat_id}: {e}")

# Error page for missing chat_id
@app.route('/error')
def error_page():
    return render_template('error.html', message="Unable to identify user. Please access the game through the Telegram bot.")

# Main game route
@app.route('/app')
def game():
    chat_id = request.args.get('chat_id', type=int)
    if not chat_id:
        logger.warning("Missing chat_id in /app request")
        return redirect('/error')
    logger.info(f"Game accessed by chat_id: {chat_id}")
    if not is_registered(chat_id):
        logger.warning(f"User {chat_id} not registered")
        return render_template('register.html')
    balance = get_balance(chat_id)
    return render_template('game.html', chat_id=chat_id, balance=balance)

# API to update score
@app.route('/api/update_score', methods=['POST'])
def update_score():
    data = request.get_json()
    chat_id = data.get('chat_id')
    score = data.get('score', 0)
    if not chat_id or not is_registered(chat_id):
        return jsonify({"error": "User not registered"}), 403
    try:
        # Convert score to balance (e.g., 1000 score = $1)
        balance_increase = score / 1000
        update_balance(chat_id, balance_increase)
        logger.info(f"Updated balance for {chat_id} by ${balance_increase}")
        return jsonify({"status": "success", "new_balance": get_balance(chat_id)})
    except Exception as e:
        logger.error(f"Error updating score for {chat_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500

# Daily reward API
@app.route('/api/claim_daily_reward', methods=['POST'])
def claim_daily_reward():
    data = request.get_json()
    chat_id = data.get('chat_id')
    if not chat_id or not is_registered(chat_id):
        return jsonify({"error": "User not registered"}), 403
    try:
        cursor.execute("SELECT last_daily_reward FROM users WHERE chat_id=%s", (chat_id,))
        row = cursor.fetchone()
        last_reward = row.get("last_daily_reward")
        now = psycopg.TimestampFromTicks(time.time())
        if last_reward and (now - last_reward).days < 1:
            return jsonify({"error": "Daily reward already claimed today"}), 400
        reward = 5.0  # $5 daily reward
        update_balance(chat_id, reward)
        cursor.execute("UPDATE users SET last_daily_reward=%s WHERE chat_id=%s", (now, chat_id))
        conn.commit()
        logger.info(f"Daily reward of ${reward} claimed by {chat_id}")
        return jsonify({"status": "success", "reward": reward, "new_balance": get_balance(chat_id)})
    except psycopg.Error as e:
        logger.error(f"Database error in claim_daily_reward {chat_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500

# Aviator game routes
@app.route('/api/aviator/fund', methods=['POST'])
def aviator_fund():
    data = request.get_json()
    chat_id = data.get('chat_id')
    amount = data.get('amount', 0)
    if not chat_id or not is_registered(chat_id):
        return jsonify({"error": "User not registered"}), 403
    if amount <= 0:
        return jsonify({"error": "Invalid amount"}), 400
    balance = get_balance(chat_id)
    if balance < amount:
        return jsonify({"error": "Insufficient balance"}), 400
    try:
        cursor.execute("UPDATE users SET balance = balance - %s WHERE chat_id=%s", (amount, chat_id))
        cursor.execute("INSERT INTO aviator_funds (chat_id, amount, timestamp) VALUES (%s, %s, %s)", 
                      (chat_id, amount, psycopg.TimestampFromTicks(time.time())))
        conn.commit()
        logger.info(f"Aviator funded with ${amount} for {chat_id}")
        return jsonify({"status": "success", "new_balance": get_balance(chat_id)})
    except psycopg.Error as e:
        logger.error(f"Database error in aviator_fund {chat_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/api/aviator/bet', methods=['POST'])
def aviator_bet():
    data = request.get_json()
    chat_id = data.get('chat_id')
    bet_amount = data.get('bet_amount', 0)
    if not chat_id or not is_registered(chat_id):
        return jsonify({"error": "User not registered"}), 403
    if bet_amount <= 0:
        return jsonify({"error": "Invalid bet amount"}), 400
    try:
        cursor.execute("SELECT SUM(amount) as total FROM aviator_funds WHERE chat_id=%s", (chat_id,))
        aviator_balance = cursor.fetchone()["total"] or 0
        if aviator_balance < bet_amount:
            return jsonify({"error": "Insufficient Aviator balance"}), 400
        cursor.execute("INSERT INTO aviator_bets (chat_id, bet_amount, timestamp) VALUES (%s, %s, %s)", 
                      (chat_id, bet_amount, psycopg.TimestampFromTicks(time.time())))
        conn.commit()
        logger.info(f"Aviator bet of ${bet_amount} placed by {chat_id}")
        return jsonify({"status": "success", "aviator_balance": aviator_balance - bet_amount})
    except psycopg.Error as e:
        logger.error(f"Database error in aviator_bet {chat_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/api/aviator/cashout', methods=['POST'])
def aviator_cashout():
    data = request.get_json()
    chat_id = data.get('chat_id')
    multiplier = data.get('multiplier', 1.0)
    if not chat_id or not is_registered(chat_id):
        return jsonify({"error": "User not registered"}), 403
    try:
        cursor.execute("SELECT bet_amount FROM aviator_bets WHERE chat_id=%s ORDER BY timestamp DESC LIMIT 1", (chat_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "No active bet"}), 400
        bet_amount = row["bet_amount"]
        winnings = bet_amount * multiplier
        update_balance(chat_id, winnings)
        cursor.execute("DELETE FROM aviator_bets WHERE chat_id=%s", (chat_id,))
        conn.commit()
        logger.info(f"Aviator cashout of ${winnings} for {chat_id} at {multiplier}x")
        return jsonify({"status": "success", "winnings": winnings, "new_balance": get_balance(chat_id)})
    except psycopg.Error as e:
        logger.error(f"Database error in aviator_cashout {chat_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/api/aviator/withdraw', methods=['POST'])
def aviator_withdraw():
    data = request.get_json()
    chat_id = data.get('chat_id')
    amount = data.get('amount', 0)
    if not chat_id or not is_registered(chat_id):
        return jsonify({"error": "User not registered"}), 403
    if amount < 50000:  # Minimum withdrawal in Naira
        return jsonify({"error": "Minimum withdrawal is ₦50,000"}), 400
    balance = get_balance(chat_id)
    if balance < (amount / 1000):  # Convert Naira to USD for balance check
        return jsonify({"error": "Insufficient balance"}), 400
    try:
        cursor.execute("UPDATE users SET balance = balance - %s WHERE chat_id=%s", (amount / 1000, chat_id))
        cursor.execute("INSERT INTO withdrawals (chat_id, amount, timestamp) VALUES (%s, %s, %s)", 
                      (chat_id, amount, psycopg.TimestampFromTicks(time.time())))
        conn.commit()
        logger.info(f"Withdrawal of ₦{amount} requested by {chat_id}")
        return jsonify({"status": "success", "new_balance": get_balance(chat_id)})
    except psycopg.Error as e:
        logger.error(f"Database error in aviator_withdraw {chat_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500

# Prediction space routes
@app.route('/api/predictions/available_matches', methods=['GET'])
def available_matches():
    chat_id = request.args.get('chat_id', type=int)
    if not chat_id or not is_registered(chat_id):
        return jsonify({"error": "User not registered"}), 403
    # Mock data (replace with football-data.org API call)
    matches = [
        {"id": 1, "home_team": "Team A", "away_team": "Team B", "date": "2025-08-22"},
        {"id": 2, "home_team": "Team C", "away_team": "Team D", "date": "2025-08-23"}
    ]
    return jsonify({"matches": matches})

@app.route('/api/predictions/get_prediction', methods=['POST'])
def get_prediction():
    data = request.get_json()
    chat_id = data.get('chat_id')
    match_id = data.get('match_id')
    if not chat_id or not is_registered(chat_id):
        return jsonify({"error": "User not registered"}), 403
    balance = get_balance(chat_id)
    prediction_cost = 0.5  # $0.5 for prediction
    if balance < prediction_cost:
        return jsonify({"error": "Insufficient balance for prediction"}), 400
    try:
        update_balance(chat_id, -prediction_cost)
        # Mock prediction (replace with football-data.org API call)
        prediction = {"match_id": match_id, "prediction": "Home team wins"}
        cursor.execute("INSERT INTO predictions (chat_id, match_id, prediction, timestamp) VALUES (%s, %s, %s, %s)", 
                      (chat_id, match_id, prediction["prediction"], psycopg.TimestampFromTicks(time.time())))
        conn.commit()
        logger.info(f"Prediction purchased by {chat_id} for match {match_id}")
        return jsonify({"status": "success", "prediction": prediction, "new_balance": get_balance(chat_id)})
    except psycopg.Error as e:
        logger.error(f"Database error in get_prediction {chat_id}: {e}")
        return jsonify({"error": "Internal server error"}), 500

# Run Flask app
def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

if __name__ == '__main__':
    # Create necessary tables
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS aviator_funds (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                amount REAL,
                timestamp TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS aviator_bets (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                bet_amount REAL,
                timestamp TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                amount REAL,
                timestamp TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                match_id INTEGER,
                prediction TEXT,
                timestamp TIMESTAMP
            )
        """)
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_daily_reward TIMESTAMP")
        conn.commit()
    except psycopg.Error as e:
        logger.error(f"Database error in table creation: {e}")
        raise

    # Start Flask in a separate thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
