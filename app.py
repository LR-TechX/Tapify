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
import asyncio
import threading

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

# Main game route
@app.route('/app')
def game():
    chat_id = request.args.get('chat_id', type=int)
    if not chat_id:
        return jsonify({"error": "Chat ID required"}), 400
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

# Templates
@app.route('/templates/register.html')
def register_template():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Tapify - Register</title>
        <style>
            body { font-family: Arial, sans-serif; text-align: center; background-color: #f0f0f0; }
            .container { margin-top: 50px; }
            h1 { color: #333; }
            p { font-size: 18px; }
            a { color: #007bff; text-decoration: none; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Please Register</h1>
            <p>You need to register to play the game. Go back to the Telegram bot and complete the registration process.</p>
            <p><a href="https://t.me/your_bot_username">Return to Bot</a></p>
        </div>
    </body>
    </html>
    '''

@app.route('/templates/game.html')
def game_template():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Tapify Game</title>
        <style>
            body { font-family: Arial, sans-serif; text-align: center; background-color: #f0f0f0; }
            .container { margin-top: 20px; }
            .game-area { margin: 20px auto; padding: 20px; background: white; border-radius: 10px; width: 80%; max-width: 600px; }
            button { padding: 10px 20px; font-size: 16px; margin: 10px; cursor: pointer; }
            #score, #balance { font-size: 18px; margin: 10px 0; }
            .tab { display: inline-block; margin: 10px; padding: 10px; cursor: pointer; background: #ddd; border-radius: 5px; }
            .tab.active { background: #007bff; color: white; }
            .tab-content { display: none; }
            .tab-content.active { display: block; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Tapify Game</h1>
            <div id="balance">Balance: ${{ balance }}</div>
            <div class="tabs">
                <div class="tab" onclick="openTab('tapify')">Tapify</div>
                <div class="tab" onclick="openTab('aviator')">Aviator</div>
                <div class="tab" onclick="openTab('predictions')">Predictions</div>
            </div>
            <div id="tapify" class="tab-content game-area">
                <p id="score">Score: 0</p>
                <button onclick="tap()">Tap Me!</button>
                <button onclick="claimDailyReward()">Claim Daily Reward</button>
            </div>
            <div id="aviator" class="tab-content game-area">
                <p>Aviator Game</p>
                <input type="number" id="fundAmount" placeholder="Amount to fund (USD)">
                <button onclick="fundAviator()">Fund Aviator</button>
                <input type="number" id="betAmount" placeholder="Bet amount (USD)">
                <button onclick="placeBet()">Place Bet</button>
                <button onclick="cashout()">Cash Out</button>
                <input type="number" id="withdrawAmount" placeholder="Withdraw amount (NGN)">
                <button onclick="withdraw()">Withdraw</button>
            </div>
            <div id="predictions" class="tab-content game-area">
                <p>Football Predictions</p>
                <select id="matchSelect"></select>
                <button onclick="getPrediction()">Get Prediction ($0.5)</button>
                <p id="predictionResult"></p>
            </div>
        </div>
        <script>
            const chatId = {{ chat_id }};
            let score = 0;

            function openTab(tabName) {
                document.querySelectorAll('.tab-content').forEach(tab => tab.classList.remove('active'));
                document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
                document.getElementById(tabName).classList.add('active');
                document.querySelector(`.tab[onclick="openTab('${tabName}')"]`).classList.add('active');
                if (tabName === 'predictions') loadMatches();
            }

            function tap() {
                score += 100;
                document.getElementById('score').innerText = `Score: ${score}`;
                fetch('/api/update_score', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({chat_id: chatId, score: 100})
                }).then(response => response.json()).then(data => {
                    if (data.status === 'success') {
                        document.getElementById('balance').innerText = `Balance: $${data.new_balance}`;
                    }
                });
            }

            function claimDailyReward() {
                fetch('/api/claim_daily_reward', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({chat_id: chatId})
                }).then(response => response.json()).then(data => {
                    if (data.status === 'success') {
                        alert(`Daily reward of $${data.reward} claimed!`);
                        document.getElementById('balance').innerText = `Balance: $${data.new_balance}`;
                    } else {
                        alert(data.error);
                    }
                });
            }

            function fundAviator() {
                const amount = parseFloat(document.getElementById('fundAmount').value);
                fetch('/api/aviator/fund', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({chat_id: chatId, amount: amount})
                }).then(response => response.json()).then(data => {
                    if (data.status === 'success') {
                        alert(`Funded Aviator with $${amount}`);
                        document.getElementById('balance').innerText = `Balance: $${data.new_balance}`;
                    } else {
                        alert(data.error);
                    }
                });
            }

            function placeBet() {
                const betAmount = parseFloat(document.getElementById('betAmount').value);
                fetch('/api/aviator/bet', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({chat_id: chatId, bet_amount: betAmount})
                }).then(response => response.json()).then(data => {
                    if (data.status === 'success') {
                        alert(`Bet of $${betAmount} placed`);
                    } else {
                        alert(data.error);
                    }
                });
            }

            function cashout() {
                const multiplier = Math.random() * 5 + 1; // Mock multiplier
                fetch('/api/aviator/cashout', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({chat_id: chatId, multiplier: multiplier})
                }).then(response => response.json()).then(data => {
                    if (data.status === 'success') {
                        alert(`Cashed out with $${data.winnings} at ${multiplier.toFixed(2)}x`);
                        document.getElementById('balance').innerText = `Balance: $${data.new_balance}`;
                    } else {
                        alert(data.error);
                    }
                });
            }

            function withdraw() {
                const amount = parseFloat(document.getElementById('withdrawAmount').value);
                fetch('/api/aviator/withdraw', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({chat_id: chatId, amount: amount})
                }).then(response => response.json()).then(data => {
                    if (data.status === 'success') {
                        alert(`Withdrawal of ₦${amount} requested`);
                        document.getElementById('balance').innerText = `Balance: $${data.new_balance}`;
                    } else {
                        alert(data.error);
                    }
                });
            }

            function loadMatches() {
                fetch(`/api/predictions/available_matches?chat_id=${chatId}`)
                    .then(response => response.json())
                    .then(data => {
                        const select = document.getElementById('matchSelect');
                        select.innerHTML = '';
                        data.matches.forEach(match => {
                            const option = document.createElement('option');
                            option.value = match.id;
                            option.text = `${match.home_team} vs ${match.away_team} (${match.date})`;
                            select.appendChild(option);
                        });
                    });
            }

            function getPrediction() {
                const matchId = document.getElementById('matchSelect').value;
                fetch('/api/predictions/get_prediction', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({chat_id: chatId, match_id: matchId})
                }).then(response => response.json()).then(data => {
                    if (data.status === 'success') {
                        document.getElementById('predictionResult').innerText = `Prediction: ${data.prediction.prediction}`;
                        document.getElementById('balance').innerText = `Balance: $${data.new_balance}`;
                    } else {
                        alert(data.error);
                    }
                });
            }

            // Initialize
            openTab('tapify');
        </script>
    </body>
    </html>
    '''

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
