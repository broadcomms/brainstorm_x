# app/main/routes.py
from flask import Blueprint, render_template, redirect, url_for
from flask_login import current_user

main_bp = Blueprint("main_bp", __name__, template_folder="templates")

@main_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("account_bp.account"))
    return render_template("auth_login.html")

