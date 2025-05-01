# app/__init__.py
import os
import sqlite3

import markdown
from flask import Flask
from flask_cors import CORS
from .config import Config
from .extensions import db, socketio, login_manager, mail
from app.models import User 

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START, StateGraph, MessagesState 
# -------------------------

# --- ADDED IMPORTS (Needed for call_model) ---
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain_ibm import ChatWatsonx 
# -------------------------


# Import blueprints
from .main.routes import main_bp
from .auth.routes import auth_bp
from .account.routes import account_bp
from .workspace.routes import workspace_bp
from .document.routes import document_bp
from .workshop.routes import workshop_bp
from .service.routes.agent import agent_bp

# Example assuming static folder is inside 'app' directory
static_dir = os.path.join(os.path.dirname(__file__), 'static')

def create_app(config_filename=None):
    app = Flask(__name__, static_folder=static_dir, static_url_path='/static')
    app.config.from_object(Config)

    # Ensure instance folder exists
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    # Enable CORS
    CORS(app, resources={r"/*": {"origins": "*"}}) # Allow all for dev, adjust later

    # Initialize Application Extensions
    db.init_app(app)
    login_manager.init_app(app) # Initialize LoginManager
    mail.init_app(app) # Initialize Mail
    socketio.init_app(app, cors_allowed_origins="*", async_mode="eventlet")
    # Register Socket.IO event handlers
    from . import sockets  # noqa: F401

    # --- Flask-Login Configuration ---
    login_manager.login_view = 'auth_bp.login' # Route name for login page
    login_manager.login_message_category = 'info' # Flash message category

    @login_manager.user_loader
    def load_user(user_id):
        # Return the user object from the user ID stored in the session
        return User.query.get(int(user_id))
    # --------------------------------
    
    # --- Register Jinja Filter for Markdown ---
    @app.template_filter('markdown')
    def markdown_filter(text):
        """Converts Markdown text to HTML."""
        # You can add extensions here if needed, e.g., 'fenced_code', 'tables'
        return markdown.markdown(text, extensions=['fenced_code'])
    # -----------------------------------------

    # Register App Blueprints
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(account_bp, url_prefix="/account")
    app.register_blueprint(workspace_bp, url_prefix="/workspace")
    app.register_blueprint(document_bp, url_prefix="/document")
    app.register_blueprint(workshop_bp, url_prefix="/workshop")
    app.register_blueprint(agent_bp, url_prefix="/agent") # Register agent blueprint

    with app.app_context():
        # Create database tables if they don't exist
        db.create_all()

        # --- INITIALIZE AGENT MEMORY ---
        workflow = StateGraph(MessagesState)
        
        # --- ADD A MINIMAL NODE AND EDGE ---
        # Define a placeholder function or a simple model call if needed
        # This mirrors the structure in agent.py but might not need the full LLM call here
        # depending on how app.agent_workflow is used elsewhere.
        # For now, a simple placeholder function is safest if you only need the checkpointer.
        def placeholder_node(state: MessagesState):
            # This node doesn't necessarily need to do anything complex,
            # it just needs to exist for the graph structure.
            # It should return a dictionary matching the state schema.
            print("Placeholder node in __init__ workflow executed.") # For debugging
            # Return the state unchanged or add a dummy message
            # return {"messages": state.get("messages", []) + [AIMessage(content="Placeholder response")]}
            return {} # Returning empty dict might be sufficient if state updates aren't needed here

        # Add the node
        workflow.add_node("placeholder", placeholder_node)
        # Add the entry point edge from START to the node
        workflow.add_edge(START, "placeholder")
        # Add an edge back to itself or to END if needed, otherwise it stops.
        # workflow.add_edge("placeholder", END) # Or loop: workflow.add_edge("placeholder", "placeholder")
        # For just setting up the checkpointer, START -> node might be enough.
        # -------------------------------------

        db_file = os.path.join(app.instance_path, "agent_memory.sqlite")
        conn = sqlite3.connect(db_file, check_same_thread=False)
        saver = SqliteSaver(conn)

        # Compile and store the agent workflow on the app object
        # This compiled workflow might differ from the one used in agent.py's create_react_agent
        app.agent_workflow = workflow.compile(checkpointer=saver)
        print("Compiled workflow in __init__ successfully.") # Debug message
        # ---------------------------------

    return app
