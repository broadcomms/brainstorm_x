# app/__init__.py
import os
from flask import Flask
from flask_cors import CORS
from .config import Config
from .extensions import db, socketio
from .main.routes import main_bp

def create_app(config_filename=None):
    app = Flask(__name__, instance_relative_config=True) # Consider instance_relative_config=True
    app.config.from_object(Config)

    # Ensure instance folder exists (useful for uploads, sqlite db, etc.)
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass # Already exists

    # Enable CORS for all API routes (TODO: adjust origin for production)
    CORS(app, resources={r"/api/*": {"origins": "*"}}) #Allow all for dev

    # Initialize Application Extensions
    db.init_app(app)
    # SocketIO: Use message_queue= for production robustness
    socketio.init_app(app, cors_allowed_origins="*", async_mode="eventlet")

    # Register App Blueprints
    app.register_blueprint(main_bp)

    # PATCH: Add db.create_all() to create db instance on initialization
    with app.app_context():
         db.create_all() # Migrate database using sqlite3 command line SQL

    return app



