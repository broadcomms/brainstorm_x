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
    WATSONX_API_KEY = os.environ.get("WATSONX_API_KEY", "")
    WATSONX_URL = os.environ.get("WATSONX_URL", "")
    WATSONX_PROJECT_ID = os.environ.get("WATSONX_PROJECT_ID", "")
    WATSONX_TTS_URL = os.environ.get("WATSONX_TTS_URL", "")

    # IBM watsonx.ai Foundation Models
    GRANITE_8B_INSTRUCT = os.environ.get("GRANITE_8B_INSTRUCT", "ibm/granite-3-3-8b-instruct")
