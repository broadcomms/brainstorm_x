# app/account/routes.py

import os
from flask import Blueprint, render_template, flash, redirect, url_for, current_app
from flask_login import login_required, current_user
# Import necessary models
from app.models import User, Workspace, WorkspaceMember, Invitation, Workshop
# Import database instance
from app import db 
from sqlalchemy import or_, desc
from app.config import Config

account_bp = Blueprint("account_bp", __name__, template_folder="templates")

def path_exists_in_static(relative_path: str) -> bool:
    if not relative_path:
        return False
    full_path = os.path.join(current_app.static_folder, relative_path)
    return os.path.isfile(full_path)

@account_bp.route("/")
@login_required
def account():
    """Account page"""
    # If user isn't 'user', block them
    if current_user.role not in ["user", "manager", "admin"]:
        flash("Access denied.", "danger")
        return redirect(url_for("main_bp.index"))

    # Validate profile pic
    if not current_user.profile_pic_url or not path_exists_in_static(current_user.profile_pic_url):
        current_user.profile_pic_url = "default-profile.png"
       


    # --- Fetch User's Workspaces ---
    user_memberships = WorkspaceMember.query.filter_by(user_id=current_user.user_id, status='active').all() # Ensure only active memberships
    my_workspace_ids = [membership.workspace_id for membership in user_memberships]
    my_workspaces = Workspace.query.filter(Workspace.workspace_id.in_(my_workspace_ids)).order_by(Workspace.name).all()

    # --- Fetch Pending Invitations ---
    # The query itself was correct, the model was missing the 'status' field
    pending_invitations = Invitation.query.filter_by(
        email=current_user.email, status='pending'
    ).order_by(desc(Invitation.sent_timestamp)).all() # Use sent_timestamp if created_at doesn't exist

    # --- Fetch User's Workshops (Sessions) ---
    # Assuming 'Workshop' is your session model and it's linked via workspace_id
    # Ensure Workshop model is imported
    workshops = Workshop.query.filter(Workshop.workspace_id.in_(my_workspace_ids)).order_by(desc(Workshop.date_time)).limit(10).all() # Example limit

    # --- Fetch User's Tasks ---
    # Replace ActionItem with your actual task model if different
    # This example assumes tasks are assigned directly to a user via 'assigned_user_id'
    # If the task model is named differently or linked differently, adjust this query
    tasks = [] # Initialize as empty list
    # Example: If you have an ActionItem model linked to Workshop, and Workshop linked to Workspace
    # from app.models import ActionItem # Make sure it's imported
    # tasks = ActionItem.query.join(Workshop).filter(Workshop.workspace_id.in_(my_workspace_ids), ActionItem.assigned_user_id == current_user.user_id).order_by(desc(ActionItem.created_at)).limit(10).all()
    # If tasks are not implemented yet, keep tasks = []



    # --- Fetch Members ---
    all_member_ids_in_my_workspaces = db.session.query(WorkspaceMember.user_id)\
        .filter(WorkspaceMember.workspace_id.in_(my_workspace_ids))\
        .distinct()\
        .all()
    all_member_ids = [m[0] for m in all_member_ids_in_my_workspaces if m[0] != current_user.user_id]
    members = User.query.filter(User.user_id.in_(all_member_ids)).order_by(User.first_name, User.last_name).limit(20).all()

    APP_NAME = current_app.config.get("APP_NAME", "BrainStormX")

    return render_template(
        "account.html",
        user=current_user,
        app_name=APP_NAME,
        my_workspaces=my_workspaces,
        pending_invitations=pending_invitations,
        workshops=workshops, # Pass workshops as sessions
        tasks=tasks,
        members=members
    )
