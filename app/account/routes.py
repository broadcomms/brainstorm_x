# app/account/routes.py

import os
from flask import Blueprint, render_template, flash, redirect, url_for, current_app, request
from flask_login import login_required, current_user
# Import necessary models
from app.models import User, Workspace, WorkspaceMember, Invitation, Workshop
# Import database instance
from app import db 
from sqlalchemy import or_, desc
from app.config import Config

account_bp = Blueprint("account_bp", __name__, template_folder="templates")

# --- Define the default path as a constant ---
DEFAULT_PROFILE_PIC = "images/default-profile.png"

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

    # --- Validate profile pic using the constant ---
    # Also handles cases where the DB might still have the old 'instance/' path
    if not current_user.profile_pic_url or \
       current_user.profile_pic_url.startswith('instance/') or \
       not path_exists_in_static(current_user.profile_pic_url):
        current_user.profile_pic_url = DEFAULT_PROFILE_PIC

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
        "account_details.html",
        user=current_user,
        app_name=APP_NAME,
        my_workspaces=my_workspaces,
        pending_invitations=pending_invitations,
        workshops=workshops,
        tasks=tasks,
        members=members,
        default_profile_pic=DEFAULT_PROFILE_PIC # Pass default path for onerror
    )


##############################################################################
# Edit Account (Email, Username, and Profile Data)
##############################################################################
@account_bp.route("/edit_account", methods=["GET", "POST"])
@login_required
def edit_account():
    """
    Allows the user to edit their personal information,
    including first/last name, job title, phone, etc.
    Only admin/manager can edit other users if needed (by passing user_id?).
    """
    user_id = request.args.get("user_id", type=int, default=current_user.user_id)

    # Only admin/manager can edit someone else's data
    if user_id != current_user.user_id:
        if current_user.role not in ["admin", "manager"]:
            flash("You do not have permission to edit another user's account.", "danger")
            return redirect(url_for("account_bp.account"))

    user_to_edit = User.query.get_or_404(user_id)

    if request.method == "POST":
        # If you're an admin or manager, or editing self
        new_username = request.form.get("username", "").strip()
        new_email = request.form.get("email", "").strip().lower()
        new_first_name = request.form.get("first_name", "").strip()
        new_last_name = request.form.get("last_name", "").strip()
        new_job_title = request.form.get("job_title", "").strip()
        new_organization = request.form.get("organization", "").strip()
        new_phone_number = request.form.get("phone_number", "").strip()

        # Basic validation
        if not new_email:
            flash("Email is required.", "danger")
            return redirect(url_for("profile_bp.edit_account"))

        # Check if new username or email is taken by someone else
        existing_user = User.query.filter(
            (User.user_id != user_to_edit.user_id) &
            ((User.username == new_username) | (User.email == new_email))
        ).first()
        if existing_user:
            flash("That email is already in use.", "danger")
            return redirect(url_for("profile_bp.edit_account"))

        # Update fields
        user_to_edit.username = new_username
        user_to_edit.email = new_email
        user_to_edit.first_name = new_first_name
        user_to_edit.last_name = new_last_name
        user_to_edit.job_title = new_job_title
        user_to_edit.organization = new_organization
        user_to_edit.phone_number = new_phone_number

        db.session.commit()

        flash("Account information updated successfully!", "success")
        return redirect(url_for("account_bp.account"))
    
    return render_template("account_edit.html", user=user_to_edit)