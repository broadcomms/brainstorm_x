# app/workshop/routes.py
import os, markdown, json, re
from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    current_app,
    abort,
    jsonify,
)  
from markupsafe import escape
from flask_login import login_required, current_user

from app.extensions import db, socketio
from app.models import (
    BrainstormIdea,
    BrainstormTask,
    Workshop,
    Workspace,
    User,
    WorkspaceMember,
    WorkshopParticipant,
    Document,
    WorkshopDocument
)
from datetime import datetime, timedelta
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_
from app.auth.routes import send_email  # TODO: Move send_email from auth to a extension module
from datetime import datetime  # Import datetime

# Import generate agenda text
from app.service.routes.agenda import generate_agenda_text
# Import aggregate_pre_workshop_data from the new utils file ---
from app.utils.data_aggregation import aggregate_pre_workshop_data
# Import extract_json_block
from app.service.routes.agent import extract_json_block 

from app.service.routes.agent import (
    generate_icebreaker_text,
    generate_tip_text,
    generate_introduction_text,
    generate_next_task_text,
    generate_action_plan_text
) 

from concurrent.futures import ThreadPoolExecutor
# Create a thread pool for asynchronous generation
executor = ThreadPoolExecutor(max_workers=4)

APP_NAME = os.getenv("APP_NAME", "BrainStormX")
workshop_bp = Blueprint("workshop_bp", __name__, template_folder="templates")

def load_or_schedule_ai_content(workshop, attr, generator_func, event_type):
    """
    Loads AI content from the workshop instance if present, otherwise
    schedules asynchronous generation and returns a placeholder.

    workshop: Workshop instance
    attr: string name of the Workshop field, e.g. 'agenda' or 'rules'
    generator_func: function(workshop_id) -> raw text
    event_type: string used for socket event type, e.g. 'agenda', 'rules'
    """
    raw = getattr(workshop, attr)
    if raw:
        # Already generated: convert to HTML
        return markdown.markdown(raw)

    # Schedule background generation
    def _generate_and_emit():
        new_raw = generator_func(workshop.id)
        # Basic success check
        if new_raw and not new_raw.startswith(("Could not generate", "No pre")):
            setattr(workshop, attr, new_raw)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                return
            # Emit update to clients in lobby
            new_html = markdown.markdown(new_raw)
            socketio.emit('ai_content_update', {
                'workshop_id': workshop.id,
                'type': event_type,
                'content': new_html
            }, room=f'workshop_lobby_{workshop.id}')
    executor.submit(_generate_and_emit)
    # Return placeholder while generating
    return '<em>Generating...</em>'


# --- Helper Function to Check Organizer ---
def is_organizer(workshop, user):
    # Organizer is the creator in this setup
    return workshop.created_by_id == user.user_id


# --- Helper to get user's workspaces ---
def get_user_active_workspaces(user_id):
    """Returns a list of Workspace objects the user is an active member of."""
    return (
        Workspace.query.join(WorkspaceMember)
        .filter(WorkspaceMember.user_id == user_id, WorkspaceMember.status == "active")
        .order_by(Workspace.name)
        .all()
    )


## --- OPTIONAL HELPERS SHOULD IN CASE: --- ##
# --- Helper to get user's active workshops ---
def get_user_active_workshops(user_id):
    """Returns a list of Workshop objects the user is an active participant in."""
    return (
        Workshop.query.join(WorkshopParticipant)
        .filter(
            WorkshopParticipant.user_id == user_id,
            WorkshopParticipant.status == "accepted",
        )
        .order_by(Workshop.date_time.desc())
        .all()
    )


# --- Helper to get user's active workshop invitations ---
def get_user_active_invitations(user_id):
    """Returns a list of WorkshopParticipant objects the user has an active invitation."""
    return WorkshopParticipant.query.filter(
        WorkshopParticipant.user_id == user_id,
        WorkshopParticipant.status == "invited",
        WorkshopParticipant.token_expires > datetime.utcnow(),
    ).all()


# --- Helper to get user's active workshop documents ---
def get_user_active_documents(user_id):
    """Returns a list of Document objects the user has access to."""
    return (
        Document.query.join(WorkshopDocument)
        .join(WorkshopParticipant)
        .filter(
            WorkshopParticipant.user_id == user_id,
            WorkshopParticipant.status == "accepted",
        )
        .order_by(Document.uploaded_at.desc())
        .all()
    )




# --- NEW: Endpoint to get raw action plan JSON ---
@workshop_bp.route("/<int:workshop_id>/get_raw_action_plan", methods=["GET"])
@login_required
def get_raw_action_plan(workshop_id):
    # Basic permission check: Ensure user can view the workshop
    workshop = Workshop.query.get_or_404(workshop_id)
    participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop.id, user_id=current_user.user_id
    ).first()
    if not participant:
         # Or check workspace membership if that's the rule
        return jsonify({"success": False, "message": "Permission denied"}), 403

    raw_json = workshop.task_sequence or '[]'
    return jsonify({"success": True, "raw_json": raw_json})


# --- 1a. Create Workshop (From General List) ---
@workshop_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_workshop_general():
    user_workspaces = get_user_active_workspaces(current_user.user_id)

    if not user_workspaces:
        flash(
            "You must be an active member of at least one workspace to create a workshop.",
            "danger",
        )
        return redirect(url_for("workshop_bp.list_workshops"))  # Or account page

    if request.method == "POST":
        workspace_id = request.form.get("workspace_id", type=int)
        title = request.form.get("title", "").strip()
        objective = request.form.get("objective", "").strip()
        date_time_str = request.form.get("date_time", "").strip()
        duration = request.form.get("duration", type=int)
        agenda = request.form.get("agenda", "").strip()

        # --- Validation ---
        if not workspace_id or not title or not date_time_str:
            flash("Workspace, Workshop title, and date/time are required.", "danger")
            return render_template(
                "workshop_create.html",
                workspaces=user_workspaces,
                show_workspace_select=True,  # Flag for template
                form_data=request.form,  # Repopulate form
            )

        # Verify selected workspace_id is valid for the user
        if not any(ws.workspace_id == workspace_id for ws in user_workspaces):
            flash("Invalid workspace selected.", "danger")
            return render_template(
                "workshop_create.html",
                workspaces=user_workspaces,
                show_workspace_select=True,
                form_data=request.form,
            )

        try:
            date_time = datetime.strptime(date_time_str, "%Y-%m-%d %H:%M")
        except ValueError:
            flash("Invalid date/time format. Please use YYYY-MM-DD HH:MM.", "danger")
            return render_template(
                "workshop_create.html",
                workspaces=user_workspaces,
                show_workspace_select=True,
                form_data=request.form,
            )

        try:
            new_workshop = Workshop(
                title=title,
                objective=objective,
                workspace_id=workspace_id,  # Use ID from form
                date_time=date_time,
                duration=duration,
                agenda=agenda,
                created_by_id=current_user.user_id,
                status="scheduled",
            )
            db.session.add(new_workshop)
            db.session.flush()

            organizer_participant = WorkshopParticipant(
                workshop_id=new_workshop.id,
                user_id=current_user.user_id,
                role="organizer",
                status="accepted",
                joined_timestamp=datetime.utcnow(),
            )
            db.session.add(organizer_participant)
            db.session.commit()

            flash(f"Workshop '{title}' created successfully!", "success")
            return redirect(
                url_for("workshop_bp.view_workshop", workshop_id=new_workshop.id)
            )

        except IntegrityError as e:
            db.session.rollback()
            current_app.logger.error(f"Database error creating workshop: {e}")
            flash("A database error occurred. Please try again.", "danger")
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error creating workshop: {e}")
            flash("An unexpected error occurred while creating the workshop.", "danger")

        return render_template(
            "workshop_create.html",
            workspaces=user_workspaces,
            show_workspace_select=True,
            form_data=request.form,
        )

    # GET request
    return render_template(
        "workshop_create.html",
        workspaces=user_workspaces,
        show_workspace_select=True,  # Flag for template
    )





# --- 1b. Create Workshop (From Specific Workspace) ---
@workshop_bp.route(
    "/create/in/<int:workspace_id>", methods=["GET", "POST"]
)  # Changed route slightly for clarity
@login_required
def create_workshop_specific(workspace_id):
    workspace = Workspace.query.get_or_404(workspace_id)

    # Verify user is a member of the workspace
    membership = WorkspaceMember.query.filter_by(
        workspace_id=workspace_id, user_id=current_user.user_id, status="active"
    ).first()
    if not membership:
        flash(
            "You must be an active member of the workspace to create a workshop.",
            "danger",
        )
        return redirect(
            url_for("workspace_bp.view_workspace", workspace_id=workspace_id)
        )

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        objective = request.form.get("objective", "").strip()
        date_time_str = request.form.get("date_time", "").strip()
        duration = request.form.get("duration", type=int)
        agenda = request.form.get("agenda", "").strip()

        if not title or not date_time_str:
            flash("Workshop title and date/time are required.", "danger")
            return render_template(
                "workshop_create.html",
                workspace=workspace,  # Pass specific workspace
                show_workspace_select=False,  # Don't show select
                form_data=request.form,
            )

        try:
            date_time = datetime.strptime(date_time_str, "%Y-%m-%d %H:%M")
        except ValueError:
            flash("Invalid date/time format. Please use YYYY-MM-DD HH:MM.", "danger")
            return render_template(
                "workshop_create.html",
                workspace=workspace,
                show_workspace_select=False,
                form_data=request.form,
            )

        try:
            new_workshop = Workshop(
                title=title,
                objective=objective,
                workspace_id=workspace.workspace_id,  # Use ID from context
                date_time=date_time,
                duration=duration,
                agenda=agenda,
                created_by_id=current_user.user_id,
                status="scheduled",
            )
            db.session.add(new_workshop)
            db.session.flush()

            organizer_participant = WorkshopParticipant(
                workshop_id=new_workshop.id,
                user_id=current_user.user_id,
                role="organizer",
                status="accepted",
                joined_timestamp=datetime.utcnow(),
            )
            db.session.add(organizer_participant)
            db.session.commit()

            flash(f"Workshop '{title}' created successfully!", "success")
            return redirect(
                url_for("workshop_bp.view_workshop", workshop_id=new_workshop.id)
            )

        except IntegrityError as e:
            db.session.rollback()
            current_app.logger.error(f"Database error creating workshop: {e}")
            flash("A database error occurred. Please try again.", "danger")
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error creating workshop: {e}")
            flash("An unexpected error occurred while creating the workshop.", "danger")

        return render_template(
            "workshop_create.html",
            workspace=workspace,
            show_workspace_select=False,
            form_data=request.form,
        )

    # GET request
    return render_template(
        "workshop_create.html",
        workspace=workspace,  # Pass specific workspace
        show_workspace_select=False,  # Don't show select
    )


# --- List Workshops ---
@workshop_bp.route("/list")
@login_required
def list_workshops():
    """Lists workshops the current user created or is participating in."""
    user_id = current_user.user_id

    # Find workshops where the user is a participant (accepted or invited)
    # Also include workshops created by the user, even if they aren't explicitly a participant (though they should be via the organizer record)
    workshops_query = (
        Workshop.query.options(
            joinedload(Workshop.workspace), joinedload(Workshop.creator)
        )
        .join(WorkshopParticipant, Workshop.id == WorkshopParticipant.workshop_id)
        .filter(
            or_(
                Workshop.created_by_id == user_id,
                WorkshopParticipant.user_id == user_id,
            ),
            # Optional: Filter by participant status if needed
            # WorkshopParticipant.status.in_(['accepted', 'invited', 'organizer'])
        )
        .order_by(Workshop.date_time.desc())  # Show upcoming/recent first
        .distinct()
    )  # Avoid duplicates if user is creator AND participant

    user_workshops = workshops_query.all()

    return render_template("workshop_list.html", workshops=user_workshops)


# --- 2. View Workshop Details ---
@workshop_bp.route("/<int:workshop_id>")
@login_required
def view_workshop(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)

    # --- Permission Check: Must be a member of the workspace ---
    workspace_membership = WorkspaceMember.query.filter_by(
        workspace_id=workshop.workspace_id,
        user_id=current_user.user_id,
        status="active",
    ).first()
    if not workspace_membership:
        flash("You do not have permission to view this workshop.", "danger")
        return redirect(url_for("workspace_bp.list_workspaces"))

    # Check if the current user is the organizer
    user_is_organizer = is_organizer(workshop, current_user)

    # Prepare participant data
    participants = workshop.participants.all()

    participant_ids = {p.user_id for p in participants}

    # Get workspace members who are NOT already participants, for the "Add Participant" modal
    potential_participants = (
        User.query.join(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == workshop.workspace_id,
            WorkspaceMember.status == "active",
            ~User.user_id.in_(participant_ids),  # Exclude users already participating
        )
        .all()
    )

    # Get workspace documents that are NOT already linked, for the "Add Document" modal
    linked_document_ids = {ld.document_id for ld in workshop.linked_documents}
    available_documents = (
        Document.query.filter(
            Document.workspace_id == workshop.workspace_id,
            ~Document.id.in_(linked_document_ids),
        )
        .order_by(Document.uploaded_at.desc())
        .all()
    )
    linked_docs = workshop.linked_documents.all()
    
    # --- Render Action Plan HTML ---
    action_plan_html = render_action_plan_html(workshop.task_sequence)
    # --- Store raw JSON for JS ---
    raw_action_plan_json = workshop.task_sequence or '[]' # Default to empty JSON array string

    
    
    return render_template(
        "workshop_details.html",
        workshop=workshop,
        participants=participants,
        potential_participants=potential_participants,
        available_documents=available_documents,
        linked_documents=linked_docs,
        user_is_organizer=user_is_organizer,
        action_plan_html=action_plan_html, # Pass rendered HTML
        raw_action_plan_json=raw_action_plan_json # Pass raw JSON string for JS
    )


# --- 3. Edit Workshop ---
@workshop_bp.route("/edit/<int:workshop_id>", methods=["GET", "POST"])
@login_required
def edit_workshop(workshop_id):
    workshop = Workshop.query.options(joinedload(Workshop.workspace)).get_or_404(
        workshop_id
    )

    # --- Permission Check: Only Organizer ---
    if not is_organizer(workshop, current_user):
        flash("Only the workshop organizer can edit it.", "danger")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        objective = request.form.get("objective", "").strip()
        date_time_str = request.form.get("date_time", "").strip()
        duration = request.form.get("duration", type=int)
        agenda = request.form.get("agenda", "").strip()
        status = request.form.get("status", "").strip()  # Allow status update

        # --- Validation ---
        if not title or not date_time_str:
            flash("Workshop title and date/time are required.", "danger")
            # Pass back form data for repopulation
            return render_template(
                "workshop_edit.html",
                workshop=workshop,
                form_data=request.form,  # Pass form data
            )

        try:
            date_time = datetime.strptime(date_time_str, "%Y-%m-%d %H:%M")
        except ValueError:
            flash("Invalid date/time format. Please use YYYY-MM-DD HH:MM.", "danger")
            return render_template(
                "workshop_edit.html",
                workshop=workshop,
                form_data=request.form,  # Pass form data
            )

        # Validate status
        allowed_statuses = [
            "scheduled",
            "inprogress",
            "paused",
            "completed",
            "cancelled",
        ]
        if status not in allowed_statuses:
            flash(f"Invalid status provided.", "danger")
            return render_template(
                "workshop_edit.html",
                workshop=workshop,
                form_data=request.form,  # Pass form data
            )

        try:
            workshop.title = title
            workshop.objective = objective
            workshop.date_time = date_time
            workshop.duration = duration
            workshop.agenda = agenda
            workshop.status = status
            workshop.updated_at = datetime.utcnow()  # Explicitly set update time

            db.session.commit()
            flash("Workshop details updated successfully!", "success")
            return redirect(
                url_for("workshop_bp.view_workshop", workshop_id=workshop_id)
            )

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error updating workshop {workshop_id}: {e}")
            flash("An error occurred while updating the workshop.", "danger")
            # Pass back form data on error too
            return render_template(
                "workshop_edit.html", workshop=workshop, form_data=request.form
            )

    # GET request - format datetime for the input field
    # Use workshop data directly, form_data is only for POST errors
    workshop.date_time_str = (
        workshop.date_time.strftime("%Y-%m-%d %H:%M") if workshop.date_time else ""
    )
    return render_template(
        "workshop_edit.html", workshop=workshop, form_data=None
    )  # Pass None for form_data on GET


# --- 4. Delete Workshop ---
@workshop_bp.route("/delete/<int:workshop_id>", methods=["POST"])
@login_required
def delete_workshop(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    workspace_id = workshop.workspace_id  # Get workspace ID before deleting

    # --- Permission Check: Only Organizer ---
    if not is_organizer(workshop, current_user):
        flash("Only the workshop organizer can delete it.", "danger")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    try:
        # Cascade delete should handle participants and document links
        db.session.delete(workshop)
        db.session.commit()
        flash(f"Workshop '{workshop.title}' deleted successfully.", "success")
        # Redirect to the list view after deleting from list/view page
        # Or redirect to workspace if deleting from workspace view (need context?)
        # Let's redirect to the list view for simplicity now.
        return redirect(url_for("workshop_bp.list_workshops"))

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error deleting workshop {workshop_id}: {e}")
        flash("An error occurred while deleting the workshop.", "danger")
        # Redirect back to list view on error
        return redirect(url_for("workshop_bp.list_workshops"))


# --- 5. Add Participant ---
@workshop_bp.route("/<int:workshop_id>/add_participant", methods=["POST"])
@login_required
def add_participant(workshop_id):
    workshop = Workshop.query.options(joinedload(Workshop.workspace)).get_or_404(
        workshop_id
    )

    # --- Permission Check: Only Organizer ---
    if not is_organizer(workshop, current_user):
        flash("Only the workshop organizer can add participants.", "danger")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    user_id_to_add = request.form.get("user_id", type=int)
    if not user_id_to_add:
        flash("No user selected to add.", "warning")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    # Verify the user exists and is a member of the workspace
    user_to_add = (
        User.query.join(WorkspaceMember)
        .filter(
            User.user_id == user_id_to_add,
            WorkspaceMember.workspace_id == workshop.workspace_id,
            WorkspaceMember.status == "active",
        )
        .first()
    )

    if not user_to_add:
        flash("Selected user is not a valid active member of this workspace.", "danger")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    # Check if already a participant
    existing_participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop_id, user_id=user_id_to_add
    ).first()
    if existing_participant:
        flash(
            f"{user_to_add.email} is already a participant or has been invited.",
            "warning",
        )
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    try:
        new_participant = WorkshopParticipant(
            workshop_id=workshop_id,
            user_id=user_id_to_add,
            role="participant",
            status="invited",
        )
        new_participant.generate_token()  # Create invitation token
        db.session.add(new_participant)
        db.session.commit()

        # Send invitation email
        invitation_link = url_for(
            "workshop_bp.respond_invitation",
            token=new_participant.invitation_token,
            _external=True,
        )
        email_body = f"""
        <p>Hello {user_to_add.first_name or user_to_add.email},</p>
        <p>You have been invited to participate in the workshop "<strong>{workshop.title}</strong>"
           scheduled for {workshop.date_time.strftime('%Y-%m-%d %H:%M')} in the workspace "{workshop.workspace.name}".</p>
        <p>Please click the link below to accept or decline the invitation:</p>
        <p><a href="{invitation_link}">Respond to Workshop Invitation</a></p>
        <p>This link will expire in 7 days.</p>
        """
        send_email(
            to_address=user_to_add.email,
            subject=f"Invitation to Workshop: {workshop.title}",
            body_html=email_body,
        )

        flash(f"Invitation sent to {user_to_add.email}.", "success")

    except IntegrityError:
        db.session.rollback()
        flash(
            "Could not add participant due to a database conflict (they might already be invited).",
            "warning",
        )
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            f"Error adding participant {user_id_to_add} to workshop {workshop_id}: {e}"
        )
        flash("An error occurred while adding the participant.", "danger")

    return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))


# --- 6. Remove Participant ---
@workshop_bp.route(
    "/<int:workshop_id>/remove_participant/<int:participant_id>", methods=["POST"]
)
@login_required
def remove_participant(workshop_id, participant_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    participant_to_remove = WorkshopParticipant.query.get_or_404(participant_id)

    # --- Permission Check: Only Organizer ---
    if not is_organizer(workshop, current_user):
        flash("Only the workshop organizer can remove participants.", "danger")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    # Ensure the participant belongs to the correct workshop
    if participant_to_remove.workshop_id != workshop_id:
        abort(404)  # Or flash an error

    # Prevent organizer from removing themselves (they should delete the workshop instead)
    if participant_to_remove.role == "organizer":
        flash(
            "The organizer cannot be removed. Delete the workshop instead.", "warning"
        )
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    try:
        user_email = participant_to_remove.user.email  # Get email before deleting
        db.session.delete(participant_to_remove)
        db.session.commit()
        flash(f"Participant {user_email} removed successfully.", "success")
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            f"Error removing participant {participant_id} from workshop {workshop_id}: {e}"
        )
        flash("An error occurred while removing the participant.", "danger")

    return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))


# --- 7. Respond to Invitation ---
@workshop_bp.route("/invitation/<token>", methods=["GET"])
@login_required  # User must be logged in to respond
def respond_invitation(token):
    participant_record = WorkshopParticipant.query.filter_by(
        invitation_token=token
    ).first()

    if not participant_record or not participant_record.is_token_valid():
        flash("Invalid or expired invitation token.", "danger")
        return redirect(url_for("account_bp.account"))  # Redirect to a sensible page

    # Ensure the logged-in user matches the invitation
    if participant_record.user_id != current_user.user_id:
        flash("This invitation is for a different user.", "danger")
        return redirect(url_for("account_bp.account"))

    # Allow choosing accept/decline via query parameters or render a simple page
    action = request.args.get("action")  # e.g., ?action=accept or ?action=decline

    if action == "accept":
        participant_record.status = "accepted"
        participant_record.joined_timestamp = datetime.utcnow()
        participant_record.invitation_token = None  # Invalidate token
        participant_record.token_expires = None
        db.session.commit()
        flash(
            f"You have accepted the invitation to workshop '{participant_record.workshop.title}'.",
            "success",
        )
        return redirect(
            url_for(
                "workshop_bp.view_workshop", workshop_id=participant_record.workshop_id
            )
        )
    elif action == "decline":
        participant_record.status = "declined"
        participant_record.invitation_token = None  # Invalidate token
        participant_record.token_expires = None
        db.session.commit()
        flash(
            f"You have declined the invitation to workshop '{participant_record.workshop.title}'.",
            "info",
        )
        return redirect(url_for("account_bp.account"))  # Redirect to account page
    else:
        # Render a simple confirmation page if no action specified
        return render_template(
            "respond_invitation.html", participant_record=participant_record
        )


# --- 8. Add Document Link ---
@workshop_bp.route("/<int:workshop_id>/add_document", methods=["POST"])
@login_required
def add_document_link(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)

    # --- Permission Check: Only Organizer ---
    if not is_organizer(workshop, current_user):
        flash("Only the workshop organizer can add documents.", "danger")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    document_id_to_add = request.form.get("document_id", type=int)
    if not document_id_to_add:
        flash("No document selected to add.", "warning")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    # Verify the document exists and belongs to the same workspace
    document_to_add = Document.query.filter_by(
        id=document_id_to_add, workspace_id=workshop.workspace_id
    ).first()

    if not document_to_add:
        flash(
            "Selected document is not valid or does not belong to this workspace.",
            "danger",
        )
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    # Check if already linked
    existing_link = WorkshopDocument.query.filter_by(
        workshop_id=workshop_id, document_id=document_id_to_add
    ).first()
    if existing_link:
        flash(
            f"Document '{document_to_add.title}' is already linked to this workshop.",
            "warning",
        )
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    try:
        new_link = WorkshopDocument(
            workshop_id=workshop_id, document_id=document_id_to_add
        )
        db.session.add(new_link)
        db.session.commit()
        flash(f"Document '{document_to_add.title}' linked successfully.", "success")

    except IntegrityError:
        db.session.rollback()
        flash("Could not link document due to a database conflict.", "warning")
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            f"Error linking document {document_id_to_add} to workshop {workshop_id}: {e}"
        )
        flash("An error occurred while linking the document.", "danger")

    return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))


# --- 9. Remove Document Link ---
@workshop_bp.route("/<int:workshop_id>/remove_document/<int:link_id>", methods=["POST"])
@login_required
def remove_document_link(workshop_id, link_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    link_to_remove = WorkshopDocument.query.get_or_404(link_id)

    # --- Permission Check: Only Organizer ---
    if not is_organizer(workshop, current_user):
        flash("Only the workshop organizer can remove documents.", "danger")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    # Ensure the link belongs to the correct workshop
    if link_to_remove.workshop_id != workshop_id:
        abort(404)

    try:
        doc_title = link_to_remove.document.title  # Get title before deleting
        db.session.delete(link_to_remove)
        db.session.commit()
        flash(f"Document link for '{doc_title}' removed successfully.", "success")
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            f"Error removing document link {link_id} from workshop {workshop_id}: {e}"
        )
        flash("An error occurred while removing the document link.", "danger")

    return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))


# ################################
# Workshop Lifecycle Routes (Lobby, Room, Report)
# ################################


@workshop_bp.route("/join/<int:workshop_id>")
@login_required
def join_workshop(workshop_id):
    """
    Handles a user clicking the 'Join' button.
    Redirects to lobby if not started, room if in progress.
    """
    workshop = Workshop.query.get_or_404(workshop_id)
    participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop.id, user_id=current_user.user_id
    ).first()

    # Basic permission check: Must be a participant (invited or accepted)
    if not participant:
        flash("You are not a participant in this workshop.", "danger")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    # Check workspace membership (optional but good practice)
    is_member = current_user.workspace_memberships.filter_by(
        workspace_id=workshop.workspace_id, status="active"
    ).first()
    if not is_member:
        flash("You must be an active member of the workspace to join.", "danger")
        return redirect(
            url_for("workspace_bp.view_workspace", org_id=workshop.workspace_id)
        )

    # Redirect based on status
    if workshop.status == "scheduled":
        # Mark participant status if needed (e.g., 'joined_lobby') - Optional
        # participant.status = 'joined_lobby' # Example
        # db.session.commit()
        return redirect(url_for("workshop_bp.workshop_lobby", workshop_id=workshop_id))
    elif workshop.status == "inprogress":
        # Mark participant status if needed (e.g., 'in_room') - Optional
        # participant.status = 'in_room' # Example
        # db.session.commit()
        return redirect(url_for("workshop_bp.workshop_room", workshop_id=workshop_id))
    elif workshop.status == "completed":
        flash("This workshop has already been completed.", "info")
        return redirect(url_for("workshop_bp.workshop_report", workshop_id=workshop_id))
    elif workshop.status == "cancelled":
        flash("This workshop has been cancelled.", "warning")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))
    elif workshop.status == "paused":
        return redirect(url_for("workshop_bp.workshop_room", workshop_id=workshop_id))
    else:
        # Handle other statuses if necessary
        flash(
            f"Workshop is currently in status: {workshop.status}. Cannot join at this time.",
            "warning",
        )
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))







@workshop_bp.route("/lobby/<int:workshop_id>")
@login_required
def workshop_lobby(workshop_id):
    """Displays the waiting lobby for a scheduled workshop with AI content slots."""
    # Load workshop with eager relationships
    workshop = Workshop.query.options(
        joinedload(Workshop.creator),
        joinedload(Workshop.workspace),
    ).get_or_404(workshop_id)
    
    # Now load participants (with their User) via a normal query:
    participants = WorkshopParticipant.query.options(
        joinedload(WorkshopParticipant.user)
    ).filter_by(workshop_id=workshop.id).all()
    
     # And load linked documents (with their Document) explicitly:
    linked_docs = WorkshopDocument.query.options(
        joinedload(WorkshopDocument.document)
    ).filter_by(workshop_id=workshop.id).all()

    # Check if the user is a participant using the preloaded data
    participant = next((p for p in workshop.participants if p.user_id == current_user.user_id), None)

    # Permission checks
    if not participant:
        flash("You are not a participant in this workshop.", "danger")
        return redirect(url_for("workshop_bp.list_workshops"))
    

    # Status checks and redirects
    if workshop.status == "inprogress":
        flash("Workshop already in progress. Joining room...", "info")
        return redirect(url_for("workshop_bp.workshop_room", workshop_id=workshop_id))
    elif workshop.status == "completed":
        flash("Workshop completed. Viewing report...", "info")
        return redirect(url_for("workshop_bp.workshop_report", workshop_id=workshop_id))
    elif workshop.status != "scheduled":
        flash(f"Workshop status is '{workshop.status}'. Cannot access lobby.", "warning")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    # --- AI Content: Load or Generate ---
    save_needed = False
    ai_rules_raw = None
    ai_icebreaker_raw = None
    ai_tip_raw = None
    ai_agenda_raw = None 

    # Agenda (Load or Generate)
    if workshop.agenda: # Check the existing agenda field first
        ai_agenda_raw = workshop.agenda
        current_app.logger.debug(f"Loaded agenda from DB for workshop {workshop_id}")
    else:
        current_app.logger.debug(f"Generating agenda for workshop {workshop_id}")
        ai_agenda_raw = generate_agenda_text(workshop_id) # Generate if missing
        if ai_agenda_raw and not ai_agenda_raw.startswith("Could not generate"):
            workshop.agenda = ai_agenda_raw # Save to the standard agenda field
            save_needed = True
        else:
            ai_agenda_raw = "Could not generate an agenda at this time." # Fallback
            current_app.logger.warning(f"Failed to generate agenda for workshop {workshop_id}")


    # Rules
    if workshop.rules:
        ai_rules_raw = workshop.rules
        current_app.logger.debug(f"Loaded rules from DB for workshop {workshop_id}")
    else:
        current_app.logger.debug(f"Generating rules for workshop {workshop_id}")
        ai_rules_raw = generate_rules_text(workshop_id) # Generate if missing
        # Basic check for generation success (adjust if your function returns specific errors)
        if ai_rules_raw and not ai_rules_raw.startswith("Could not generate"):
            workshop.rules = ai_rules_raw
            save_needed = True
        else:
             ai_rules_raw = "Could not generate rules at this time." # Provide fallback text
             current_app.logger.warning(f"Failed to generate rules for workshop {workshop_id}")

    # Icebreaker
    if workshop.icebreaker:
        ai_icebreaker_raw = workshop.icebreaker
        current_app.logger.debug(f"Loaded icebreaker from DB for workshop {workshop_id}")
    else:
        current_app.logger.debug(f"Generating icebreaker for workshop {workshop_id}")
        ai_icebreaker_raw = generate_icebreaker_text(workshop_id) # Generate if missing
        if ai_icebreaker_raw and not ai_icebreaker_raw.startswith("Could not generate"):
            workshop.icebreaker = ai_icebreaker_raw
            save_needed = True
        else:
            ai_icebreaker_raw = "Could not generate an icebreaker." # Fallback
            current_app.logger.warning(f"Failed to generate icebreaker for workshop {workshop_id}")

    # Tip (load or generate)
    if workshop.tip:
        ai_tip_raw = workshop.tip
        current_app.logger.debug(f"Loaded tip from DB for workshop {workshop_id}")
    else:
        current_app.logger.debug(f"Generating tip for workshop {workshop_id}")
        ai_tip_raw = generate_tip_text(workshop_id) # Generate if missing
        # Adjust check based on actual error/fallback message from generate_tip_text
        if ai_tip_raw and not ai_tip_raw.startswith("No pre‑workshop data found") and not ai_tip_raw.startswith("Could not generate"):
            workshop.tip = ai_tip_raw
            save_needed = True
        else:
            ai_tip_raw = "Could not generate a tip." # Fallback
            current_app.logger.warning(f"Failed to generate tip for workshop {workshop_id}")

    # Save to DB if any content was newly generated
    if save_needed:
        try:
            db.session.commit()
            current_app.logger.info(f"Saved newly generated AI content for workshop {workshop_id}")
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error saving generated AI content for workshop {workshop_id}: {e}")
            # Don't necessarily fail the request, but log the error
            flash("Could not save generated content. Please try refreshing.", "warning")

    # Convert raw text to Markdown HTML
    # Use the standard markdown filter for consistency
    ai_agenda_html = markdown.markdown(ai_agenda_raw or "No agenda available.")
    ai_rules_html = markdown.markdown(ai_rules_raw or "No rules available.")
    ai_icebreaker_html = markdown.markdown(ai_icebreaker_raw or "No icebreaker available.")
    ai_tip_html = markdown.markdown(ai_tip_raw or "No tip available.")


    # Get participants list for display
    participants = WorkshopParticipant.query.options(
        joinedload(WorkshopParticipant.user) # Eager load user details for participants
        ).filter_by(workshop_id=workshop.id).all()

    # Get linked documents (already loaded via joinedload on workshop query)
    linked_docs = workshop.linked_documents # Access the preloaded relationship
    
    # Check if current user is the organizer
    is_organizer_flag = workshop.created_by_id == current_user.user_id
    

    # Debugging print statement (optional)
    # print(f"Passing to template - Rules HTML: {ai_rules_html}")

    return render_template(
        "workshop_lobby.html",
        workshop=workshop,
        participants=participants,
        current_participant=participant,
        linked_documents=linked_docs,
        # --- FIX: Pass the correct HTML variables with matching names ---
        ai_agenda=ai_agenda_html,
        ai_rules=ai_rules_html,
        ai_icebreaker=ai_icebreaker_html,
        ai_tip=ai_tip_html,
        # -------------------------------------------------------------
        user_is_organizer=is_organizer_flag, # Pass organizer flag
    )
    
    



















# --- Add New Routes for Regenerating and Editing AI Content ---

# Helper function for permission check
def check_organizer_permission(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    if workshop.created_by_id != current_user.user_id:
        abort(403, description="You do not have permission to perform this action.")
    return workshop

@workshop_bp.route("/<int:workshop_id>/regenerate/rules", methods=["POST"])
@login_required
def regenerate_rules(workshop_id):
    workshop = check_organizer_permission(workshop_id)
    try:
        new_rules_raw = generate_rules_text(workshop_id)
        if not new_rules_raw.startswith("Could not generate"):
            workshop.rules = new_rules_raw
            db.session.commit()
            new_rules_html = markdown.markdown(new_rules_raw)
            # Emit WebSocket event (optional but good for real-time updates)
            socketio.emit('ai_content_update', {
                'workshop_id': workshop_id,
                'type': 'rules',
                'content': new_rules_html
            }, room=f'workshop_lobby_{workshop_id}')
            return jsonify({"success": True, "content": new_rules_html})
        else:
            return jsonify({"success": False, "message": "Failed to generate new rules."}), 500
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error regenerating rules for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error during regeneration."}), 500

@workshop_bp.route("/<int:workshop_id>/regenerate/icebreaker", methods=["POST"])
@login_required
def regenerate_icebreaker(workshop_id):
    workshop = check_organizer_permission(workshop_id)
    try:
        new_icebreaker_raw = generate_icebreaker_text(workshop_id)
        if not new_icebreaker_raw.startswith("Could not generate"):
            workshop.icebreaker = new_icebreaker_raw
            db.session.commit()
            new_icebreaker_html = markdown.markdown(new_icebreaker_raw)
            socketio.emit('ai_content_update', {
                'workshop_id': workshop_id,
                'type': 'icebreaker',
                'content': new_icebreaker_html
            }, room=f'workshop_lobby_{workshop_id}')
            return jsonify({"success": True, "content": new_icebreaker_html})
        else:
            return jsonify({"success": False, "message": "Failed to generate new icebreaker."}), 500
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error regenerating icebreaker for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error during regeneration."}), 500

@workshop_bp.route("/<int:workshop_id>/regenerate/tip", methods=["POST"])
@login_required
def regenerate_tip(workshop_id):
    workshop = check_organizer_permission(workshop_id)
    try:
        new_tip_raw = generate_tip_text(workshop_id)
        if not new_tip_raw.startswith("No pre‑workshop data found"):
            workshop.tip = new_tip_raw
            db.session.commit()
            new_tip_html = markdown.markdown(new_tip_raw)
            socketio.emit('ai_content_update', {
                'workshop_id': workshop_id,
                'type': 'tip',
                'content': new_tip_html
            }, room=f'workshop_lobby_{workshop_id}')
            return jsonify({"success": True, "content": new_tip_html})
        else:
            return jsonify({"success": False, "message": "Failed to generate new tip."}), 500
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error regenerating tip for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error during regeneration."}), 500


@workshop_bp.route("/<int:workshop_id>/edit/rules", methods=["POST"])
@login_required
def edit_rules(workshop_id):
    workshop = check_organizer_permission(workshop_id)
    edited_content = request.json.get('content')
    if edited_content is None:
        return jsonify({"success": False, "message": "No content provided."}), 400
    try:
        workshop.rules = edited_content # Store raw markdown/text
        db.session.commit()
        edited_content_html = markdown.markdown(edited_content)
        socketio.emit('ai_content_update', {
            'workshop_id': workshop_id,
            'type': 'rules',
            'content': edited_content_html
        }, room=f'workshop_lobby_{workshop_id}')
        return jsonify({"success": True, "content": edited_content_html})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving edited rules for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error saving edit."}), 500

@workshop_bp.route("/<int:workshop_id>/edit/icebreaker", methods=["POST"])
@login_required
def edit_icebreaker(workshop_id):
    workshop = check_organizer_permission(workshop_id)
    edited_content = request.json.get('content')
    if edited_content is None:
        return jsonify({"success": False, "message": "No content provided."}), 400
    try:
        workshop.icebreaker = edited_content
        db.session.commit()
        edited_content_html = markdown.markdown(edited_content)
        socketio.emit('ai_content_update', {
            'workshop_id': workshop_id,
            'type': 'icebreaker',
            'content': edited_content_html
        }, room=f'workshop_lobby_{workshop_id}')
        return jsonify({"success": True, "content": edited_content_html})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving edited icebreaker for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error saving edit."}), 500

@workshop_bp.route("/<int:workshop_id>/edit/tip", methods=["POST"])
@login_required
def edit_tip(workshop_id):
    workshop = check_organizer_permission(workshop_id)
    edited_content = request.json.get('content')
    if edited_content is None:
        return jsonify({"success": False, "message": "No content provided."}), 400
    try:
        workshop.tip = edited_content
        db.session.commit()
        edited_content_html = markdown.markdown(edited_content)
        socketio.emit('ai_content_update', {
            'workshop_id': workshop_id,
            'type': 'tip',
            'content': edited_content_html
        }, room=f'workshop_lobby_{workshop_id}')
        return jsonify({"success": True, "content": edited_content_html})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving edited tip for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error saving edit."}), 500


@workshop_bp.route("/<int:workshop_id>/regenerate/agenda", methods=["POST"])
@login_required
def regenerate_agenda(workshop_id):
    workshop = check_organizer_permission(workshop_id)
    try:
        new_agenda_raw = generate_agenda_text(workshop_id) # Call the new generator
        if not new_agenda_raw.startswith("Could not generate"):
            workshop.agenda = new_agenda_raw # Update the main agenda field
            db.session.commit()
            new_agenda_html = markdown.markdown(new_agenda_raw)
            socketio.emit('ai_content_update', {
                'workshop_id': workshop_id,
                'type': 'agenda', # <-- Use 'agenda' type
                'content': new_agenda_html
            }, room=f'workshop_lobby_{workshop_id}')
            return jsonify({"success": True, "content": new_agenda_html})
        else:
            return jsonify({"success": False, "message": "Failed to generate new agenda."}), 500
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error regenerating agenda for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error during regeneration."}), 500

@workshop_bp.route("/<int:workshop_id>/edit/agenda", methods=["POST"])
@login_required
def edit_agenda(workshop_id):
    workshop = check_organizer_permission(workshop_id)
    edited_content = request.json.get('content')
    if edited_content is None:
        return jsonify({"success": False, "message": "No content provided."}), 400
    try:
        workshop.agenda = edited_content # Update the main agenda field
        db.session.commit()
        edited_content_html = markdown.markdown(edited_content)
        socketio.emit('ai_content_update', {
            'workshop_id': workshop_id,
            'type': 'agenda', # <-- Use 'agenda' type
            'content': edited_content_html
        }, room=f'workshop_lobby_{workshop_id}')
        return jsonify({"success": True, "content": edited_content_html})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving edited agenda for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error saving edit."}), 500



# --- Helper Function to Render Action Plan HTML ---
def render_action_plan_html(action_plan_json_string):
    """
    Parses the action plan JSON string and returns an HTML list representation.
    Returns placeholder text if parsing fails or data is empty.
    """
    if not action_plan_json_string:
        return '<p class="text-muted mb-0">No action plan generated yet.</p>'

    try:
        # Clean potential markdown/fencing if LLM added it
        cleaned_json_string = extract_json_block(action_plan_json_string)
        action_plan_items = json.loads(cleaned_json_string)

        if not isinstance(action_plan_items, list) or not action_plan_items:
            return '<p class="text-muted mb-0">Action plan is empty or invalid.</p>'

        html_output = '<ul class="list-group action-plan-list">'
        for index, item in enumerate(action_plan_items):
            phase = escape(item.get('phase', 'N/A')) # Use escape for security
            description = escape(item.get('description', 'No description'))
            # Use index as a simple identifier for deletion
            html_output += f'''
            <li class="list-group-item d-flex align-items-center">
                <input class="form-check-input me-2" type="checkbox" value="{index}" id="action-item-{index}">
                <label class="form-check-label flex-grow-1" for="action-item-{index}">
                    <strong>{phase}:</strong> {description}
                </label>
            </li>
            '''
        html_output += '</ul>'
        return html_output

    except json.JSONDecodeError:
        current_app.logger.warning(f"Failed to decode action plan JSON: {action_plan_json_string[:100]}...")
        # Fallback: Render the raw string safely if JSON parsing fails
        return f'<div class="alert alert-warning">Could not parse action plan JSON. Raw content:</div><pre><code>{escape(action_plan_json_string)}</code></pre>'
    except Exception as e:
        current_app.logger.error(f"Error rendering action plan HTML: {e}")
        return '<p class="text-danger mb-0">Error rendering action plan.</p>'








@workshop_bp.route("/<int:workshop_id>/regenerate/action_plan", methods=["POST"])
@login_required
def regenerate_action_plan(workshop_id):
    workshop = check_organizer_permission(workshop_id)

    try:
        # --- MODIFIED: Call generator with force=True ---
        new_plan_raw = generate_action_plan_text(workshop_id, force=True)
        # ------------------------------------------------

        print("WORKSHOP Regenerating action plan for workshop:", new_plan_raw) # Keep for debugging if needed

        # --- Validation ---
        # Check for specific error messages from the generator
        if isinstance(new_plan_raw, str) and new_plan_raw.startswith("Could not generate"):
             current_app.logger.error(f"Failed to generate new action plan for workshop {workshop_id}: {new_plan_raw}")
             return jsonify({"success": False, "message": new_plan_raw}), 500

        # Attempt to parse to ensure it's valid JSON before saving (generator should return cleaned JSON now)
        try:
            json.loads(new_plan_raw) # Validate the string from the generator
            workshop.task_sequence = new_plan_raw # Save the validated JSON string
        except json.JSONDecodeError:
             current_app.logger.error(f"Regenerated action plan is invalid JSON for workshop {workshop_id}. Raw: {new_plan_raw[:100]}...")
             # Return error if the generator somehow still produced invalid JSON
             return jsonify({"success": False, "message": "Generated action plan was not valid JSON."}), 500

        db.session.commit()
        current_app.logger.info(f"Successfully regenerated and saved action plan for workshop {workshop_id}")

        # --- Render the HTML list from the new JSON string ---
        new_plan_html = render_action_plan_html(workshop.task_sequence)

        # Emit update via WebSocket
        socketio.emit('ai_content_update', {
            'workshop_id': workshop_id,
            'type': 'action_plan',
            'content': new_plan_html
        }, room=f'workshop_lobby_{workshop_id}')
        socketio.emit('ai_content_update', {
            'workshop_id': workshop_id,
            'type': 'action_plan',
            'content': new_plan_html
        }, room=f'workshop_room_{workshop_id}')

        # --- Return the rendered HTML list and the raw JSON ---
        # The JS already fetches raw JSON separately, so just returning HTML is fine
        return jsonify({"success": True, "content": new_plan_html})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error regenerating action plan for workshop {workshop_id}: {e}", exc_info=True) # Add exc_info for traceback
        return jsonify({"success": False, "message": "Server error during regeneration."}), 500


@workshop_bp.route("/<int:workshop_id>/edit/action_plan", methods=["POST"])
@login_required
def edit_action_plan(workshop_id):
    workshop = check_organizer_permission(workshop_id)
    # Expecting the updated JSON *string* in the 'content' field
    edited_json_string = request.json.get('content')

    if edited_json_string is None:
        return jsonify({"success": False, "message": "No content provided."}), 400

    # Validate if the received string is valid JSON before saving
    try:
        json.loads(edited_json_string) # Try parsing
        workshop.task_sequence = edited_json_string # Save the validated JSON string
        db.session.commit()

        # --- Render the HTML list from the *saved* JSON string ---
        edited_content_html = render_action_plan_html(workshop.task_sequence)

        # Emit update via WebSocket (optional)
        socketio.emit('ai_content_update', {
            'workshop_id': workshop_id,
            'type': 'action_plan',
            'content': edited_content_html
        }, room=f'workshop_lobby_{workshop_id}')
        socketio.emit('ai_content_update', {
            'workshop_id': workshop_id,
            'type': 'action_plan',
            'content': edited_content_html
        }, room=f'workshop_room_{workshop_id}')

        # --- Return the rendered HTML list ---
        return jsonify({"success": True, "content": edited_content_html})

    except json.JSONDecodeError:
         # If the client sent invalid JSON
         return jsonify({"success": False, "message": "Invalid JSON format received."}), 400
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving edited action plan for workshop {workshop_id}: {e}")
        return jsonify({"success": False, "message": "Server error saving edit."}), 500


# --- Add extract_json_block if it's not already imported/defined ---
# (Copied from app/agent/routes.py for completeness if needed here)
def extract_json_block(text):
    """
    Extract JSON object/array from a Markdown-style fenced LLM output block.
    Handles both objects {} and arrays [].
    """
    # Try matching array first
    match = re.search(r"```json\s*(\[.*?\])\s*```", text, re.DOTALL)
    if match:
        return match.group(1)
    # Try matching object
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return match.group(1)
    # Fallback: assume the whole text might be JSON, strip whitespace
    return text.strip()













@workshop_bp.route("/room/<int:workshop_id>")
@login_required
def workshop_room(workshop_id):
    """Displays the main workshop room when it's in progress."""
    workshop = Workshop.query.get_or_404(workshop_id)
    participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop.id, user_id=current_user.user_id
    ).first()

    # Permission checks
    if not participant:
        flash("You are not a participant in this workshop.", "danger")
        return redirect(url_for("workshop_bp.list_workshops"))

    if workshop.status == "scheduled":
        flash("Workshop has not started yet. Waiting in lobby...", "info")
        return redirect(url_for("workshop_bp.workshop_lobby", workshop_id=workshop_id))
    elif workshop.status == "completed":
        flash("Workshop completed. Viewing report...", "info")
        return redirect(url_for("workshop_bp.workshop_report", workshop_id=workshop_id))
    
    elif workshop.status != "inprogress" and workshop.status != "paused":
        flash(f"Workshop status is '{workshop.status}'. Cannot access room.", "warning")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))
    
    # Get participants list
    participants = WorkshopParticipant.query.filter_by(workshop_id=workshop.id).all()

    # TODO: Add logic for real-time features (transcription, chat, etc.)
    return render_template(
        "workshop_room.html",
        workshop=workshop,
        participants=participants,
        current_participant=participant,
    )


# Use POST for actions that change state
@workshop_bp.route("/start/<int:workshop_id>", methods=["POST"])
@login_required
def start_workshop(workshop_id):
    """Starts the workshop (organizer only)."""
    workshop = Workshop.query.get_or_404(workshop_id)
    participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop.id, user_id=current_user.user_id
    ).first()

    # Permission Check: Must be the organizer (or creator, adjust as needed)
    # Using creator for simplicity here, adjust if 'organizer' role is strictly enforced
    if not participant or workshop.created_by_id != current_user.user_id:
        # Or check: if not participant or participant.role != 'organizer':
        flash("You do not have permission to start this workshop.", "danger")
        # Return an error response suitable for AJAX if called via JS, or redirect
        return (
            jsonify({"success": False, "message": "Permission denied"}),
            403,
        )  # Or redirect

    if workshop.status != "scheduled":
        flash(f"Workshop cannot be started (status: {workshop.status}).", "warning")
        return (
            jsonify(
                {"success": False, "message": f"Workshop status is {workshop.status}"}
            ),
            400,
        )  # Or redirect

    # Update workshop status
    workshop.status = "inprogress"
    # Optionally record the actual start time
    # workshop.actual_start_time = datetime.utcnow()
    db.session.commit()

    # --- Emit WebSocket event to notify lobby participants ---
    socketio.emit(
        "workshop_started",
        {"workshop_id": workshop_id},
        room=f"workshop_lobby_{workshop_id}",
    )
    # --------------------------------------------------------

    flash("Workshop started successfully!", "success")
    # Redirect organizer to the room, or return success for AJAX
    # return redirect(url_for('workshop_bp.workshop_room', workshop_id=workshop_id))
    return jsonify(
        {
            "success": True,
            "message": "Workshop started",
            "redirect_url": url_for(
                "workshop_bp.workshop_room", workshop_id=workshop_id
            ),
        }
    )


# Use POST for actions that change state
@workshop_bp.route("/stop/<int:workshop_id>", methods=["POST"])
@login_required
def stop_workshop(workshop_id):
    """Stops the workshop (organizer only)."""
    workshop = Workshop.query.get_or_404(workshop_id)
    participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop.id, user_id=current_user.user_id
    ).first()

    # Permission Check: Must be the organizer
    if not participant or workshop.created_by_id != current_user.user_id:
        # Or check: if not participant or participant.role != 'organizer':
        flash("You do not have permission to stop this workshop.", "danger")
        return (
            jsonify({"success": False, "message": "Permission denied"}),
            403,
        )  # Or redirect

    if workshop.status != "inprogress":
        flash(f"Workshop cannot be stopped (status: {workshop.status}).", "warning")
        return (
            jsonify(
                {"success": False, "message": f"Workshop status is {workshop.status}"}
            ),
            400,
        )  # Or redirect

    # Update workshop status
    workshop.status = "completed"
    # Optionally record the actual end time
    # workshop.actual_end_time = datetime.utcnow()
    db.session.commit()

    # --- Emit WebSocket event to notify room participants ---
    socketio.emit(
        "workshop_stopped",
        {"workshop_id": workshop_id},
        room=f"workshop_room_{workshop_id}",
    )
    # ------------------------------------------------------

    flash("Workshop stopped and completed.", "success")
    # Redirect organizer to the report, or return success for AJAX
    # return redirect(url_for('workshop_bp.workshop_report', workshop_id=workshop_id))
    return jsonify(
        {
            "success": True,
            "message": "Workshop stopped",
            "redirect_url": url_for(
                "workshop_bp.workshop_report", workshop_id=workshop_id
            ),
        }
    )


@workshop_bp.route("/report/<int:workshop_id>")
@login_required
def workshop_report(workshop_id):
    """Displays the post-workshop report."""
    workshop = Workshop.query.get_or_404(workshop_id)
    participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop.id, user_id=current_user.user_id
    ).first()

    # Permission checks
    if not participant:
        flash("You are not a participant in this workshop.", "danger")
        return redirect(url_for("workshop_bp.list_workshops"))

    if workshop.status != "completed":
        flash("Workshop report is only available after completion.", "warning")
        # Redirect based on current status
        if workshop.status == "scheduled":
            return redirect(
                url_for("workshop_bp.workshop_lobby", workshop_id=workshop_id)
            )
        elif workshop.status == "inprogress":
            return redirect(
                url_for("workshop_bp.workshop_room", workshop_id=workshop_id)
            )
        else:
            return redirect(
                url_for("workshop_bp.view_workshop", workshop_id=workshop_id)
            )

    # Get participants list
    participants = WorkshopParticipant.query.filter_by(workshop_id=workshop.id).all()
    # TODO: Fetch generated report data (summary, transcript, action items, etc.)

    return render_template(
        "workshop_report.html",
        workshop=workshop,
        participants=participants,
        current_participant=participant,
    )
    # TODO: Pass report data here

##################### PAUSE AND RESUME WORKSHOP ####################################

# Pause workshop
@workshop_bp.route("/pause/<int:workshop_id>", methods=["POST"])
@login_required
def pause_workshop(workshop_id):
    """Pauses the workshop (organizer only)."""
    workshop = Workshop.query.get_or_404(workshop_id)
    participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop.id, user_id=current_user.user_id
    ).first()
    # Permission Check: Must be the organizer
    if not participant or workshop.created_by_id != current_user.user_id:
        flash("You do not have permission to pause this workshop.", "danger")
        return (
            jsonify({"success": False, "message": "Permission denied"}),
            403,
        )
    # Or check: if not participant or participant.role != 'organizer':
    # Check status
    if workshop.status != "inprogress":
        # Only allow pausing if currently in progress
        # or already paused
        flash(f"Workshop cannot be paused (status: {workshop.status}).", "warning")
        return (
            jsonify(
                {"success": False, "message": f"Workshop status is {workshop.status}"}
            ),
            400,
        )
    # Update workshop status
    workshop.status = "paused"
    # Optionally record the actual pause time
    # workshop.actual_pause_time = datetime.utcnow()
    db.session.commit()
    # --- Emit WebSocket event to notify room participants ---
    socketio.emit(
        "workshop_paused",
        {"workshop_id": workshop_id},
        room=f"workshop_room_{workshop_id}",
    )
    # ------------------------------------------------------
    flash("Workshop paused successfully.", "success")
    return jsonify(
        success=True, 
        redirect_url=url_for("workshop_bp.workshop_room", workshop_id=workshop_id)
        )
    
# Resume workshop
@workshop_bp.route("/resume/<int:workshop_id>", methods=["POST"])
@login_required
def resume_workshop(workshop_id):
    """Resumes the workshop (organizer only)."""
    workshop = Workshop.query.get_or_404(workshop_id)
    participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop.id, user_id=current_user.user_id
    ).first()
    # Permission Check: Must be the organizer
    if not participant or workshop.created_by_id != current_user.user_id:
        flash("You do not have permission to resume this workshop.", "danger")
        return (
            jsonify({"success": False, "message": "Permission denied"}),
            403,
        )
    # Or check: if not participant or participant.role != 'organizer':
    # Check status
    if workshop.status != "paused":
        flash(f"Workshop cannot be resumed (status: {workshop.status}).", "warning")
        return (
            jsonify(
                {"success": False, "message": f"Workshop status is {workshop.status}"}
            ),
            400,
        )
    # Update workshop status
    workshop.status = "inprogress"
    # Optionally record the actual resume time
    # workshop.actual_resume_time = datetime.utcnow()
    db.session.commit()
    # --- Emit WebSocket event to notify room participants ---
    socketio.emit(
        "workshop_resumed",
        {"workshop_id": workshop_id},
        room=f"workshop_room_{workshop_id}",
    )
    # ------------------------------------------------------
    flash("Workshop resumed successfully.", "success")
    return jsonify(
        success=True,
        redirect_url=url_for("workshop_bp.workshop_room", workshop_id=workshop_id)
    )
    
    
# restart workshop
@workshop_bp.route("/restart/<int:workshop_id>", methods=["POST"])
@login_required
def restart_workshop(workshop_id):
    """Restarts the workshop (organizer only)."""
    workshop = Workshop.query.get_or_404(workshop_id)
    participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop.id, user_id=current_user.user_id
    ).first()
    # Permission Check: Must be the organizer
    if not participant or workshop.created_by_id != current_user.user_id:
        flash("You do not have permission to restart this workshop.", "danger")
        return (
            jsonify({"success": False, "message": "Permission denied"}),
            403,
        )
    # Or check: if not participant or participant.role != 'organizer':
    # Check status
    if workshop.status != "cancelled":
        flash(f"Workshop cannot be restarted (status: {workshop.status}).", "warning")
        return (
            jsonify(
                {"success": False, "message": f"Workshop status is {workshop.status}"}
            ),
            400,
        )
    # Update workshop status
    workshop.status = "inprogress"
    # Optionally record the actual restart time
    # workshop.actual_restart_time = datetime.utcnow()
    db.session.commit()
    # --- Emit WebSocket event to notify room participants ---
    socketio.emit(
        "workshop_restarted",
        {"workshop_id": workshop_id},
        room=f"workshop_room_{workshop_id}",
    )
    # ------------------------------------------------------
    flash("Workshop restarted successfully.", "success")
    return jsonify(
        success=True,
        redirect_url=url_for("workshop_bp.workshop_room", workshop_id=workshop_id)
    )

# #################################################################################
# WORKSHOP ROOM
##################################################################################
# --- Begin Workshop Introduction Task ---
@workshop_bp.route("/<int:workshop_id>/begin_intro", methods=["POST"])
@login_required
def begin_intro(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    # Only organizer can start the introduction
    if workshop.created_by_id != current_user.user_id:
        return jsonify({"success": False, "message": "Permission denied"}), 403

    # Call the agent to get JSON intro
    raw = generate_introduction_text(workshop_id)
    print(f"[DEBUG] Raw LLM intro: {raw}")

    # Attempt to extract clean JSON
    cleaned_raw = extract_json_block(raw)
    try:
        intro = json.loads(cleaned_raw)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Failed to parse intro JSON: {e}")
        return jsonify({"success": False, "message": "Invalid JSON format returned by the LLM."}), 500

    # Persist as a BrainstormTask
    task = BrainstormTask(
        workshop_id=workshop.id,
        title="Introduction",
        prompt=json.dumps(intro),
        duration=int(request.form.get("duration", 60)),  # default 60s
        status="running",
        started_at=datetime.utcnow()
    )
    db.session.add(task)
    db.session.commit()

    # Emit to everyone in the room
    socketio.emit("introduction_start", {
        "task_id": task.id,
        "welcome": intro["welcome"],
        "goals": intro["goals"],
        "rules": intro["rules"],
        "instructions": intro["instructions"],
        "task": intro["task"],
        "task_type": intro["task_type"],
        "duration": intro["task_duration"],
        # "duration": int(intro["task_duration"].split()[0]),  # e.g. "5 minutes" → 5
    }, room=f"workshop_room_{workshop_id}")

    return jsonify({"success": True}), 200



# --- Workshop Next Task --------------
@workshop_bp.route("/next_task/<int:workshop_id>", methods=["POST"])
@login_required
def next_task(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    if workshop.created_by_id != current_user.user_id:
        return jsonify({"success": False, "message": "Permission denied"}), 403
    
    # --- Action Plan Logic ---
    action_plan_items = []
    next_action_plan_item = None
    next_index = 0

    if workshop.task_sequence:
        try:
            # Clean potential markdown/fencing if LLM added it
            cleaned_json_string = extract_json_block(workshop.task_sequence)
            action_plan_items = json.loads(cleaned_json_string)
            if not isinstance(action_plan_items, list):
                action_plan_items = [] # Ensure it's a list
        except (json.JSONDecodeError, TypeError):
            current_app.logger.warning(f"Could not parse action plan JSON for workshop {workshop_id}. Proceeding without plan.")
            action_plan_items = []

    if action_plan_items:
        current_index = workshop.current_action_plan_index
        if current_index is None: # First task after intro
            next_index = 0
        else:
            next_index = current_index + 1

        if 0 <= next_index < len(action_plan_items):
            next_action_plan_item = action_plan_items[next_index]
            current_app.logger.info(f"Next task for workshop {workshop_id} based on action plan item {next_index}: {next_action_plan_item.get('phase')}")
        else:
            # Reached the end of the action plan
            current_app.logger.info(f"Workshop {workshop_id} has completed all action plan items.")
            # Option 1: Stop generating tasks
            # return jsonify({"success": False, "message": "All planned tasks completed."}), 400
            # Option 2: Generate a generic wrap-up task (pass None to agent)
            next_action_plan_item = {"phase": "Wrap-up", "description": "Discuss key takeaways and next steps."}
            # Or Option 3: Emit a specific event
            # socketio.emit("plan_completed", {...}, room=f"workshop_room_{workshop_id}")
            # return jsonify({"success": True, "message": "Action plan complete."}), 200

    else:
        # No action plan, proceed with generic task generation (or return error)
        current_app.logger.warning(f"No valid action plan found for workshop {workshop_id}. Generating generic task.")
        # You might want to return an error here if an action plan is mandatory
        # return jsonify({"success": False, "message": "Workshop action plan is missing or invalid."}), 400
        # Or allow generic task generation by passing None
        next_action_plan_item = None
    
    # --- Call Agent ---
    # Pass the specific action plan item (or None) to the generator
    raw_task_data = generate_next_task_text(workshop_id, action_plan_item=next_action_plan_item)
    cleaned_raw = extract_json_block(raw_task_data)

    try:
        task_payload = json.loads(cleaned_raw)
        if not isinstance(task_payload, dict): # Basic validation
             raise ValueError("LLM did not return a JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        current_app.logger.error(f"Failed to parse next task JSON from LLM for workshop {workshop_id}: {e}. Raw: {raw_task_data[:200]}")
        # Try to extract at least a description if possible, otherwise fail
        fallback_description = raw_task_data if isinstance(raw_task_data, str) else "Error generating task details."
        task_payload = {
            "title": "Task Generation Error",
            "task_type": "Error",
            "task_description": fallback_description,
            "instructions": "Please contact the organizer.",
            "task_duration": "60" # Default duration
        }
        # Or return a hard error:
        # return jsonify({"success": False, "message": "Invalid task format received from AI."}), 500

    # --- Duration Parsing ---
    raw_duration = task_payload.get("task_duration", "60") # Default to 60 seconds
    duration_seconds = 60 # Default
    if isinstance(raw_duration, (int, float)):
        duration_seconds = int(raw_duration)
    elif isinstance(raw_duration, str):
        match = re.match(r"(\d+)", raw_duration) # Extract leading numbers
        if match:
            duration_seconds = int(match.group(1))
            # Optional: Check for 'minute' and multiply
            if "minute" in raw_duration.lower():
                 duration_seconds *= 60
        else:
             current_app.logger.warning(f"Could not parse duration '{raw_duration}', defaulting to 60s.")
             duration_seconds = 60
             
    # --- Persist Task ---
    task = BrainstormTask(
        workshop_id=workshop_id,
        title=task_payload.get("title", "Workshop Task"), # Use title from payload
        prompt=json.dumps(task_payload), # Store the whole payload for potential future use
        duration=duration_seconds,
        status="running", # Set status immediately
        started_at=datetime.utcnow()
    )
    db.session.add(task)
    
    # --- Update Workshop Index ---
    # Only update if we successfully used an item from the plan
    if action_plan_items and 0 <= next_index < len(action_plan_items):
         workshop.current_action_plan_index = next_index

    db.session.commit() # Commit both task and workshop update

    # --- Broadcast Task ---
    socketio.emit("task_ready", {
        "task_id":   task.id,
        "title":     task_payload.get("title", task.title),
        "description": task_payload.get("task_description", "No description provided."),
        "instructions": task_payload.get("instructions", "Submit your ideas."),
        "duration":  task.duration # Send the calculated duration in seconds
    }, room=f"workshop_room_{workshop_id}")

    return jsonify({"success": True}), 200






# --- Submit Idea --- TODO: NOT DISPLAYING ON UI
@workshop_bp.route("/<int:workshop_id>/submit_idea", methods=["POST"])
@login_required
def submit_idea(workshop_id):
    task_id = request.json.get("task_id")
    content = request.json.get("content","").strip()
    participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop_id, user_id=current_user.user_id
    ).first_or_404()

    idea = BrainstormIdea(task_id=task_id, participant_id=participant.id, content=content)
    db.session.add(idea)
    db.session.commit()

    socketio.emit("new_idea", {
        "user": current_user.first_name or current_user.email,
        "content": content,
        "idea_id": idea.id
    }, room=f"workshop_room_{workshop_id}")
    return jsonify(success=True), 200

