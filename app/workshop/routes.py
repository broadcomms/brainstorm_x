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
import markdown # Import markdown
from datetime import datetime, timedelta

# --- Socket.IO Room Join/Leave Handlers ---
from flask_socketio import join_room, leave_room

from app.utils.json_utils import extract_json_block
from app.extensions import socketio
from app.service.routes.task import get_next_task_payload


from sqlalchemy.orm import joinedload, selectinload, subqueryload # <--- Add subqueryload
from sqlalchemy.exc import IntegrityError

@socketio.on('join_room')
@login_required
def handle_join_room(data):
    """
    Join the user to the specified Socket.IO room.
    Expects data dict with key 'room'.
    """
    room = data.get('room')
    if room:
        join_room(room)
        current_app.logger.info(f"User {current_user.user_id} joined room {room}")

@socketio.on('leave_room')
@login_required
def handle_leave_room(data):
    """
    Remove the user from the specified Socket.IO room.
    Expects data dict with key 'room'.
    """
    room = data.get('room')
    if room:
        leave_room(room)
        current_app.logger.info(f"User {current_user.user_id} left room {room}")

from app.extensions import db
from app.models import (
    BrainstormIdea,
    BrainstormTask,
    IdeaCluster,
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

from app.service.routes.agenda import generate_agenda_text
from app.service.routes.rules import generate_rules_text # Assuming this exists
from app.service.routes.icebreaker import generate_icebreaker_text # Assuming this exists
from app.service.routes.tip import generate_tip_text # Assuming this exists

# Import aggregate_pre_workshop_data from the new utils file ---
from app.utils.data_aggregation import aggregate_pre_workshop_data
# Import extract_json_block
from app.service.routes.agent import extract_json_block 


from app.service.routes.introduction import get_introduction_payload
from app.service.routes.task import get_next_task_payload

from concurrent.futures import ThreadPoolExecutor
# Create a thread pool for asynchronous generation
executor = ThreadPoolExecutor(max_workers=4)

APP_NAME = os.getenv("APP_NAME", "BrainStormX")
workshop_bp = Blueprint('workshop_bp', __name__,
                        template_folder='templates'
                        # Remove static_folder='static' if present and not intended
                       )


# --- Import Socket.IO Emitters ---
# It's cleaner to import specific emitters if sockets.py defines them
from app.sockets import (
    emit_introduction_start,
    emit_task_ready,
    emit_clusters_ready,        # New emitter for cluster/voting phase
    emit_feasibility_ready,     # New emitter for feasibility phase
    emit_summary_ready,         # New emitter for summary phase
    emit_workshop_paused,
    emit_workshop_resumed,
    emit_workshop_stopped,
    
    # Import helpers needed for beacon_leave simulation if defined in sockets.py
    _sid_registry,
    _room_presence,
    _broadcast_participant_list
)

from app.config import TASK_SEQUENCE # <-- Import from config








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
    if (raw):
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
    
 

    
    
    return render_template(
        "workshop_details.html",
        workshop=workshop,
        participants=participants,
        potential_participants=potential_participants,
        available_documents=available_documents,
        linked_documents=linked_docs,
        user_is_organizer=user_is_organizer# Pass raw JSON string for JS
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
    
# Add profile picture URL to each participant
    for participant in participants:
        participant.profile_pic_url = url_for('static', filename='images/default-profile.png')

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
        
        # Adjust check based on actual error/fallback message from generate_tip_text
        ai_tip_raw = generate_tip_text(workshop_id)
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

    # Add profile picture URL to each participant
    for participant in participants:
        participant.profile_pic_url = url_for('static', filename='images/default-profile.png')
        
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
        ai_agenda=ai_agenda_html,
        ai_rules=ai_rules_html,
        ai_icebreaker=ai_icebreaker_html,
        ai_tip=ai_tip_html,
        user_is_organizer=is_organizer_flag,
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
        new_agenda = generate_agenda_text(workshop_id)
        workshop.agenda = new_agenda
        db.session.commit()

        # Emit the update to the room
        socketio.emit(
            "ai_content_update",
            {
                "workshop_id": workshop_id,
                "type": "agenda",
                "content": new_agenda,
            },
            room=f"workshop_lobby_{workshop_id}",
        )
        return jsonify({"success": True}), 200
    except Exception as e:
        current_app.logger.error(f"Error regenerating agenda: {e}")
        return jsonify({"error": "Failed to regenerate agenda"}), 500


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

























# --- Render Workshop Room ---
@workshop_bp.route("/room/<int:workshop_id>")
@login_required
def workshop_room(workshop_id):
    """Displays the main workshop room."""
    workshop = Workshop.query.options(
        selectinload(Workshop.current_task) # Eager load current task if needed often
    ).get_or_404(workshop_id)

    participant = WorkshopParticipant.query.filter_by(
        workshop_id=workshop.id, user_id=current_user.user_id
    ).first()

    if not participant:
        flash("You are not a participant in this workshop.", "danger")
        return redirect(url_for("workshop_bp.list_workshops"))

    # Redirect based on status
    if workshop.status == "scheduled":
        return redirect(url_for("workshop_bp.workshop_lobby", workshop_id=workshop_id))
    elif workshop.status == "completed":
        return redirect(url_for("workshop_bp.workshop_report", workshop_id=workshop_id))
    elif workshop.status not in ["inprogress", "paused"]:
        flash(f"Workshop status is '{workshop.status}'. Cannot access room.", "warning")
        return redirect(url_for("workshop_bp.view_workshop", workshop_id=workshop_id))

    # No need to fetch participants/docs here, JS will request/receive via sockets

    return render_template(
        "workshop_room.html",
        workshop=workshop,
        # Pass minimal necessary data, JS handles the rest
        # participants=participants, # Removed, handled by sockets
        current_participant=participant, # Keep for user context
    )

# --- Workshop Lifecycle Routes ---

@workshop_bp.route("/start/<int:workshop_id>", methods=["POST"])
@login_required
def start_workshop(workshop_id):
    """Starts the workshop (organizer only)."""
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user): # Use helper
        return jsonify({"success": False, "message": "Permission denied"}), 403

    if workshop.status != "scheduled":
        return jsonify({"success": False, "message": f"Workshop status is {workshop.status}"}), 400

    workshop.status = "inprogress"
    # Reset timer fields in case it was previously stopped/paused incorrectly
    workshop.current_task_id = None
    workshop.timer_start_time = None
    workshop.timer_paused_at = None
    workshop.timer_elapsed_before_pause = 0
    workshop.current_task_index = None # Reset task sequence index

    db.session.commit()

    socketio.emit(
        "workshop_started",
        {"workshop_id": workshop_id},
        room=f"workshop_lobby_{workshop_id}",
    )
    socketio.emit(
        "workshop_status_update",
        {"workshop_id": workshop_id, "status": "inprogress"},
        room=f"workshop_room_{workshop_id}",
    )


    flash("Workshop started successfully!", "success")
    return jsonify(
        success=True,
        message="Workshop started",
        redirect_url=url_for("workshop_bp.workshop_room", workshop_id=workshop_id),
    )

@workshop_bp.route("/pause/<int:workshop_id>", methods=["POST"])
@login_required
def pause_workshop(workshop_id):
    """Pauses the workshop (organizer only)."""
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user):
        return jsonify({"success": False, "message": "Permission denied"}), 403

    if workshop.status != "inprogress":
        return jsonify({"success": False, "message": f"Workshop status is {workshop.status}"}), 400

    workshop.status = "paused"
    if workshop.timer_start_time: # Only calculate elapsed time if a timer was running
        elapsed_this_run = (datetime.utcnow() - workshop.timer_start_time).total_seconds()
        workshop.timer_elapsed_before_pause += int(elapsed_this_run)
        workshop.timer_paused_at = datetime.utcnow()
        workshop.timer_start_time = None # Clear start time as it's now paused

    db.session.commit()

    emit_workshop_paused(f"workshop_room_{workshop_id}", workshop_id) # Use helper emitter

    flash("Workshop paused successfully.", "success")
    # No redirect needed if handled by socket event + JS reload
    return jsonify(success=True, message="Workshop paused")


@workshop_bp.route("/resume/<int:workshop_id>", methods=["POST"])
@login_required
def resume_workshop(workshop_id):
    """Resumes the workshop (organizer only)."""
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user):
        return jsonify({"success": False, "message": "Permission denied"}), 403

    if workshop.status != "paused":
        return jsonify({"success": False, "message": f"Workshop status is {workshop.status}"}), 400

    workshop.status = "inprogress"
    if workshop.current_task_id and workshop.timer_paused_at: # Only set start time if resuming a task timer
        workshop.timer_start_time = datetime.utcnow() # Set new start time for the current run
        workshop.timer_paused_at = None # Clear paused time

    db.session.commit()

    emit_workshop_resumed(f"workshop_room_{workshop_id}", workshop_id) # Use helper emitter

    flash("Workshop resumed successfully.", "success")
    # No redirect needed if handled by socket event + JS reload
    return jsonify(success=True, message="Workshop resumed")



@workshop_bp.route("/stop/<int:workshop_id>", methods=["POST"])
@login_required
def stop_workshop(workshop_id):
    """Stops the workshop (organizer only)."""
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user):
        return jsonify({"success": False, "message": "Permission denied"}), 403

    # Allow stopping from 'inprogress' or 'paused'
    if workshop.status not in ["inprogress", "paused"]:
        return jsonify({"success": False, "message": f"Workshop status is {workshop.status}"}), 400

    workshop.status = "completed"
    # Clear current task and timer state
    if workshop.current_task_id:
        task = BrainstormTask.query.get(workshop.current_task_id)
        if task and task.status == 'running':
            task.status = 'completed' # Mark task as completed
            task.ended_at = datetime.utcnow()
    workshop.current_task_id = None
    workshop.timer_start_time = None
    workshop.timer_paused_at = None
    workshop.timer_elapsed_before_pause = 0
    # workshop.current_task_index = None # Keep index if needed for report?

    db.session.commit()

    emit_workshop_stopped(f"workshop_room_{workshop_id}", workshop_id) # Use helper emitter

    flash("Workshop stopped and completed.", "success")
    return jsonify(
        success=True,
        message="Workshop stopped",
        redirect_url=url_for("workshop_bp.workshop_report", workshop_id=workshop_id),
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



# --- Begin Workshop Introduction Task ---
@workshop_bp.route("/<int:workshop_id>/begin_intro", methods=["POST"])
@login_required
def begin_intro(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    if not is_organizer(workshop, current_user):
        return jsonify(success=False, message="Permission denied"), 403

    # Prevent starting intro if already started or not scheduled/inprogress
    if workshop.current_task_id or workshop.status not in ['scheduled', 'inprogress']:
         return jsonify(success=False, message="Workshop introduction cannot be started at this time."), 400

    # If starting from scheduled, update status
    if workshop.status == 'scheduled':
        workshop.status = 'inprogress'

    result = get_introduction_payload(workshop_id)
    if isinstance(result, tuple) and not isinstance(result[0], dict):
        err_msg, code = result
        return jsonify(success=False, message=err_msg), code

    payload = result
    try:
        
        
        
        
        
        
        # --- INTERCEPTION TIMER OVERRIDE FOR DEBUGGING ---
        original_duration = payload.get("task_duration", 60)
        override_duration_str = current_app.config.get('DEBUG_OVERRIDE_TASK_DURATION') # Get from config
        if override_duration_str:
            try:
                override_duration = int(override_duration_str)
                current_app.logger.warning(f"[DEBUG] Overriding intro task duration from {original_duration} to {override_duration}s")
                payload['task_duration'] = override_duration
            except (ValueError, TypeError):
                current_app.logger.error(f"[DEBUG] Invalid DEBUG_OVERRIDE_TASK_DURATION value: {override_duration_str}")
        # --- INTERCEPTION TIMER OVERRIDE FOR DEBUGGING ---
        
        
        
        
        
        
        try:
            duration_seconds = int(payload.get("task_duration", 60))
        except (ValueError, TypeError):
            duration_seconds = 60
            current_app.logger.warning(f"Invalid task_duration in intro payload for {workshop_id}, defaulting to 60s.")

        intro_task = BrainstormTask(
            workshop_id=workshop_id,
            title=payload.get("title", "Introduction & Warm-up"),
            prompt=json.dumps(payload), # Store full payload for context
            duration=duration_seconds,
            status="running",
            started_at=datetime.utcnow()
        )
        db.session.add(intro_task)
        db.session.flush() # Get the ID

        # Update workshop state
        workshop.current_task_id = intro_task.id
        workshop.timer_start_time = intro_task.started_at # Use task start time
        workshop.timer_paused_at = None
        workshop.timer_elapsed_before_pause = 0
        workshop.current_task_index = -1 # Indicate intro task is before index 0

        db.session.commit()

        
        payload['task_id'] = intro_task.id # Add task ID to payload for client
        payload['duration'] = intro_task.duration # Ensure duration is correct

        emit_introduction_start(f'workshop_room_{workshop.id}', payload) # Use helper
        return jsonify(success=True)

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error creating welcome and warm-up task for {workshop_id}: {e}", exc_info=True)
        return jsonify(success=False, message="Server error starting introduction. /begin-into"), 500



# ################################################################################
# WORKSHOP TASK MANAGEMENT
##################################################################################
# --- Workshop Next Task --------------
from app.service.routes.brainstorming import get_brainstorming_task_payload

@workshop_bp.route("/<int:workshop_id>/next_task", methods=["POST"])
@login_required
def next_task(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)

    # --- Permission Check: Only Organizer ---
    if not is_organizer(workshop, current_user):
        abort(403)

    # Ensure the workshop is in progress
    if workshop.status != "inprogress":
        return jsonify({"error": "Workshop is not in progress."}), 400

    # Load the task sequence
    task_sequence = TASK_SEQUENCE

    if not task_sequence:
        current_app.logger.warning(f"Task sequence is empty for workshop {workshop_id}")
        return jsonify({"error": "No tasks in the action plan."}), 400

    # Validate the current index
    # --- FIX: Default index should be -1 before first task, so next is 0 ---
    current_index = workshop.current_task_index if workshop.current_task_index is not None else -1
    next_index = current_index + 1
    # --------------------------------------------------------------------
    current_app.logger.info(f"TRACING BREAK POINT: task_sequence: {task_sequence}")
    current_app.logger.info(f"TRACING BREAK POINT: current_index: {current_index}, next_index: {next_index}") # Log next index

    if next_index >= len(task_sequence): # Check if next_index is out of bounds
        current_app.logger.warning(f"No more tasks in the sequence for workshop {workshop_id}")
        return jsonify({"error": "No more tasks in the action plan."}), 400

    # Determine the next task type
    next_task_type = task_sequence[next_index] # Use next_index
    task_payload = None
    task_function = None # To store the function to call
    current_app.logger.info(f"TRACING BREAK POINT: next_task_type: {next_task_type}") # Log next index
    
    
    
    
    
    
    # --- Map task types to functions ---
    if next_task_type == "brainstorming":
        task_payload = get_brainstorming_task_payload(workshop_id)
        
    elif next_task_type == "clustering_voting":
        # Need to fetch ideas from the previous brainstorming task first
        previous_task_id = workshop.current_task_id
        if not previous_task_id:
             return jsonify({"error": "Cannot start clustering/voting without a completed brainstorming task."}), 400
        ideas = BrainstormIdea.query.filter_by(task_id=previous_task_id).all()
        if not ideas:
             return jsonify({"error": "No ideas found from the previous task to cluster."}), 400
        # Pass ideas to the generator function
        task_payload = '' # generate_clusters_and_voting_task(workshop_id, ideas) # Call directly for now
        # Note: This assumes generate_clusters_and_voting_task handles DB saving & returns payload or error tuple
    elif next_task_type == "results_feasibility":
         # Need to fetch clusters and votes from the previous task
        task_payload = '' # workshop.current_task_id # Assuming the voting task updates current_task_id
        if not previous_task_id:
             return jsonify({"error": "Cannot start feasibility without a completed voting task."}), 400
        # Fetch clusters associated with the workshop (or task if linked) including vote counts
        clusters_with_votes = IdeaCluster.query.filter_by(workshop_id=workshop_id).options(selectinload(IdeaCluster.votes)).all() # Adjust filter if needed
        if not clusters_with_votes:
             return jsonify({"error": "No clusters found from the previous task."}), 400
        #task_payload = generate_feasibility_task(workshop_id, clusters_with_votes) # Call directly
    elif next_task_type == "discussion":
        task_payload = '' #  generate_discussion_task(workshop_id) # Call directly
    elif next_task_type == "summary":
        task_payload = '' #  = generate_summary_task(workshop_id) # Call directly
    else:
        return jsonify({"error": f"Unsupported task type: {next_task_type}"}), 400
    # -----------------------------------

    # --- Call the selected function if not already called ---
    if task_function:
        task_payload = task_function(workshop_id)
    # ------------------------------------------------------

    # Handle errors from the task generation function
    if isinstance(task_payload, tuple):
        error_message, status_code = task_payload
        return jsonify({"error": error_message}), status_code

    # --- Determine Emitter based on task_type ---
    room = f"workshop_room_{workshop_id}"
    task_type_in_payload = task_payload.get("task_type")

    if task_type_in_payload == "brainstorming":
        emit_task_ready(room, task_payload)
    elif task_type_in_payload == "clustering_voting":
        emit_clusters_ready(room, task_payload) # Use specific emitter
    elif task_type_in_payload == "results_feasibility":
        emit_feasibility_ready(room, task_payload) # Use specific emitter
    elif task_type_in_payload == "discussion":
        emit_task_ready(room, task_payload) # Re-use generic task emitter? Or create specific?
    elif task_type_in_payload == "summary":
        emit_summary_ready(room, task_payload) # Use specific emitter
    else:
        current_app.logger.error(f"Unknown task type '{task_type_in_payload}' in payload for workshop {workshop_id}")
        return jsonify({"error": "Internal error: Unknown task type generated."}), 500
    # ------------------------------------------

    # Update the workshop's current task index *after* successful generation and emission
    workshop.current_task_index = next_index # Update to the index of the task just started
    # The task_payload should contain the new task_id, which was set in get_brainstorming_task_payload
    # For other task types, ensure their respective functions also update workshop.current_task_id
    # workshop.current_task_id = task_payload.get('task_id') # This is handled within the task generation functions now

    db.session.commit()

    return jsonify({"success": True, "task": task_payload})

@workshop_bp.route("/<int:workshop_id>/submit_idea", methods=["POST"])
@login_required
def submit_idea(workshop_id):
    data = request.get_json()
    if not data: return jsonify(success=False, message="Invalid request."), 400

    task_id = data.get("task_id")
    content = data.get("content", "").strip()

    if not task_id: return jsonify(success=False, message="Task ID required."), 400
    if not content: return jsonify(success=False, message="Idea content required."), 400

    workshop = Workshop.query.get(workshop_id) # Get workshop to check current task and timer
    if not workshop: return jsonify(success=False, message="Workshop not found."), 404

    participant_record = WorkshopParticipant.query.filter_by(
         workshop_id=workshop_id, user_id=current_user.user_id
    ).first()
    if not participant_record: return jsonify(success=False, message="Not a participant."), 403

    # --- Validation: Check against current task and timer ---
    if workshop.current_task_id != task_id:
        return jsonify(success=False, message="Cannot submit to inactive task."), 400

    # Check if timer is still running for the current task
    remaining_time = workshop.get_remaining_task_time()
    if remaining_time <= 0 and workshop.status == 'inprogress': # Allow submission if paused
         return jsonify(success=False, message="Time for this task has expired."), 400
    # -------------------------------------------------------

    task = BrainstormTask.query.get(task_id) # Task should exist if it's the current one
    if not task: return jsonify(success=False, message="Task not found."), 404 # Should not happen

    try:
        idea = BrainstormIdea(
            task_id=task.id,
            participant_id=participant_record.id,
            content=content,
            timestamp=datetime.utcnow()
        )
        db.session.add(idea)
        db.session.commit()

        user_display_name = current_user.first_name or current_user.email.split('@')[0]
        socketio.emit("new_idea", { # Changed event name to match JS
            "user": user_display_name,
            "content": content,
            "idea_id": idea.id,
            "task_id": task.id
        }, room=f"workshop_room_{workshop_id}")

        return jsonify(success=True, idea_id=idea.id), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error saving idea for task {task_id}: {e}", exc_info=True)
        return jsonify(success=False, message="Error saving idea."), 500


@workshop_bp.route("/<int:workshop_id>/beacon_leave", methods=['POST'])
def beacon_leave(workshop_id):
    """Handles leave notification via navigator.sendBeacon."""
    # Use try-except as request might be incomplete on browser close
    try:
        data = request.get_json(silent=True) or {}
        user_id = data.get('user_id')
        room = data.get('room') # e.g., workshop_room_123

        if user_id and room and room == f"workshop_room_{workshop_id}":
            current_app.logger.info(f"[Beacon] Received leave notification for user {user_id} from room {room}")

            # --- Simulate disconnect logic ---
            # Find SIDs associated with this user in this room
            sids_to_remove = [sid for sid, info in _sid_registry.items() if info.get("workshop_id") == workshop_id and info.get("user_id") == user_id]

            if sids_to_remove:
                 _room_presence[room].discard(user_id)
                 for sid in sids_to_remove:
                     _sid_registry.pop(sid, None)
                 current_app.logger.info(f"[Beacon] Cleaned up presence for user {user_id} in room {room}")
                 # Broadcast update if room still active
                 if room in _room_presence and _room_presence[room]:
                     _broadcast_participant_list(room, workshop_id)
                 elif room in _room_presence:
                     del _room_presence[room]
            # --- End Simulate disconnect ---

        else:
             current_app.logger.warning(f"[Beacon] Received invalid leave data for workshop {workshop_id}: {data}")

    except Exception as e:
        current_app.logger.error(f"[Beacon] Error processing leave beacon for workshop {workshop_id}: {e}")

    # Beacon expects a 2xx response, often 204 No Content
    return '', 204