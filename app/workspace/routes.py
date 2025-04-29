# app/workspace/routes.py

import os
from flask import Blueprint, request, redirect, url_for, flash, render_template, jsonify, current_app
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Workspace, WorkspaceMember, User, Invitation, Document, Workshop
from datetime import datetime
from flask_mail import Message
from sqlalchemy import desc
from sqlalchemy.orm import joinedload, selectinload

APP_NAME = os.getenv("APP_NAME", "BrainStormX")
workspace_bp = Blueprint("workspace_bp", __name__, template_folder="templates")


# --- Helper Function for Permission Check ---
def check_admin_permission(workspace_id, user_id):
    """Checks if the user is an admin or manager of the workspace."""
    workspace = Workspace.query.get_or_404(workspace_id)
    # Owner always has permission
    if workspace.owner_id == user_id:
        return True
    # Check for admin/manager role in WorkspaceMember
    membership = WorkspaceMember.query.filter_by(
        workspace_id=workspace_id, user_id=user_id, status='active'
    ).first()
    return membership and membership.role in ['admin', 'manager']









###################################
# 1. List Workspaces
###################################
@workspace_bp.route("/list")
@login_required
def list_workspaces():
    """
    Shows workspaces the user belongs to, and optionally public workspaces they can join.
    """
    # Workspaces the current user is in:
    my_workspace_ids = [m.workspace_id for m in current_user.workspace_memberships]
    my_workspaces = Workspace.query.filter(
        Workspace.workspace_id.in_(my_workspace_ids)
    ).all()

    # Optionally list all public workspaces that the user is not in:
    public_workspaces = (
        Workspace.query.filter_by(is_private=False)
        .filter(~Workspace.workspace_id.in_(my_workspace_ids))
        .all()
    )

    return render_template(
        "workspace_list.html",
        my_workspaces=my_workspaces,
        public_workspaces=public_workspaces,
    )

###################################
# 2. Create Workspace
###################################
@workspace_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_workspace():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        # --- ADDED: Get description ---
        description = request.form.get("description", "").strip()
        is_private = request.form.get("is_private") == "on"

        if not name:
            flash("Workspace name is required.", "danger")
            # --- Return render_template instead of redirect to preserve form data (optional but good UX) ---
            return render_template("workspace_create.html", name=name, description=description, is_private=is_private)

        # Check if name already in use
        existing = Workspace.query.filter_by(name=name).first()
        if existing:
            flash(
                f"Workspace '{name}' already exists. Please choose a different name.",
                "danger",
            )
            # --- Return render_template instead of redirect ---
            return render_template("workspace_create.html", name=name, description=description, is_private=is_private)


        # Create the workspace object
        new_workspace = Workspace(
            name=name,
            owner_id=current_user.user_id,
            is_private=is_private,
            description=description,
        )
        db.session.add(new_workspace)

        # --- FIX: Flush the session to get the workspace_id ---
        try:
            db.session.flush() # This assigns the ID to new_workspace

            # Now new_workspace.workspace_id has a value
            owner_membership = WorkspaceMember(
                workspace_id=new_workspace.workspace_id, # Now this works!
                user_id=current_user.user_id,
                role="admin",
                status="active",
            )
            db.session.add(owner_membership)

            # Commit both the workspace and the membership together
            db.session.commit()
            flash("Workspace created successfully!", "success")
            return redirect(
                url_for(
                    "workspace_bp.view_workspace", workspace_id=new_workspace.workspace_id
                )
            )
        except Exception as e:
            db.session.rollback() # Rollback the transaction on error
            current_app.logger.error(f"Error creating workspace: {e}")
            flash("An error occurred while creating the workspace.", "danger")
            # It's good practice to pass the attempted values back to the form
            return render_template("workspace_create.html", name=name, description=description, is_private=is_private)

    # GET request
    return render_template("workspace_create.html")



###################################
# 3. Edit Workspace
###################################
@workspace_bp.route("/edit/<int:workspace_id>", methods=["GET", "POST"])
@login_required
def edit_workspace(workspace_id):
    """
    Allows the workspace owner or admin to edit workspace details.
    """
    workspace = Workspace.query.get_or_404(workspace_id)

    # --- Permission Check: Only owner or admin can edit ---
    is_owner = workspace.owner_id == current_user.user_id
    membership = WorkspaceMember.query.filter_by(
        workspace_id=workspace_id, user_id=current_user.user_id, status='active'
    ).first()
    is_admin = membership and membership.role == 'admin'

    if not (is_owner or is_admin):
        flash("You do not have permission to edit this workspace.", "danger")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        new_description = request.form.get("description", "").strip()
        new_is_private = request.form.get("is_private") == "on"

        if not new_name:
            flash("Workspace name cannot be empty.", "danger")
            # Re-render form with error and existing data
            return render_template("workspace_edit.html", workspace=workspace)

        # Check if new name conflicts with another workspace (excluding itself)
        existing = Workspace.query.filter(
            Workspace.name == new_name,
            Workspace.workspace_id != workspace_id
        ).first()
        if existing:
            flash(f"Workspace name '{new_name}' is already taken.", "danger")
            # Re-render form with error and existing data (pass back attempted values)
            workspace.name = new_name # Temporarily set for template rendering
            workspace.description = new_description
            workspace.is_private = new_is_private
            return render_template("workspace_edit.html", workspace=workspace)

        # Update workspace details
        workspace.name = new_name
        workspace.description = new_description
        workspace.is_private = new_is_private
        workspace.updated_timestamp = datetime.utcnow() # Explicitly update timestamp

        try:
            db.session.commit()
            flash("Workspace details updated successfully!", "success")
            return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error updating workspace {workspace_id}: {e}")
            flash("An error occurred while updating the workspace.", "danger")
            # Re-render form with error and existing data
            return render_template("workspace_edit.html", workspace=workspace)

    # --- GET Request: Render the edit form with current data ---
    return render_template("workspace_edit.html", workspace=workspace)



###################################
# 4. View Workspace
###################################
@workspace_bp.route("/<int:workspace_id>")
@login_required
def view_workspace(workspace_id):
    """
    Displays workspace details, including members, documents, and upcoming/past workshops
    for the workspace. If private, ensure user is a member.
    """
    workspace = (
        Workspace.query.options(
            # fetch members and each member’s user in one round‑trip
            selectinload(Workspace.members).selectinload(WorkspaceMember.user)
        )
        .get_or_404(workspace_id)
    )
    my_membership = None # Initialize


    # If private, must be a member or owner
    if workspace.is_private:
        my_membership = WorkspaceMember.query.filter_by(
            workspace_id=workspace_id, user_id=current_user.user_id, status='active' # Ensure member is active
        ).first()
        # Also allow owner even if they somehow aren't a member (shouldn't happen with current logic)
        is_owner = workspace.owner_id == current_user.user_id
        if not my_membership and not is_owner:
            flash("This workspace is private. Access denied.", "danger")
            return redirect(url_for("workspace_bp.list_workspaces"))
    else:
         # For public workspaces, still useful to know the user's membership status/role
         my_membership = WorkspaceMember.query.filter_by(
            workspace_id=workspace_id, user_id=current_user.user_id, status='active'
        ).first()

    # --- Prepare Member Data for Template ---
    all_members = workspace.members  # Get all members (already eager loaded)
    active_members = [m for m in all_members if m.status == 'active']
    pending_members = [m for m in all_members if m.status != 'active'] # Includes 'invited', 'requested', etc.
    active_member_count = len(active_members)

    # --- Fetch Workspace Documents ---
    workspace_documents = Document.query.options(
            joinedload(Document.uploader) # Eager load uploader details
        ).filter_by(
            workspace_id=workspace_id
        ).order_by(
            Document.uploaded_at.desc() # Show newest first
        ).all()

    # --- Fetch Workspace Workshops ---
    # Eager load creator to avoid N+1 in template
    workspace_workshops = Workshop.query.options(
            joinedload(Workshop.creator)
        ).filter_by(
            workspace_id=workspace_id
        ).order_by(
            Workshop.date_time.asc() # Show upcoming first
        ).all()
    
    # --- Determine Edit Permission ---
    can_edit = (workspace.owner_id == current_user.user_id) or \
               (my_membership and my_membership.role == 'admin')

    can_manage_members = (workspace.owner_id == current_user.user_id) or \
                         (my_membership and my_membership.role in ['admin', 'manager'])
                         
    # --- Determine Workshop Creation Permission ---
    # Any active member can create a workshop (adjust if needed)
    can_create_workshop = my_membership is not None and my_membership.status == 'active'

    # --- Render Template with Prepared Data ---
    return render_template( 
        "workspace_details.html",
        workspace=workspace,
        documents=workspace_documents,
        my_membership=my_membership,
        can_edit=can_edit,
        can_manage_members=can_manage_members, # Pass this flag
        # Pass the prepared member lists and count
        active_members=active_members,
        pending_members=pending_members,
        active_member_count=active_member_count,
        # --- ADDED: Pass workshops and permission ---
        workshops=workspace_workshops,
        can_create_workshop=can_create_workshop
    )

###################################
# 5. Invite User to Workspace
###################################
@workspace_bp.route("/invite_member", methods=["POST"])
@login_required
def invite_member():
    """
    Admin/manager can invite a user by email.
    If the user exists, create a WorkspaceMember with status 'invited'
    and record an Invitation so they can choose to accept or decline.
    If the user does not exist, create an Invitation record and send a registration link.
    """
    import secrets

    workspace_id = request.form.get("workspace_id", type=int)
    email = request.form.get("email", "").strip().lower()
    custom_message = request.form.get("custom_message", "").strip()
    workspace = Workspace.query.get_or_404(workspace_id)

    # Check permission
    my_membership = WorkspaceMember.query.filter_by(
        workspace_id=workspace_id, user_id=current_user.user_id
    ).first()
    if not my_membership or my_membership.role not in ["admin", "manager"]:
        flash("You do not have permission to invite members.", "danger")
        return redirect(
            url_for("workspace_bp.view_workspace", workspace_id=workspace_id)
        )

    from app.auth.routes import send_email

    invited_user = User.query.filter_by(email=email).first()
    
    # generate one token for either branch
    invitation_token = secrets.token_urlsafe(32)

    # For existing users:
    if invited_user:
        # Check if membership already exists
        existing_membership = WorkspaceMember.query.filter_by(
            workspace_id=workspace_id, user_id=invited_user.user_id
        ).first()
        if existing_membership:
            flash(
                "That user is already in the workspace or has been invited.", "warning"
            )
            return redirect(
                url_for("workspace_bp.view_workspace", workspace_id=workspace_id)
            )

        # Create membership with status 'invited'
        new_membership = WorkspaceMember(
            workspace_id=workspace_id,
            user_id=invited_user.user_id,
            role="user",
            status="invited",
        )
        db.session.add(new_membership)
        db.session.commit()

        # Create an Invitation record for existing users too
        invitation_token = secrets.token_urlsafe(32)
        invitation = Invitation(
            token=invitation_token,
            workspace_id=workspace_id,
            inviter_id=current_user.user_id,       # supply inviter
            email=email,
            custom_message=custom_message,
        )
        db.session.add(invitation)
        db.session.commit()

        # Build a link to the respond invitation page
        invitation_link = url_for(
            "workspace_bp.respond_invitation",
            invitation_id=invitation.id,
            _external=True,
        )
        email_body = f"""
        <p>Hello {invited_user.first_name or invited_user.username},</p>
        <p>You have been invited to join the workspace <strong>{workspace.name}</strong> on {APP_NAME}.</p>
        <p>{custom_message}</p>
        <p>Please click the link below to accept or decline the invitation:</p>
        <p><a href="{invitation_link}">Respond to Invitation</a></p>
        """
        send_email(
            to_address=email,
            subject=f"Invitation to Join {APP_NAME} Workspace",
            body_html=email_body,
        )
        flash(f"Invitation sent to {email}.", "success")
    else:
        # For users who do not exist, create an Invitation record and send registration link.
        invitation = Invitation(
            token=invitation_token,
            workspace_id=workspace_id,
            email=email,
            inviter_id=current_user.user_id,   # <— supply the inviter
            custom_message=custom_message,   # <— supply the custom message
        )

        db.session.add(invitation)
        db.session.commit()

        registration_link = url_for(
            "auth_bp.register",
            invitation_token=invitation_token,
            workspace_id=workspace_id,
            _external=True,
        )
        email_body = f"""
        <p>Hello,</p>
        <p>You have been invited to join the workspace <strong>{workspace.name}</strong> on {APP_NAME}.</p>
        <p>{custom_message}</p>
        <p>Please register using the following link. Once you register, you'll be automatically added to the workspace:</p>
        <p><a href="{registration_link}">Register on {APP_NAME}</a></p>
        """
        send_email(
            to_address=email,
            subject=f"Invitation to Join {APP_NAME} Workspace",
            body_html=email_body,
        )
        flash(f'Invitation email sent to {email}. <a target="_blank" href="https://ai.broadcomms.net/webmail">Check Email</a>', "success")

    return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

###################################
# 6. Request to Join Workspace
###################################
@workspace_bp.route("/request_join/<int:workspace_id>", methods=["POST"])
@login_required
def request_join(workspace_id):
    """
    Handles requests to join a public workspace.
    """
    workspace = Workspace.query.get_or_404(workspace_id)

    # Check if the user is already a member
    existing_member = WorkspaceMember.query.filter_by(
        workspace_id=workspace_id, user_id=current_user.user_id
    ).first()
    if existing_member:
        flash("You are already a member of this workspace.", "info")
        return redirect(url_for("workspace_bp.list_workspaces"))

    # Add the user as a pending member
    new_member = WorkspaceMember(
        workspace_id=workspace_id,
        user_id=current_user.user_id,
        role="user",
        status="requested",
    )
    db.session.add(new_member)
    db.session.commit()

    flash("Your request to join the workspace has been sent.", "success")
    return redirect(url_for("workspace_bp.list_workspaces"))

###################################
# 7. Approve Member Request/Invite
###################################
@workspace_bp.route("/<int:workspace_id>/members/<int:member_id>/approve", methods=["POST"])
@login_required
def approve_member(workspace_id, member_id):
    """Approves a pending member (status 'requested' or 'invited')."""
    if not check_admin_permission(workspace_id, current_user.user_id):
        flash("You don't have permission to manage members.", "danger")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    member = WorkspaceMember.query.filter_by(id=member_id, workspace_id=workspace_id).first_or_404()

    if member.status not in ['requested', 'invited']:
        flash("This member is not pending approval.", "warning")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    try:
        member.status = 'active'
        member.joined_timestamp = datetime.utcnow() # Set join time on approval
        db.session.commit()
        flash(f"Member {member.user.email} approved.", "success")
        # TODO: Optionally send an email notification to the approved user
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error approving member {member_id} in workspace {workspace_id}: {e}")
        flash("An error occurred while approving the member.", "danger")

    return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

###################################
# 8. Reject Member Request/Invite
###################################
@workspace_bp.route("/<int:workspace_id>/members/<int:member_id>/reject", methods=["POST"])
@login_required
def reject_member(workspace_id, member_id):
    """Rejects a pending member (status 'requested' or 'invited') by deleting the record."""
    if not check_admin_permission(workspace_id, current_user.user_id):
        flash("You don't have permission to manage members.", "danger")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    member = WorkspaceMember.query.filter_by(id=member_id, workspace_id=workspace_id).first_or_404()

    if member.status not in ['requested', 'invited']:
        flash("This member is not pending rejection.", "warning")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    try:
        email = member.user.email # Get email before deleting
        db.session.delete(member)
        db.session.commit()
        flash(f"Membership request/invitation for {email} rejected.", "success")
        # TODO: Optionally send an email notification about the rejection
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error rejecting member {member_id} in workspace {workspace_id}: {e}")
        flash("An error occurred while rejecting the member.", "danger")

    return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

###################################
# 9. Remove Active Member
###################################
@workspace_bp.route("/<int:workspace_id>/members/<int:member_id>/remove", methods=["POST"])
@login_required
def remove_member(workspace_id, member_id):
    """Removes an active member from the workspace."""
    workspace = Workspace.query.get_or_404(workspace_id) # Need workspace to check owner
    if not check_admin_permission(workspace_id, current_user.user_id):
        flash("You don't have permission to manage members.", "danger")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    member = WorkspaceMember.query.filter_by(id=member_id, workspace_id=workspace_id).first_or_404()

    # Prevent removing the workspace owner
    if member.user_id == workspace.owner_id:
        flash("The workspace owner cannot be removed.", "danger")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    # Prevent removing oneself (should leave workspace instead if that's implemented)
    if member.user_id == current_user.user_id:
        flash("You cannot remove yourself using this function.", "warning")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    if member.status != 'active':
        flash("This member is not active and cannot be removed this way.", "warning")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    try:
        email = member.user.email # Get email before deleting
        db.session.delete(member)
        db.session.commit()
        flash(f"Member {email} removed from the workspace.", "success")
        # TODO: Optionally send an email notification about the removal
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error removing member {member_id} from workspace {workspace_id}: {e}")
        flash("An error occurred while removing the member.", "danger")

    return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))


###################################
# 10. Change Member Role
###################################
@workspace_bp.route("/<int:workspace_id>/members/<int:member_id>/change_role", methods=["POST"])
@login_required
def change_role(workspace_id, member_id):
    """Changes the role of an active member (e.g., member <-> manager)."""
    workspace = Workspace.query.get_or_404(workspace_id) # Need workspace to check owner

    # --- Permission Check: Only Owner or Admin can change roles ---
    # Using a stricter check here - maybe only owner/admin can promote/demote
    is_owner = workspace.owner_id == current_user.user_id
    membership = WorkspaceMember.query.filter_by(
        workspace_id=workspace_id, user_id=current_user.user_id, status='active'
    ).first()
    is_admin = membership and membership.role == 'admin'

    if not (is_owner or is_admin):
        flash("You do not have permission to change member roles.", "danger")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    member = WorkspaceMember.query.filter_by(id=member_id, workspace_id=workspace_id).first_or_404()
    new_role = request.form.get("new_role")

    # Validate new role
    allowed_roles = ['member', 'manager', 'admin'] # Define roles that can be assigned
    if new_role not in allowed_roles:
        flash("Invalid role specified.", "danger")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    # Prevent changing the owner's role (owner should always be admin implicitly or explicitly)
    if member.user_id == workspace.owner_id:
        flash("The workspace owner's role cannot be changed.", "danger")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    # Prevent non-owners from promoting someone to 'admin' (only owner can make admins)
    if new_role == 'admin' and not is_owner:
         flash("Only the workspace owner can assign the 'admin' role.", "danger")
         return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    # Prevent demoting the last admin if you are not the owner (optional rule)
    # if member.role == 'admin' and not is_owner:
    #    admin_count = WorkspaceMember.query.filter_by(workspace_id=workspace_id, role='admin', status='active').count()
    #    if admin_count <= 1:
    #         flash("Cannot remove the last admin.", "danger")
    #         return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    if member.status != 'active':
        flash("Cannot change role for inactive members.", "warning")
        return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

    try:
        member.role = new_role
        db.session.commit()
        flash(f"Role for {member.user.email} updated to {new_role}.", "success")
        # TODO: Optionally send an email notification about the role change
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error changing role for member {member_id} in workspace {workspace_id}: {e}")
        flash("An error occurred while changing the role.", "danger")

    return redirect(url_for("workspace_bp.view_workspace", workspace_id=workspace_id))

##############################################################################
# View Member Profile
##############################################################################

# Helper to build absolute path for images TODO: Consider
def path_exists_in_static(relative_path: str) -> bool:
    """
    Given a relative path (e.g., "uploads/profile_pics/3_user.png"),
    build the absolute path using current_app.static_folder and check if the file exists.
    """
    if not relative_path:
        return False
    # Ensure the path doesn't start with a slash if it's meant to be relative to static_folder
    relative_path = relative_path.lstrip('/')
    full_path = os.path.join(current_app.static_folder, relative_path)
    return os.path.isfile(full_path)

@workspace_bp.route("/members/<int:user_id>")
@login_required
def member_profile(user_id):
    """
    Renders a specific user's profile.
    If the member's profile_pic_url is invalid, reset it to the default.
    """
    member = User.query.get_or_404(user_id)
    # Use a default image path if the specific one doesn't exist or is empty
    if not member.profile_pic_url or not path_exists_in_static(member.profile_pic_url):
        member.profile_pic_url = "images/default-profile.png" # Assuming default is in static/images

    # Pass the user object as 'user' to the template
    return render_template("workspace_member.html", member=member) # Changed 'member=member' to 'user=member'

