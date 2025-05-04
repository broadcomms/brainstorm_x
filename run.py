# run.py
import eventlet
eventlet.monkey_patch()
import logging
from app import create_app, socketio
from app.extensions import db

app = create_app()

# Configure logging to include line number
logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d - %(message)s',
    level=logging.INFO
)


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5001, debug=True)