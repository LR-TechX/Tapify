import asyncio
import threading
from app import start_bot  # Import the start_bot function from app.py

# Gunicorn configuration
bind = "0.0.0.0:$PORT"  # Bind to Render's dynamically assigned PORT
workers = 2  # Number of worker processes for Flask
worker_class = "gevent"  # Use gevent for async compatibility
timeout = 120  # Worker timeout in seconds

def when_ready(server):
    """
    Called when Gunicorn server is ready.
    Starts the Telegram bot polling in a separate thread with its own event loop.
    """
    def run_bot():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start_bot())

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
