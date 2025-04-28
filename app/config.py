# app/config.py
import os
from dotenv import load_dotenv

class Config:
    # General environmental details
    APP_NAME = os.environ.get("APP_NAME", "BrainStormX")
    SECRET_KEY = os.environ.get("SECRET_KEY", "change_me_in_env")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URI", "sqlite:///app_database.sqlite")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # IBM watsonx.ai Credentials
    WATSONX_API_KEY = os.environ.get("WATSONX_API_KEY", "EJo-VJ76CL6r9DDH9Mp78b3J9Ne-xnpGJbhifkFm9Nua")
    WATSONX_URL = os.environ.get("WATSONX_URL", "https://us-south.ml.cloud.ibm.com")
    WATSONX_PROJECT_ID = os.environ.get("WATSONX_PROJECT_ID", "c2e6fb46-5f89-4a58-83fb-8c0e5ae9d365")
    WATSONX_TTS_URL = os.environ.get("WATSONX_TTS_URL", "")

    # IBM watsonx.ai Foundation Models
    GRANITE_8B_INSTRUCT = os.environ.get("GRANITE_8B_INSTRUCT", "ibm/granite-3-3-8b-instruct")
    WATSONX_MODEL_ID_1 = os.environ.get("WATSONX_MODEL_ID_1", "ibm/granite-3-3-8b-instruct")
    WATSONX_MODEL_ID_2 = os.environ.get("WATSONX_MODEL_ID_2", "ibm/granite-3-3-8b-instruct")
    WATSONX_MODEL_ID_3 = os.environ.get("WATSONX_MODEL_ID_3", "ibm/granite-3-3-8b-instruct")
    
        # Flask-Mail config
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "broadcomms.net")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "465"))
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'False').lower() == 'true'
    MAIL_USE_SSL = os.environ.get('MAIL_USE_SSL', 'True').lower() == 'true'
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "no-reply@broadcomms.net")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "5i-8v@S4y$Y?")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "no-reply@broadcomms.net")