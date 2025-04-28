# app/auth/routes.py

import os
import jwt
import datetime
import secrets
from smtplib import SMTPException
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import login_user, logout_user, login_required, current_user
from passlib.hash import bcrypt
from sqlalchemy import or_
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
# Flask-Mail
from flask_mail import Message

# Import database & models
from app.extensions import db, login_manager, mail
from app.models import User

auth_bp = Blueprint("auth_bp", __name__, template_folder="templates")

# SECRET_KEY for JWT or token generation (in production, load from config)
SECRET_KEY = os.environ.get("SECRET_KEY", "change_me_in_env")
APP_NAME = os.getenv("APP_NAME", "BrainStormX") 
MAIL_DEFAULT_SENDER = os.getenv("MAIL_SENDER_EMAIL", "no-reply@broadcomms.net") 


########################################################
# Helper function to send verification/reset emails
########################################################
# TODO: Move send_email from auth to extension module
def send_email(to_address, subject, body_html):
    """
    Uses Flask-Mail to send an HTML email.
    Make sure your app.config[MAIL_*] settings are correct.
    """
    msg = Message(
        subject, 
        sender=(APP_NAME, MAIL_DEFAULT_SENDER),
        recipients=[to_address]
        )
    msg.html = body_html
    try:
        mail.send(msg)
    except SMTPException as e:
        print(f"SMTP error occurred: {e}")
    print("Email Verification message sent") # DEBUG

########################################################
# Registration
########################################################
@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    
    """
    Handle user registration.
    If an invitation token and workspace_id are provided, automatically add the new user
    to that workspace.
    """
    
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        username = request.form.get("username", "").strip()  # Optional: for display
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        job_title = request.form.get("job_title", "").strip()
        workspace = request.form.get("workspace", "").strip()
        phone_number = request.form.get("phone_number", "").strip()

        # Basic validation
        if not email or not password:
            flash("Email and password are required.", "danger")
            return redirect(url_for("auth_bp.register"))

        # Check for existing user
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash("That email is already taken.", "danger")
            return redirect(url_for("auth_bp.register"))

        # Hash password with passlib
        hashed_pw = bcrypt.hash(password)

        # Generate verification token
        verification_token = secrets.token_urlsafe(32)

        new_user = User(
            email=email,
            password=hashed_pw,
            username=username if username else email.split("@")[0],
            first_name=first_name,
            last_name=last_name,
            job_title=job_title,
            phone_number=phone_number,
            email_verified=False,
            verification_token=verification_token,
            role="user",
        )
        db.session.add(new_user)
        db.session.commit()
        
        # Check if invitation token was provided
        invitation_token = request.args.get("invitation_token")
        workspace_id = request.args.get("workspace_id", type=int)
        if invitation_token and workspace_id:
            from app.models import Invitation, WorkspaceMember
            invitation = Invitation.query.filter_by(token=invitation_token, workspace_id=workspace_id, email=email).first()
            if invitation:
                # Create membership for the new user
                new_membership = WorkspaceMember(
                    workspace_id=workspace_id,
                    user_id=new_user.user_id,
                    role="user",
                    status="active"
                )
                db.session.add(new_membership)
                # Optionally, mark the invitation as used (or delete it)
                db.session.delete(invitation)
                db.session.commit()

        # Send verification email
        verification_link = url_for("auth_bp.verify_email", token=verification_token, _external=True)
        email_body = f"""
        <p>Welcome to {APP_NAME}!</p>
        <p>Please verify your email by clicking this link:
        <a href="{verification_link}">Verify Email</a></p>
        <p>If you did not sign up for {APP_NAME}, please ignore this email.</p>
        """
        send_email(to_address=email, subject=f"Verify your {APP_NAME} account", body_html=email_body)

        flash("Registration successful! Please check your email to verify your account.", "success")
        return redirect(url_for("auth_bp.login"))

    # Render registration form (optionally, pass along invitation_token and workspace_id to the template)
    invitation_token = request.args.get("invitation_token")
    workspace_id = request.args.get("workspace_id")
    return render_template("account_create.html", invitation_token=invitation_token, workspace_id=workspace_id)

########################################################
# Email Verification
########################################################
@auth_bp.route("/verify_email/<token>")
def verify_email(token):
    """
    Handles email verification via token in the URL.
    If token matches, set email_verified=True.
    Then if any invitation exist for this email, automatically add
    user to those workspace.
    """
    user = User.query.filter_by(verification_token=token).first()
    if not user:
        flash("Invalid verification token.", "danger")
        return redirect(url_for("auth_bp.login"))

    # Mark user as verified
    user.email_verified = True
    user.verification_token = None  # Clear token so it can’t be reused
    db.session.commit()

    # Check for invitations that match this user's email
    from app.models import Invitation, WorkspaceMember
    pending_invitations = Invitation.query.filter_by(email=user.email).all()

    added_orgs = []
    for inv in pending_invitations:
        # Check if user is already in the org
        existing_member = WorkspaceMember.query.filter_by(
            workspace_id=inv.workspace_id,
            user_id=user.user_id
        ).first()
        if not existing_member:
            # Add them as active or invited
            new_member = WorkspaceMember(
                workspace_id=inv.workspace_id,
                user_id=user.user_id,
                role="user",
                status="active"
            )
            db.session.add(new_member)
            added_orgs.append(inv.workspace_id)
        # Remove or mark invitation as used
        db.session.delete(inv)
    db.session.commit()

    if added_orgs:
        flash("Your email has been verified! You have also been added to any pending workspace", "success")
    else:
        flash("Your email has been verified!", "success")

    return redirect(url_for("auth_bp.login"))


########################################################
# Login
########################################################
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    next_page = request.args.get('next') or url_for("account_bp.account")
    """
    Email-based login.
    - Check if email_verified first. If not verified, block or show message.
    """
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        user = User.query.filter_by(email=email).first()
        if not user:
            flash("Invalid email or password.", "danger")
            return redirect(url_for("auth_bp.login"))

        if not bcrypt.verify(password, user.password):
            flash("Invalid email or password.", "danger")
            return redirect(url_for("auth_bp.login"))

        if not user.email_verified:
            flash("Your email is not verified. Please check your inbox.", "danger")
            return redirect(url_for("auth_bp.login"))

        # Log user in
        login_user(user)
        flash("Logged in successfully.", "success")
        return redirect(next_page)

    return render_template("auth_login.html")

########################################################
# Logout
########################################################
@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("main_bp.index"))



########################################################
# Password Reset - Request (Forgot Password)
########################################################
@auth_bp.route("/forgot_password", methods=["GET", "POST"])
def forgot_password():
    """
    Request password reset. Generates a token and emails it to the user.
    """
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()
        if not user:
            flash("If that email exists, a reset link has been sent.", "info")
            return redirect(url_for("auth_bp.forgot_password"))

        # Generate reset token
        reset_token = secrets.token_urlsafe(32)
        user.reset_token = reset_token
        user.reset_token_expires = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
        db.session.commit()

        reset_link = url_for("auth_bp.reset_password", token=reset_token, _external=True)
        email_body = f"""
        <p>We received a request to reset your {APP_NAME} password.</p>
        <p>Click here to reset: <a href="{reset_link}">Reset Password</a></p>
        <p>If you did not request this, please ignore.</p>
        """
        send_email(to_address=email, subject=f"Password Reset - {APP_NAME}", body_html=email_body)

        flash("If that email exists, a reset link has been sent.", "info")
        return redirect(url_for("auth_bp.login"))

    return render_template("auth_password.html")

########################################################
# Password Reset - Process
########################################################
@auth_bp.route("/reset_password/<token>", methods=["GET", "POST"])
def reset_password(token):
    """
    Validate reset token, let user set a new password.
    """
    user = User.query.filter_by(reset_token=token).first()
    if not user:
        flash("Invalid or expired reset token.", "danger")
        return redirect(url_for("auth_bp.forgot_password"))

    # Check if token is expired
    if user.reset_token_expires < datetime.datetime.utcnow():
        flash("That reset link has expired. Please request a new one.", "danger")
        user.reset_token = None
        user.reset_token_expires = None
        db.session.commit()
        return redirect(url_for("auth_bp.forgot_password"))

    if request.method == "POST":
        new_password = request.form.get("new_password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()

        if not new_password or not confirm_password:
            flash("All fields are required.", "danger")
            return redirect(url_for("auth_bp.reset_password", token=token))

        if new_password != confirm_password:
            flash("Passwords do not match.", "danger")
            return redirect(url_for("auth_bp.reset_password", token=token))

        # Update password
        user.password = bcrypt.hash(new_password)
        # Clear reset token
        user.reset_token = None
        user.reset_token_expires = None
        db.session.commit()

        flash("Your password has been updated. Please log in.", "success")
        return redirect(url_for("auth_bp.login"))

    return render_template("reset_password.html")

########################################################
# Example Role-Restricted Endpoint
########################################################
@auth_bp.route("/admin_only")
@login_required
def admin_only():
    """
    Example protected route that only Admin can access.
    """
    if current_user.role != "admin":
        flash("You do not have permission to access this resource.", "danger")
        return redirect(url_for("dashboard_bp.dashboard"))
    return "Welcome, Admin! (This is a secure admin-only endpoint.)"
